"""SQLite 访问层：连接、建表 schema、以及各业务表的行读取/序列化助手。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from server_common import (
    APPLICATION_STATUS_KEYS,
    DEFAULT_CITIES,
    parse_json_str_list,
)


def open_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(path) as database:
        database.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                access_salt TEXT NOT NULL,
                access_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS clicks (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                clicked_at TEXT NOT NULL,
                PRIMARY KEY (user_id, url)
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_user_time ON clicks(user_id, clicked_at);
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                cities TEXT NOT NULL DEFAULT '[]',
                keywords TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id)
            );
            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (user_id, name)
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL,
                collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                account TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, group_id)
            );
            CREATE INDEX IF NOT EXISTS idx_favorites_collection ON favorites(user_id, collection_id);
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                company TEXT NOT NULL,
                job_url TEXT NOT NULL DEFAULT '',
                article_group_id TEXT NOT NULL DEFAULT '',
                article_url TEXT NOT NULL DEFAULT '',
                article_title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'applied',
                history TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(user_id, updated_at);
            """
        )
        database.execute("PRAGMA optimize")


def clicked_urls(database_path: Path, user_id: int) -> dict[str, str]:
    with open_db(database_path) as database:
        rows = database.execute(
            "SELECT url, clicked_at FROM clicks WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row["url"]: row["clicked_at"] for row in rows}


def user_preferences(database_path: Path, user_id: int) -> dict[str, list[str]]:
    """读取用户筛选偏好；从未设置过时返回默认意向城市（无方向关键词）。"""
    with open_db(database_path) as database:
        row = database.execute(
            "SELECT cities, keywords FROM preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return {"cities": list(DEFAULT_CITIES), "keywords": []}
    return {
        "cities": parse_json_str_list(row["cities"]),
        "keywords": parse_json_str_list(row["keywords"]),
    }


def collection_rows(database: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = database.execute(
        "SELECT id, name, created_at FROM collections WHERE user_id = ? ORDER BY created_at, id", (user_id,)
    ).fetchall()
    return [{"id": row["id"], "name": row["name"]} for row in rows]


def favorite_index(database: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """返回 文章组 id -> 收藏分组 id（None 表示未分组） 的索引，供 bootstrap 与前端按钮态使用。"""
    rows = database.execute(
        "SELECT group_id, collection_id FROM favorites WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {row["group_id"]: row["collection_id"] for row in rows}


def favorite_rows(database: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = database.execute(
        """
        SELECT group_id, collection_id, title, url, account, date, created_at
        FROM favorites WHERE user_id = ? ORDER BY created_at DESC, group_id
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def application_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        history = json.loads(row["history"]) if row["history"] else []
    except json.JSONDecodeError:
        history = []
    if not isinstance(history, list):
        history = []
    events = [event for event in history if isinstance(event, dict) and event.get("key") in APPLICATION_STATUS_KEYS]
    return {
        "id": row["id"],
        "company": row["company"],
        "job_url": row["job_url"],
        "article_group_id": row["article_group_id"],
        "article_url": row["article_url"],
        "article_title": row["article_title"],
        "status": row["status"],
        "history": events,
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
