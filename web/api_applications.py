"""投递管理 API：公司记录的增删改、阶段推进（含笔试轮次）与进度撤销。"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

from database import application_to_dict, open_db
from server_common import (
    APPLICATION_STATUS_KEYS,
    GROUP_ID_RE,
    MAX_COMPANY_LENGTH,
    MAX_NOTE_LENGTH,
    MAX_STATUS_NOTE_LENGTH,
    MAX_URL_LENGTH,
    body_int,
    clean_web_url,
    iso_now,
)


class ApiApplicationsMixin:
    """挂在 CoreHandler 之上的投递管理端点。"""

    def handle_applications(self) -> None:
        user = self.require_user()
        if user is None:
            return
        with open_db(self.server.config.database) as database:
            rows = database.execute(
                "SELECT * FROM applications WHERE user_id = ? ORDER BY updated_at DESC, id DESC", (user["id"],)
            ).fetchall()
        self.send_json({"applications": [application_to_dict(row) for row in rows]})

    def handle_application_add(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json(max_bytes=32_768)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        company = str(body.get("company", "")).strip()
        if not (1 <= len(company) <= MAX_COMPANY_LENGTH):
            self.send_json({"error": f"公司名称需为 1-{MAX_COMPANY_LENGTH} 个字符"}, HTTPStatus.BAD_REQUEST)
            return
        job_url = clean_web_url(body.get("job_url"))
        if job_url is None:
            self.send_json({"error": "招聘链接应为 http(s):// 或 mailto: 地址"}, HTTPStatus.BAD_REQUEST)
            return
        status = body.get("status", "applied")
        if not isinstance(status, str) or status not in APPLICATION_STATUS_KEYS:
            self.send_json({"error": "无效的投递状态"}, HTTPStatus.BAD_REQUEST)
            return
        note = str(body.get("note", "")).strip()[:MAX_NOTE_LENGTH]
        status_note = str(body.get("status_note", "")).strip()[:MAX_STATUS_NOTE_LENGTH]
        article_group_id = str(body.get("article_group_id", "")).strip()
        if article_group_id and not GROUP_ID_RE.fullmatch(article_group_id):
            self.send_json({"error": "无效的文章组标识"}, HTTPStatus.BAD_REQUEST)
            return
        article_url = str(body.get("article_url", "")).strip()[:MAX_URL_LENGTH]
        article_title = str(body.get("article_title", "")).strip()[:200]
        if article_group_id:
            try:
                _, groups_by_id = self.server.articles.load()
            except Exception as error:
                self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            group = groups_by_id.get(article_group_id)
            if group:
                # 以服务端当前分组数据为准写快照，避免客户端传旧标题
                article_url = group["url"]
                article_title = group["title"]
        if article_url:
            checked = clean_web_url(article_url)
            if checked is None:
                self.send_json({"error": "文章链接格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            article_url = checked
        stamp = iso_now()
        event = {"key": status, "at": stamp}
        if status_note:
            event["note"] = status_note
        with open_db(self.server.config.database) as database:
            cursor = database.execute(
                """
                INSERT INTO applications(user_id, company, job_url, article_group_id, article_url,
                                         article_title, status, history, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], company, job_url, article_group_id, article_url,
                 article_title, status, json.dumps([event], ensure_ascii=False), note, stamp, stamp),
            )
            row = database.execute("SELECT * FROM applications WHERE id = ?", (cursor.lastrowid,)).fetchone()
        self.send_json({"ok": True, "application": application_to_dict(row)})

    def handle_application_update(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        app_id = body_int(body, "id")
        if app_id is None:
            self.send_json({"error": "无效的记录标识"}, HTTPStatus.BAD_REQUEST)
            return
        with open_db(self.server.config.database) as database:
            row = database.execute(
                "SELECT * FROM applications WHERE id = ? AND user_id = ?", (app_id, user["id"])
            ).fetchone()
            if row is None:
                self.send_json({"error": "投递记录不存在"}, HTTPStatus.NOT_FOUND)
                return
            updates: dict[str, Any] = {}
            history = application_to_dict(row)["history"]
            if body.get("undo"):
                # 撤销最后一条进度；撤空后回到「待投递」
                if history:
                    history.pop()
                    updates["status"] = history[-1]["key"] if history else "planned"
            else:
                if "company" in body:
                    company = str(body.get("company", "")).strip()
                    if not (1 <= len(company) <= MAX_COMPANY_LENGTH):
                        self.send_json({"error": f"公司名称需为 1-{MAX_COMPANY_LENGTH} 个字符"}, HTTPStatus.BAD_REQUEST)
                        return
                    updates["company"] = company
                if "job_url" in body:
                    job_url = clean_web_url(body.get("job_url"))
                    if job_url is None:
                        self.send_json({"error": "招聘链接应为 http(s):// 或 mailto: 地址"}, HTTPStatus.BAD_REQUEST)
                        return
                    updates["job_url"] = job_url
                if "note" in body:
                    updates["note"] = str(body.get("note", "")).strip()[:MAX_NOTE_LENGTH]
                if "status" in body:
                    status = body.get("status")
                    if not isinstance(status, str) or status not in APPLICATION_STATUS_KEYS:
                        self.send_json({"error": "无效的投递状态"}, HTTPStatus.BAD_REQUEST)
                        return
                    # 同状态重复点「笔试」= 记下一轮笔试；其余同状态点击不产生新事件
                    if status != row["status"] or status == "test":
                        event = {"key": status, "at": iso_now()}
                        status_note = str(body.get("status_note", "")).strip()[:MAX_STATUS_NOTE_LENGTH]
                        if status_note:
                            event["note"] = status_note
                        history.append(event)
                    updates["status"] = status
            updates["history"] = json.dumps(history, ensure_ascii=False)
            updates["updated_at"] = iso_now()
            assignments = ", ".join(f"{column} = ?" for column in updates)
            database.execute(
                f"UPDATE applications SET {assignments} WHERE id = ? AND user_id = ?",
                [*updates.values(), app_id, user["id"]],
            )
            row = database.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        self.send_json({"ok": True, "application": application_to_dict(row)})

    def handle_application_delete(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        app_id = body_int(body, "id")
        if app_id is None:
            self.send_json({"error": "无效的记录标识"}, HTTPStatus.BAD_REQUEST)
            return
        with open_db(self.server.config.database) as database:
            cursor = database.execute(
                "DELETE FROM applications WHERE id = ? AND user_id = ?", (app_id, user["id"])
            )
            if cursor.rowcount == 0:
                self.send_json({"error": "投递记录不存在"}, HTTPStatus.NOT_FOUND)
                return
        self.send_json({"ok": True})
