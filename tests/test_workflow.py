"""端到端业务流程测试。"""

from conftest import confirm_all, create_manuscript


def test_create_and_get_working_copy(client):
    m = create_manuscript(client)
    assert m["revision"] == 1
    assert [s["position"] for s in m["segments"]] == [1, 2, 3]
    assert all(s["status"] == "empty" for s in m["segments"])
    assert all(s["source_version_no"] == 1 for s in m["segments"])

    got = client.get(f"/manuscripts/{m['id']}").json()
    assert len(got["segments"]) == 3


def test_submit_then_confirm(client):
    m = create_manuscript(client)
    seg = m["segments"][0]
    sid = seg["segment_id"]

    r = client.put(
        f"/segments/{sid}/translations",
        json={"target_text": "译文1", "source_version_no": 1},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"

    r = client.post(
        f"/segments/{sid}/translations/confirm",
        json={"target_text": "译文1-定稿", "source_version_no": 1},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["matches_current_source"] is True


def test_stale_submit_and_confirm_are_rejected(client):
    m = create_manuscript(client)
    sid = m["segments"][0]["segment_id"]

    # 先基于 v1 提交
    client.put(
        f"/segments/{sid}/translations",
        json={"target_text": "译文1", "source_version_no": 1},
    )
    # 原文改写到 v2
    r = client.put(
        f"/manuscripts/{m['id']}/segments/1/source",
        json={"text": "原文1-改写", "expected_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 2

    # 依据过期版本提交 -> 409
    r = client.put(
        f"/segments/{sid}/translations",
        json={"target_text": "依据旧版", "source_version_no": 1},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_source_version"

    # 依据过期版本确认 -> 409
    r = client.post(
        f"/segments/{sid}/translations/confirm",
        json={"target_text": "依据旧版", "source_version_no": 1},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_source_version"


def test_rewrite_invalidates_only_that_segment(client):
    m = create_manuscript(client)
    confirm_all(client, m)

    # 改写第 2 段
    r = client.put(
        f"/manuscripts/{m['id']}/segments/2/source",
        json={"text": "原文2-改写", "expected_revision": 1},
    )
    assert r.status_code == 200
    segs = {s["position"]: s for s in r.json()["segments"]}

    assert segs[2]["source_version_no"] == 2
    assert segs[2]["status"] == "pending_review"
    assert segs[2]["translation"]["matches_current_source"] is False
    # 其他段不受影响
    assert segs[1]["status"] == "confirmed"
    assert segs[1]["source_version_no"] == 1
    assert segs[3]["status"] == "confirmed"


def test_resubmit_after_rewrite_clears_pending_review(client):
    m = create_manuscript(client)
    confirm_all(client, m)
    sid2 = m["segments"][1]["segment_id"]

    client.put(
        f"/manuscripts/{m['id']}/segments/2/source",
        json={"text": "原文2-改写", "expected_revision": 1},
    )
    r = client.put(
        f"/segments/{sid2}/translations",
        json={"target_text": "新译文2", "source_version_no": 2},
    )
    assert r.json()["status"] == "submitted"
    r = client.post(
        f"/segments/{sid2}/translations/confirm",
        json={"target_text": "新译文2-定稿", "source_version_no": 2},
    )
    assert r.json()["status"] == "confirmed"


def test_publish_blocked_until_all_confirmed(client):
    m = create_manuscript(client)
    confirm_all(client, m)
    # 改写第 3 段但不重新确认
    client.put(
        f"/manuscripts/{m['id']}/segments/3/source",
        json={"text": "原文3-改写", "expected_revision": 1},
    )

    r = client.post(
        f"/manuscripts/{m['id']}/publish", json={"expected_revision": 2}
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "blocked_segments"
    assert [b["position"] for b in detail["blocked_segments"]] == [3]

    # 阻塞时不产生定版
    pubs = client.get(f"/manuscripts/{m['id']}/publications").json()
    assert pubs["publications"] == []


def test_publish_revision_conflict_does_not_increment(client):
    m = create_manuscript(client)
    confirm_all(client, m)

    # 工作稿已在 revision=1，但请求带过期的 expected_revision=0 不可能（>=1校验），
    # 先制造一次改写使当前修订号为 2，再用旧号发布。
    client.put(
        f"/manuscripts/{m['id']}/segments/1/source",
        json={"text": "原文1-改写", "expected_revision": 1},
    )
    r = client.post(
        f"/manuscripts/{m['id']}/publish", json={"expected_revision": 1}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "revision_conflict"
    assert client.get(f"/manuscripts/{m['id']}/publications").json()[
        "publications"
    ] == []


def test_publish_creates_snapshot_and_versions(client):
    m = create_manuscript(client)
    confirm_all(client, m)

    r = client.post(
        f"/manuscripts/{m['id']}/publish", json={"expected_revision": 1}
    )
    assert r.status_code == 201
    pub1 = r.json()
    assert pub1["version_no"] == 1
    assert pub1["manuscript_revision"] == 1
    assert [s["source_text"] for s in pub1["segments"]] == [
        "原文1",
        "原文2",
        "原文3",
    ]
    assert [s["target_text"] for s in pub1["segments"]] == [
        "译1",
        "译2",
        "译3",
    ]

    # 按版号读取
    r = client.get(f"/manuscripts/{m['id']}/publications/1")
    assert r.status_code == 200
    assert r.json()["segments"] == pub1["segments"]

    # 改写第 2 段并重新确认后发布 v2
    client.put(
        f"/manuscripts/{m['id']}/segments/2/source",
        json={"text": "原文2-v2", "expected_revision": 1},
    )
    sid2 = next(
        s["segment_id"]
        for s in client.get(f"/manuscripts/{m['id']}").json()["segments"]
        if s["position"] == 2
    )
    client.put(
        f"/segments/{sid2}/translations",
        json={"target_text": "译2-v2", "source_version_no": 2},
    )
    client.post(
        f"/segments/{sid2}/translations/confirm",
        json={"target_text": "译2-v2", "source_version_no": 2},
    )
    r = client.post(
        f"/manuscripts/{m['id']}/publish", json={"expected_revision": 2}
    )
    assert r.status_code == 201
    pub2 = r.json()
    assert pub2["version_no"] == 2
    assert pub2["segments"][1]["source_text"] == "原文2-v2"

    # 旧定版不随后续修改改变
    old = client.get(f"/manuscripts/{m['id']}/publications/1").json()
    assert old["segments"][1]["source_text"] == "原文2"
    assert old["segments"][1]["target_text"] == "译2"
    assert old == pub1


def test_rewrite_unchanged_text_rejected(client):
    m = create_manuscript(client)
    r = client.put(
        f"/manuscripts/{m['id']}/segments/1/source",
        json={"text": "原文1", "expected_revision": 1},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unchanged_source"


def test_rewrite_stale_expected_revision_rejected(client):
    m = create_manuscript(client)
    client.put(
        f"/manuscripts/{m['id']}/segments/1/source",
        json={"text": "原文1-a", "expected_revision": 1},
    )
    r = client.put(
        f"/manuscripts/{m['id']}/segments/2/source",
        json={"text": "原文2-a", "expected_revision": 1},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "revision_conflict"
