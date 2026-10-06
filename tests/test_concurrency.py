"""原文改写与定版发布并发时的数据一致性测试。

启动真实 uvicorn 服务：
- 4 个改写线程，各自负责一段，反复“改写原文 -> 重新提交 -> 确认”；
- 1 个发布线程，持续按当时工作稿修订号尝试发布；
校验：
1. 任何请求都不出现 5xx；
2. 每个成功生成的定版，其每段 (原文, 译文) 必须是曾经确认过的合法配对，
   即定版不会混入不同工作稿修订的数据；
3. 旧定版在后续改写/复核/再发布后内容不变。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROUNDS = 6
N_SEGMENTS = 4


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "concurrency.db"))
    import uvicorn

    # 子线程内构建 app，确保读取到上面的 DB_PATH。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    port = _free_port()
    config = uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=2).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        raise RuntimeError("server failed to start")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


_errors: list[str] = []
_errors_lock = threading.Lock()


def _record_error(resp: httpx.Response) -> None:
    if resp.status_code >= 500:
        with _errors_lock:
            _errors.append(f"{resp.status_code}: {resp.text}")


def test_rewrite_and_publish_concurrency(live_server):
    base = live_server
    # 已确认的合法 (position, source_text) -> target_text 配对
    confirmed_pairs: dict[tuple[int, str], str] = {}
    pairs_lock = threading.Lock()
    # 成功生成的全部定版（含准备阶段的 v1）
    published: list[dict] = []

    def c() -> httpx.Client:
        return httpx.Client(base_url=base, timeout=15)

    # ---- 初始稿件并全部确认、发布 v1 ----
    with c() as client:
        r = client.post(
            "/manuscripts",
            json={
                "title": "并发测试",
                "segments": [{"text": f"原文{p}"} for p in range(1, N_SEGMENTS + 1)],
            },
        )
        _record_error(r)
        assert r.status_code == 201
        mid = r.json()["id"]

        # segment id 即创建顺序 1..N
        for p in range(1, N_SEGMENTS + 1):
            sid = p
            t = f"译{p}-初"
            rr = client.put(
                f"/segments/{sid}/translations",
                json={"target_text": t, "source_version_no": 1},
            )
            _record_error(rr)
            rr = client.post(
                f"/segments/{sid}/translations/confirm",
                json={"target_text": t, "source_version_no": 1},
            )
            _record_error(rr)
            assert rr.status_code == 200
            with pairs_lock:
                confirmed_pairs[(p, f"原文{p}")] = t

        r = client.post(f"/manuscripts/{mid}/publish", json={"expected_revision": 1})
        _record_error(r)
        assert r.status_code == 201
        assert r.json()["version_no"] == 1
        published.append(r.json())

    # ---- 改写线程 ----
    def writer(position: int) -> None:
        sid = position
        with c() as client:
            for round_no in range(1, ROUNDS + 1):
                while True:
                    r = client.get(f"/manuscripts/{mid}")
                    _record_error(r)
                    if r.status_code != 200:
                        time.sleep(0.05)
                        continue
                    copy = r.json()
                    seg = next(s for s in copy["segments"] if s["position"] == position)
                    new_text = f"原文{position}-r{round_no}"
                    if seg["source_text"] == new_text:
                        break
                    r = client.put(
                        f"/manuscripts/{mid}/segments/{position}/source",
                        json={
                            "text": new_text,
                            "expected_revision": copy["revision"],
                        },
                    )
                    _record_error(r)
                    if r.status_code == 409:
                        # 其他线程刚改写过，刷新修订号重试。
                        continue
                    assert r.status_code == 200, r.text
                    break

                # 重新提交并确认，依据新原文版本
                while True:
                    r = client.get(f"/manuscripts/{mid}")
                    copy = r.json()
                    seg = next(s for s in copy["segments"] if s["position"] == position)
                    ver = seg["source_version_no"]
                    t = f"译{position}-r{round_no}-v{ver}"
                    r = client.put(
                        f"/segments/{sid}/translations",
                        json={"target_text": t, "source_version_no": ver},
                    )
                    _record_error(r)
                    if r.status_code == 409:
                        # 原文又被自己的下一轮之外的情况改变：本段只有本线程改写，
                        # 此情况不应发生，保险起见刷新重试。
                        continue
                    assert r.status_code == 200, r.text
                    r = client.post(
                        f"/segments/{sid}/translations/confirm",
                        json={"target_text": t, "source_version_no": ver},
                    )
                    _record_error(r)
                    if r.status_code == 409:
                        continue
                    assert r.status_code == 200, r.text
                    with pairs_lock:
                        confirmed_pairs[(position, new_text)] = t
                    break

    # ---- 发布线程 ----
    publish_stop = threading.Event()

    def publisher() -> None:
        with c() as client:
            while not publish_stop.is_set():
                r = client.get(f"/manuscripts/{mid}")
                _record_error(r)
                rev = r.json()["revision"]
                r = client.post(
                    f"/manuscripts/{mid}/publish",
                    json={"expected_revision": rev},
                )
                _record_error(r)
                assert r.status_code in (201, 409, 422), r.text
                if r.status_code == 201:
                    published.append(r.json())
                time.sleep(0.02)

    with ThreadPoolExecutor(max_workers=N_SEGMENTS + 1) as pool:
        futures = [pool.submit(writer, p) for p in range(1, N_SEGMENTS + 1)]
        pub_future = pool.submit(publisher)
        for f in futures:
            f.result()
        publish_stop.set()
        pub_future.result()

    # ---- 收敛：在最终修订号上再发一次，必然成功 ----
    with c() as client:
        copy = client.get(f"/manuscripts/{mid}").json()
        for seg in copy["segments"]:
            if not (
                seg["translation"]
                and seg["translation"]["status"] == "confirmed"
                and seg["translation"]["matches_current_source"]
            ):
                ver = seg["source_version_no"]
                t = f"译{seg['position']}-终"
                client.put(
                    f"/segments/{seg['segment_id']}/translations",
                    json={"target_text": t, "source_version_no": ver},
                )
                client.post(
                    f"/segments/{seg['segment_id']}/translations/confirm",
                    json={"target_text": t, "source_version_no": ver},
                )
                with pairs_lock:
                    confirmed_pairs[(seg["position"], seg["source_text"])] = t
        r = client.post(
            f"/manuscripts/{mid}/publish",
            json={"expected_revision": copy["revision"]},
        )
        assert r.status_code == 201, r.text
        published.append(r.json())

    # ---- 全程无 5xx ----
    assert not _errors, _errors[:3]

    # ---- 每个定版都是合法、自洽的快照：不混版 ----
    assert len(published) >= 2
    last_revision = 0
    for pub in published:
        assert len(pub["segments"]) == N_SEGMENTS
        assert pub["manuscript_revision"] >= last_revision
        last_revision = pub["manuscript_revision"]
        for item in pub["segments"]:
            key = (item["position"], item["source_text"])
            # 译文必须恰好是“该原文版本”被确认过的译文
            assert item["target_text"] == confirmed_pairs[key], (
                f"mixed revision in publication v{pub['version_no']} "
                f"position {item['position']}: {item}"
            )

    # ---- 版号连续，旧定版不可变 ----
    with c() as client:
        versions = [p["version_no"] for p in published]
        assert versions == sorted(set(versions))
        for pub in published:
            got = client.get(
                f"/manuscripts/{mid}/publications/{pub['version_no']}"
            ).json()
            assert got["segments"] == pub["segments"]
            assert got["manuscript_revision"] == pub["manuscript_revision"]

        listing = client.get(f"/manuscripts/{mid}/publications").json()[
            "publications"
        ]
        assert [p["version_no"] for p in listing] == list(range(1, len(listing) + 1))
