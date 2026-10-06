"""双语稿件定版 HTTP API。

工作流：
  1. POST /manuscripts 以有序原文段创建稿件（revision=1）；
  2. PUT  /manuscripts/{id}/segments/{pos}/source 改写原文（工作稿修订号 +1，
     原已确认译文失效为 pending_review，其他段不受影响）；
  3. PUT  /segments/{id}/translations 提交注明 source_version_no 的译文；
  4. POST /segments/{id}/translations/confirm 确认译文；
     依据过期原文版本的提交/确认一律 409 拒绝；
  5. POST /manuscripts/{id}/publish 携带 expected_revision，
     每段都有匹配“当前原文版本”的 confirmed 译文才原子生成定版；
     否则返回修订冲突或阻塞段，且不增加版号。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .db import get_conn, init_db, write_tx


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Bilingual Manuscript API", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------------- #
class SegmentIn(BaseModel):
    text: str = Field(min_length=1)


class ManuscriptIn(BaseModel):
    title: str = Field(min_length=1)
    segments: list[SegmentIn] = Field(min_length=1)


class SourceRewriteIn(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)


class TranslationIn(BaseModel):
    target_text: str = Field(min_length=1)
    source_version_no: int = Field(ge=1)


class PublishIn(BaseModel):
    expected_revision: int = Field(ge=1)


def conflict(code: str, message: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"message": message})


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #
def _segments_view(conn: sqlite3.Connection, manuscript_id: int) -> list[dict[str, Any]]:
    """组装工作稿：每段的当前原文版本、译文及派生状态。"""
    rows = conn.execute(
        """
        SELECT s.id AS segment_id, s.position,
               sv.version_no AS source_version_no,
               sv.source_text AS source_text,
               t.target_text, t.source_version_no AS translation_source_version_no,
               t.status AS translation_status
        FROM segment s
        JOIN segment_version sv
          ON sv.id = (
              SELECT id FROM segment_version
              WHERE segment_id = s.id ORDER BY version_no DESC LIMIT 1)
        LEFT JOIN translation t ON t.segment_id = s.id
        WHERE s.manuscript_id = ?
        ORDER BY s.position
        """,
        (manuscript_id,),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for r in rows:
        if r["translation_status"] is None:
            seg_status = "empty"
            current_match = False
        elif r["translation_status"] == "confirmed":
            seg_status = "confirmed"
            current_match = r["translation_source_version_no"] == r["source_version_no"]
        elif r["translation_status"] == "submitted":
            seg_status = "submitted"
            current_match = r["translation_source_version_no"] == r["source_version_no"]
        else:  # pending_review：原文已改写，译文依据必然是旧版本
            seg_status = "pending_review"
            current_match = False

        item: dict[str, Any] = {
            "segment_id": r["segment_id"],
            "position": r["position"],
            "source_version_no": r["source_version_no"],
            "source_text": r["source_text"],
            "status": seg_status,
            "translation": None,
        }
        if r["translation_status"] is not None:
            item["translation"] = {
                "target_text": r["target_text"],
                "source_version_no": r["translation_source_version_no"],
                "status": r["translation_status"],
                "matches_current_source": current_match,
            }
        result.append(item)
    return result


def _get_manuscript(conn: sqlite3.Connection, manuscript_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM manuscript WHERE id = ?", (manuscript_id,)
    ).fetchone()
    if row is None:
        raise not_found(f"manuscript {manuscript_id} not found")
    return row


def _get_segment(
    conn: sqlite3.Connection, manuscript_id: int | None, position: int
) -> sqlite3.Row:
    if manuscript_id is None:
        row = conn.execute("SELECT * FROM segment WHERE id = ?", (position,)).fetchone()
        if row is None:
            raise not_found(f"segment {position} not found")
        return row
    row = conn.execute(
        "SELECT * FROM segment WHERE manuscript_id = ? AND position = ?",
        (manuscript_id, position),
    ).fetchone()
    if row is None:
        raise not_found(
            f"segment position {position} of manuscript {manuscript_id} not found"
        )
    return row


def _current_version(conn: sqlite3.Connection, segment_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM segment_version WHERE segment_id = ? "
        "ORDER BY version_no DESC LIMIT 1",
        (segment_id,),
    ).fetchone()


# --------------------------------------------------------------------------- #
# 健康检查 & 稿件
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/manuscripts", status_code=status.HTTP_201_CREATED)
def create_manuscript(
    payload: ManuscriptIn, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO manuscript (title) VALUES (?)", (payload.title,)
        )
        manuscript_id = cur.lastrowid
        for idx, seg in enumerate(payload.segments, start=1):
            cur = conn.execute(
                "INSERT INTO segment (manuscript_id, position) VALUES (?, ?)",
                (manuscript_id, idx),
            )
            segment_id = cur.lastrowid
            conn.execute(
                "INSERT INTO segment_version "
                "(segment_id, version_no, source_text, created_revision) "
                "VALUES (?, 1, ?, 1)",
                (segment_id, seg.text),
            )

    row = _get_manuscript(conn, manuscript_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "segments": _segments_view(conn, manuscript_id),
    }


@app.get("/manuscripts")
def list_manuscripts(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, title, revision, created_at FROM manuscript ORDER BY id"
    ).fetchall()
    return {"manuscripts": [dict(r) for r in rows]}


@app.get("/manuscripts/{manuscript_id}")
def get_manuscript(
    manuscript_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    row = _get_manuscript(conn, manuscript_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "segments": _segments_view(conn, manuscript_id),
    }


# --------------------------------------------------------------------------- #
# 原文改写
# --------------------------------------------------------------------------- #
@app.put("/manuscripts/{manuscript_id}/segments/{position}/source")
def rewrite_source(
    manuscript_id: int,
    position: int,
    payload: SourceRewriteIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    with write_tx(conn) as tx:
        m = _get_manuscript(tx, manuscript_id)
        if m["revision"] != payload.expected_revision:
            raise conflict(
                "revision_conflict",
                "expected_revision does not match current manuscript revision",
                expected_revision=payload.expected_revision,
                current_revision=m["revision"],
            )

        seg = _get_segment(tx, manuscript_id, position)
        cur_ver = _current_version(tx, seg["id"])
        if cur_ver["source_text"] == payload.text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "unchanged_source",
                    "message": "new source text is identical to current version",
                },
            )

        new_version_no = cur_ver["version_no"] + 1
        new_revision = m["revision"] + 1
        tx.execute(
            "INSERT INTO segment_version "
            "(segment_id, version_no, source_text, created_revision) "
            "VALUES (?, ?, ?, ?)",
            (seg["id"], new_version_no, payload.text, new_revision),
        )
        # 只影响本段：已确认译文失效，标注待复核；提交态译文同样不再匹配当前版本。
        tx.execute(
            "UPDATE translation SET status = 'pending_review', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE segment_id = ? AND status = 'confirmed'",
            (seg["id"],),
        )
        tx.execute(
            "UPDATE manuscript SET revision = ? WHERE id = ?",
            (new_revision, manuscript_id),
        )

    return get_manuscript(manuscript_id, conn=conn)


# --------------------------------------------------------------------------- #
# 译文提交 / 确认
# --------------------------------------------------------------------------- #
@app.put("/segments/{segment_id}/translations")
def submit_translation(
    segment_id: int,
    payload: TranslationIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    with write_tx(conn) as tx:
        seg = tx.execute(
            "SELECT * FROM segment WHERE id = ?", (segment_id,)
        ).fetchone()
        if seg is None:
            raise not_found(f"segment {segment_id} not found")
        cur_ver = _current_version(tx, segment_id)
        if payload.source_version_no != cur_ver["version_no"]:
            raise conflict(
                "stale_source_version",
                "translation must be based on the current source version",
                submitted_source_version_no=payload.source_version_no,
                current_source_version_no=cur_ver["version_no"],
            )

        existing = tx.execute(
            "SELECT id FROM translation WHERE segment_id = ?", (segment_id,)
        ).fetchone()
        if existing is None:
            tx.execute(
                "INSERT INTO translation "
                "(segment_id, target_text, source_version_no, status) "
                "VALUES (?, ?, ?, 'submitted')",
                (segment_id, payload.target_text, payload.source_version_no),
            )
        else:
            # 重新提交即覆盖旧译文并回到 submitted（pending_review 也由此解除）。
            tx.execute(
                "UPDATE translation SET target_text = ?, source_version_no = ?, "
                "status = 'submitted', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE segment_id = ?",
                (payload.target_text, payload.source_version_no, segment_id),
            )

    return _translation_view(conn, segment_id)


@app.post("/segments/{segment_id}/translations/confirm")
def confirm_translation(
    segment_id: int,
    payload: TranslationIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    with write_tx(conn) as tx:
        seg = tx.execute(
            "SELECT * FROM segment WHERE id = ?", (segment_id,)
        ).fetchone()
        if seg is None:
            raise not_found(f"segment {segment_id} not found")
        cur_ver = _current_version(tx, segment_id)
        if payload.source_version_no != cur_ver["version_no"]:
            raise conflict(
                "stale_source_version",
                "cannot confirm a translation based on an outdated source version",
                submitted_source_version_no=payload.source_version_no,
                current_source_version_no=cur_ver["version_no"],
            )
        t = tx.execute(
            "SELECT * FROM translation WHERE segment_id = ? AND source_version_no = ?",
            (segment_id, payload.source_version_no),
        ).fetchone()
        if t is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "translation_not_found",
                    "message": "submit a translation for this source version first",
                },
            )

        tx.execute(
            "UPDATE translation SET target_text = ?, status = 'confirmed', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE segment_id = ?",
            (payload.target_text, segment_id),
        )

    return _translation_view(conn, segment_id)


def _translation_view(conn: sqlite3.Connection, segment_id: int) -> dict[str, Any]:
    r = conn.execute(
        """
        SELECT t.target_text, t.source_version_no, t.status, t.updated_at,
               sv.version_no AS current_version_no
        FROM translation t
        JOIN segment s ON s.id = t.segment_id
        JOIN segment_version sv ON sv.segment_id = s.id
         AND sv.version_no = (
             SELECT MAX(version_no) FROM segment_version WHERE segment_id = s.id)
        WHERE t.segment_id = ?
        """,
        (segment_id,),
    ).fetchone()
    return {
        "segment_id": segment_id,
        "target_text": r["target_text"],
        "source_version_no": r["source_version_no"],
        "status": r["status"],
        "matches_current_source": r["source_version_no"] == r["current_version_no"],
        "updated_at": r["updated_at"],
    }


# --------------------------------------------------------------------------- #
# 定版发布
# --------------------------------------------------------------------------- #
@app.post("/manuscripts/{manuscript_id}/publish", status_code=status.HTTP_201_CREATED)
def publish(
    manuscript_id: int,
    payload: PublishIn,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    with write_tx(conn) as tx:
        m = _get_manuscript(tx, manuscript_id)
        current_revision = m["revision"]
        if payload.expected_revision != current_revision:
            raise conflict(
                "revision_conflict",
                "expected_revision does not match current manuscript revision; "
                "re-fetch the working copy before publishing",
                expected_revision=payload.expected_revision,
                current_revision=current_revision,
            )

        segments = _segments_view(tx, manuscript_id)
        blocked = [
            {
                "position": s["position"],
                "segment_id": s["segment_id"],
                "status": s["status"],
                "current_source_version_no": s["source_version_no"],
                "translation_source_version_no": (
                    s["translation"]["source_version_no"] if s["translation"] else None
                ),
            }
            for s in segments
            if not (
                s["translation"]
                and s["translation"]["status"] == "confirmed"
                and s["translation"]["matches_current_source"]
            )
        ]
        if blocked:
            # 未全部满足：事务回滚，不增加任何版号。
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "blocked_segments",
                    "message": "every segment needs a confirmed translation "
                    "matching its current source version",
                    "current_revision": current_revision,
                    "blocked_segments": blocked,
                },
            )

        next_version = (
            tx.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS v "
                "FROM publication WHERE manuscript_id = ?",
                (manuscript_id,),
            ).fetchone()["v"]
        )

        cur = tx.execute(
            "INSERT INTO publication "
            "(manuscript_id, version_no, manuscript_revision, title) "
            "VALUES (?, ?, ?, ?)",
            (manuscript_id, next_version, current_revision, m["title"]),
        )
        publication_id = cur.lastrowid
        tx.executemany(
            "INSERT INTO publication_segment "
            "(publication_id, position, source_text, target_text) VALUES (?, ?, ?, ?)",
            [
                (
                    publication_id,
                    s["position"],
                    s["source_text"],
                    s["translation"]["target_text"],
                )
                for s in segments
            ],
        )

        snapshot = _publication_dict(tx, publication_id)

    return snapshot


def _publication_dict(conn: sqlite3.Connection, publication_id: int) -> dict[str, Any]:
    p = conn.execute(
        "SELECT * FROM publication WHERE id = ?", (publication_id,)
    ).fetchone()
    items = conn.execute(
        "SELECT position, source_text, target_text FROM publication_segment "
        "WHERE publication_id = ? ORDER BY position",
        (publication_id,),
    ).fetchall()
    return {
        "id": p["id"],
        "manuscript_id": p["manuscript_id"],
        "version_no": p["version_no"],
        "manuscript_revision": p["manuscript_revision"],
        "title": p["title"],
        "created_at": p["created_at"],
        "segments": [dict(i) for i in items],
    }


@app.get("/manuscripts/{manuscript_id}/publications")
def list_publications(
    manuscript_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    _get_manuscript(conn, manuscript_id)
    rows = conn.execute(
        "SELECT id, manuscript_id, version_no, manuscript_revision, title, created_at "
        "FROM publication WHERE manuscript_id = ? ORDER BY version_no",
        (manuscript_id,),
    ).fetchall()
    return {"publications": [dict(r) for r in rows]}


@app.get("/manuscripts/{manuscript_id}/publications/{version_no}")
def get_publication(
    manuscript_id: int,
    version_no: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    _get_manuscript(conn, manuscript_id)
    p = conn.execute(
        "SELECT id FROM publication WHERE manuscript_id = ? AND version_no = ?",
        (manuscript_id, version_no),
    ).fetchone()
    if p is None:
        raise not_found(
            f"publication version {version_no} of manuscript {manuscript_id} not found"
        )
    return _publication_dict(conn, p["id"])
