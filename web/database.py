"""SQLite 访问层：连接、建表 schema、以及各业务表的行读取/序列化助手。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from server_common import (
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
                pinned_provinces TEXT NOT NULL DEFAULT '["北京"]',
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
                -- 0 未标记 / 1 已投递 / 2 不投递（互斥，单列存三态；历史遗留，
                -- 现役的分组级标记在 group_states 表，此列仅作迁移来源）
                applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1, 2)),
                apply_url TEXT NOT NULL DEFAULT '',
                -- 收藏时快照的 AI 提取信息（unit/positions 等的 JSON）：
                -- 文章移出看板后收藏页仍能展示完整岗位信息
                screen_snapshot TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, group_id)
            );
            CREATE INDEX IF NOT EXISTS idx_favorites_collection ON favorites(user_id, collection_id);
            CREATE TABLE IF NOT EXISTS group_states (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                group_id TEXT NOT NULL,
                -- 分组级投递意向（与收藏解耦）：0 未标记 / 1 已投递 / 2 不投递
                applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1, 2)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, group_id)
            );
            """
        )
        # 旧库迁移：preferences 建表后新增过 pinned_provinces 列；favorites 建表后
        # 新增过 applied（已投递标记）与 apply_url（报名链接快照）列。
        # 重复执行会报"列已存在"，忽略即可
        for migration in (
            "ALTER TABLE preferences ADD COLUMN pinned_provinces TEXT NOT NULL DEFAULT '[\"北京\"]'",
            "ALTER TABLE favorites ADD COLUMN applied INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE favorites ADD COLUMN apply_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE favorites ADD COLUMN screen_snapshot TEXT NOT NULL DEFAULT ''",
        ):
            try:
                database.execute(migration)
            except sqlite3.OperationalError:
                pass
        # 一次性迁移：applied（已投递/不投递）原先挂在收藏行上，改为分组级统一状态；
        # OR IGNORE 保证 group_states 里已有的新标记不被旧收藏值覆盖
        database.execute(
            """
            INSERT OR IGNORE INTO group_states(user_id, group_id, applied, updated_at)
            SELECT user_id, group_id, applied, created_at FROM favorites WHERE applied > 0
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
    """读取用户筛选偏好；从未设置过时返回默认意向城市（无方向关键词）。
    pinned_provinces 列是宣讲会页"置顶省份"时期的遗留，现已改省份筛选，不再读写。"""
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
        SELECT group_id, collection_id, title, url, account, date, created_at, apply_url, screen_snapshot
        FROM favorites WHERE user_id = ? ORDER BY created_at DESC, group_id
        """,
        (user_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["screen_snapshot"] = json.loads(row["screen_snapshot"]) if row["screen_snapshot"] else None
        except json.JSONDecodeError:
            item["screen_snapshot"] = None
        items.append(item)
    return items


def group_state_index(database: sqlite3.Connection, user_id: int) -> dict[str, int]:
    """分组级投递意向索引：文章组 id -> applied（1 已投递 / 2 不投递），未标记的不出现。"""
    rows = database.execute(
        "SELECT group_id, applied FROM group_states WHERE user_id = ? AND applied > 0", (user_id,)
    ).fetchall()
    return {row["group_id"]: row["applied"] for row in rows}
