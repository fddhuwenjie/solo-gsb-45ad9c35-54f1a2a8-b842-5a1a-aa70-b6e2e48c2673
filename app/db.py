"""SQLite 连接与初始化。

所有写操作都在显式事务中执行（BEGIN IMMEDIATE），配合
``busy_timeout`` 与 WAL，保证“原文改写”和“定版发布”并发时
不会混入不同工作稿修订号的数据。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS manuscript (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS segment (
    id            INTEGER PRIMARY KEY,
    manuscript_id INTEGER NOT NULL REFERENCES manuscript(id),
    position      INTEGER NOT NULL,
    UNIQUE (manuscript_id, position)
);

-- 原文版本：每改写一次原文段，就新增一行，旧版本永久保留，
-- 已发布定版通过它引用不可变的历史原文。
CREATE TABLE IF NOT EXISTS segment_version (
    id               INTEGER PRIMARY KEY,
    segment_id       INTEGER NOT NULL REFERENCES segment(id),
    version_no       INTEGER NOT NULL,
    source_text      TEXT NOT NULL,
    created_revision INTEGER NOT NULL,  -- 产生该版本时的工作稿修订号
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (segment_id, version_no)
);

-- 每段的译文只保留一行，按“所依据的原文版本”维护：
-- 提交新版本译文时整体改写，confirmed 译文随原文改写被置为 pending_review。
CREATE TABLE IF NOT EXISTS translation (
    id                  INTEGER PRIMARY KEY,
    segment_id          INTEGER NOT NULL UNIQUE REFERENCES segment(id),
    target_text         TEXT NOT NULL,
    source_version_no   INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('submitted', 'confirmed', 'pending_review')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS publication (
    id               INTEGER PRIMARY KEY,
    manuscript_id    INTEGER NOT NULL REFERENCES manuscript(id),
    version_no       INTEGER NOT NULL,
    manuscript_revision INTEGER NOT NULL,  -- 定版所依据的工作稿修订号（快照）
    title            TEXT NOT NULL,        -- 标题快照
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (manuscript_id, version_no)
);

-- 定版条目是整篇双语文本的不可变快照，与后续改写/复核完全解耦。
CREATE TABLE IF NOT EXISTS publication_segment (
    id             INTEGER PRIMARY KEY,
    publication_id INTEGER NOT NULL REFERENCES publication(id),
    position       INTEGER NOT NULL,
    source_text    TEXT NOT NULL,
    target_text    TEXT NOT NULL,
    UNIQUE (publication_id, position)
);

CREATE INDEX IF NOT EXISTS idx_segment_manuscript ON segment(manuscript_id);
CREATE INDEX IF NOT EXISTS idx_version_segment ON segment_version(segment_id);
CREATE INDEX IF NOT EXISTS idx_pub_manuscript ON publication(manuscript_id);
"""


def get_db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 的同步依赖生成器可能在线程池的不同
    # 工作线程中执行“进入”和“退出”部分；连接本身每请求独立、不跨请求共享，
    # 写操作再由 BEGIN IMMEDIATE 串行化，因此这样使用是安全的。
    conn = sqlite3.connect(
        db_path or get_db_path(), timeout=10.0, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即获取写锁的事务，提交成功才可见，异常一律回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每个请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
