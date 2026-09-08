"""收藏 API：收藏/取消/移动分组，以及收藏分组的增删改名。"""

from __future__ import annotations

import json
import sqlite3
from http import HTTPStatus
from typing import Any

from database import (
    collection_rows,
    favorite_index,
    favorite_rows,
    open_db,
)
from server_common import (
    GROUP_ID_RE,
    MAX_COLLECTION_NAME,
    MAX_COLLECTIONS,
    body_int,
    iso_now,
)


def favorites_payload(database: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    return {
        "collections": collection_rows(database, user_id),
        "items": favorite_rows(database, user_id),
        "favorites": favorite_index(database, user_id),
    }


class ApiFavoritesMixin:
    """挂在 CoreHandler 之上的收藏端点。"""

    def handle_favorites(self) -> None:
        user = self.require_user()
        if user is None:
            return
        with open_db(self.server.config.database) as database:
            payload = favorites_payload(database, user["id"])
        self.send_json(payload)

    def handle_set_favorite(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        group_id = str(body.get("group_id", ""))
        favorited = body.get("favorited")
        collection_id = body.get("collection_id")
        if not GROUP_ID_RE.fullmatch(group_id) or not isinstance(favorited, bool):
            self.send_json({"error": "无效的收藏请求"}, HTTPStatus.BAD_REQUEST)
            return
        if favorited:
            try:
                _, groups_by_id = self.server.articles.load()
            except Exception as error:
                self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            group = groups_by_id.get(group_id)
            if not group:
                self.send_json({"error": "文章组已更新，请刷新页面"}, HTTPStatus.CONFLICT)
                return
            if collection_id is not None and (isinstance(collection_id, bool) or not isinstance(collection_id, int)):
                self.send_json({"error": "无效的收藏分组"}, HTTPStatus.BAD_REQUEST)
                return
            with open_db(self.server.config.database) as database:
                if collection_id is not None:
                    owned = database.execute(
                        "SELECT 1 FROM collections WHERE id = ? AND user_id = ?", (collection_id, user["id"])
                    ).fetchone()
                    if not owned:
                        self.send_json({"error": "收藏分组不存在"}, HTTPStatus.NOT_FOUND)
                        return
                # 收藏时快照标题/链接等字段：之后抓取数据更新甚至文章下线，收藏列表仍可打开原文
                database.execute(
                    """
                    INSERT INTO favorites(user_id, group_id, collection_id, title, url, account, date, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, group_id) DO UPDATE SET
                      collection_id = excluded.collection_id,
                      title = excluded.title, url = excluded.url,
                      account = excluded.account, date = excluded.date,
                      created_at = excluded.created_at
                    """,
                    (user["id"], group_id, collection_id, group["title"], group["url"],
                     group["account"], group["date"], iso_now()),
                )
        else:
            with open_db(self.server.config.database) as database:
                database.execute(
                    "DELETE FROM favorites WHERE user_id = ? AND group_id = ?", (user["id"], group_id)
                )
        with open_db(self.server.config.database) as database:
            payload = {"ok": True, **favorites_payload(database, user["id"])}
        self.send_json(payload)

    def handle_collections(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        action = str(body.get("action", ""))
        user_id = user["id"]
        with open_db(self.server.config.database) as database:
            if action == "create":
                name = str(body.get("name", "")).strip()
                if not (1 <= len(name) <= MAX_COLLECTION_NAME):
                    self.send_json({"error": f"分组名称需为 1-{MAX_COLLECTION_NAME} 个字符"}, HTTPStatus.BAD_REQUEST)
                    return
                count = database.execute(
                    "SELECT count(*) FROM collections WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
                if count >= MAX_COLLECTIONS:
                    self.send_json({"error": f"分组数量已达上限（{MAX_COLLECTIONS} 个）"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    database.execute(
                        "INSERT INTO collections(user_id, name, created_at) VALUES (?, ?, ?)",
                        (user_id, name, iso_now()),
                    )
                except sqlite3.IntegrityError:
                    self.send_json({"error": "已存在同名分组"}, HTTPStatus.CONFLICT)
                    return
            elif action == "rename":
                collection_id = body_int(body, "id")
                name = str(body.get("name", "")).strip()
                if collection_id is None or not (1 <= len(name) <= MAX_COLLECTION_NAME):
                    self.send_json({"error": "无效的重命名请求"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    cursor = database.execute(
                        "UPDATE collections SET name = ? WHERE id = ? AND user_id = ?",
                        (name, collection_id, user_id),
                    )
                except sqlite3.IntegrityError:
                    self.send_json({"error": "已存在同名分组"}, HTTPStatus.CONFLICT)
                    return
                if cursor.rowcount == 0:
                    self.send_json({"error": "收藏分组不存在"}, HTTPStatus.NOT_FOUND)
                    return
            elif action == "delete":
                collection_id = body_int(body, "id")
                if collection_id is None:
                    self.send_json({"error": "无效的删除请求"}, HTTPStatus.BAD_REQUEST)
                    return
                cursor = database.execute(
                    "DELETE FROM collections WHERE id = ? AND user_id = ?", (collection_id, user_id)
                )
                if cursor.rowcount == 0:
                    self.send_json({"error": "收藏分组不存在"}, HTTPStatus.NOT_FOUND)
                    return
                # 组内收藏通过外键 ON DELETE SET NULL 自动回到「未分组」，不会被删除
            else:
                self.send_json({"error": "未知的分组操作"}, HTTPStatus.BAD_REQUEST)
                return
            payload = {"ok": True, **favorites_payload(database, user_id)}
        self.send_json(payload)
