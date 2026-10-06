import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)

    # 确保 app 模块在设置 DB_PATH 之后初始化。
    import importlib

    import app.db as db_module
    import app.main as main_module

    importlib.reload(db_module)
    importlib.reload(main_module)
    db_module.init_db(db_path)

    with TestClient(main_module.app) as c:
        yield c


def create_manuscript(client, n=3, title="示例"):
    resp = client.post(
        "/manuscripts",
        json={
            "title": title,
            "segments": [{"text": f"原文{i}"} for i in range(1, n + 1)],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def confirm_all(client, manuscript, target_prefix="译"):
    for seg in manuscript["segments"]:
        resp = client.post(
            f"/segments/{seg['segment_id']}/translations/confirm",
            json={
                "target_text": f"{target_prefix}{seg['position']}",
                "source_version_no": seg["source_version_no"],
            },
        )
        # 可能尚未提交：先提交再确认。
        if resp.status_code == 422:
            client.put(
                f"/segments/{seg['segment_id']}/translations",
                json={
                    "target_text": f"{target_prefix}{seg['position']}",
                    "source_version_no": seg["source_version_no"],
                },
            )
            resp = client.post(
                f"/segments/{seg['segment_id']}/translations/confirm",
                json={
                    "target_text": f"{target_prefix}{seg['position']}",
                    "source_version_no": seg["source_version_no"],
                },
            )
        assert resp.status_code == 200, resp.text
