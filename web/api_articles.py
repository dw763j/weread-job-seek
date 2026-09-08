"""文章看板 API：bootstrap 聚合、已读状态、筛选偏好、AI 筛选任务触发与进度。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from database import (
    clicked_urls,
    collection_rows,
    favorite_index,
    open_db,
    user_preferences,
)
from server_common import (
    DATE_RE,
    HERE,
    MAX_CITIES,
    MAX_CITY_LENGTH,
    MAX_KEYWORDS,
    MAX_KEYWORD_LENGTH,
    iso_now,
    normalize_str_list,
)


class ApiArticlesMixin:
    """挂在 CoreHandler 之上的文章看板端点。"""

    def handle_bootstrap(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            payload, _ = self.server.articles.load()
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        clicks = clicked_urls(self.server.config.database, user["id"])
        screen_index = self.server.screens.load()
        with open_db(self.server.config.database) as database:
            collections = collection_rows(database, user["id"])
            favorites = favorite_index(database, user["id"])
        groups = []
        for group in payload["groups"]:
            item = {key: value for key, value in group.items() if key != "urls"}
            stamps = [clicks[url] for url in group["urls"] if url in clicks]
            item["clicked_at"] = max(stamps) if stamps else None
            item["screen"] = next((screen_index[url] for url in group["urls"] if url in screen_index), None)
            groups.append(item)
        self.send_json(
            {
                "user": {"username": user["username"], "display_name": user["display_name"]},
                "generated_at": payload["generated_at"],
                "range": payload["range"],
                "source_rows": payload["source_rows"],
                "preferences": user_preferences(self.server.config.database, user["id"]),
                "collections": collections,
                "favorites": favorites,
                "groups": groups,
            }
        )

    def handle_get_clicks(self) -> None:
        user = self.require_user()
        if user is None:
            return
        _, groups_by_id = self.server.articles.load()
        clicks = clicked_urls(self.server.config.database, user["id"])
        states = {}
        for group_id, group in groups_by_id.items():
            stamps = [clicks[url] for url in group["urls"] if url in clicks]
            if stamps:
                states[group_id] = max(stamps)
        self.send_json({"states": states, "updated_at": iso_now()})

    def handle_set_click(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        group_id = str(body.get("group_id", ""))
        clicked = body.get("clicked")
        if not re.fullmatch(r"[0-9a-f]{24}", group_id) or not isinstance(clicked, bool):
            self.send_json({"error": "无效的点击状态"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            _, groups_by_id = self.server.articles.load()
        except Exception as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        group = groups_by_id.get(group_id)
        if not group:
            self.send_json({"error": "文章组已更新，请刷新页面"}, HTTPStatus.CONFLICT)
            return
        stamp = iso_now()
        with open_db(self.server.config.database) as database:
            if clicked:
                database.executemany(
                    """
                    INSERT INTO clicks(user_id, url, clicked_at) VALUES (?, ?, ?)
                    ON CONFLICT(user_id, url) DO UPDATE SET clicked_at = excluded.clicked_at
                    """,
                    [(user["id"], url, stamp) for url in group["urls"]],
                )
            else:
                placeholders = ",".join("?" for _ in group["urls"])
                database.execute(
                    f"DELETE FROM clicks WHERE user_id = ? AND url IN ({placeholders})",
                    [user["id"], *group["urls"]],
                )
        self.send_json({"ok": True, "group_id": group_id, "clicked_at": stamp if clicked else None})

    def handle_preferences(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        cities = normalize_str_list(body.get("cities", []), MAX_CITIES, MAX_CITY_LENGTH)
        keywords = normalize_str_list(body.get("keywords", []), MAX_KEYWORDS, MAX_KEYWORD_LENGTH)
        if cities is None or keywords is None:
            self.send_json({"error": "意向城市与方向关键词都应是字符串数组"}, HTTPStatus.BAD_REQUEST)
            return
        with open_db(self.server.config.database) as database:
            database.execute(
                """
                INSERT INTO preferences(user_id, cities, keywords, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  cities = excluded.cities,
                  keywords = excluded.keywords,
                  updated_at = excluded.updated_at
                """,
                (user["id"], json.dumps(cities, ensure_ascii=False),
                 json.dumps(keywords, ensure_ascii=False), iso_now()),
            )
        self.send_json({"ok": True, "preferences": user_preferences(self.server.config.database, user["id"])})

    # ---- AI 筛选任务 ----

    def log_tail(self, log_path: Path, max_lines: int = 12) -> list[str]:
        """读取运行日志的最后几行，供前端展示筛选进度。"""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [line for line in lines[-max_lines:] if line.strip()]

    def handle_screen_start(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        scope_all = bool(body.get("all"))
        date = "all" if scope_all else str(body.get("date", "")).strip()
        if not scope_all:
            if not DATE_RE.fullmatch(date):
                self.send_json({"error": "日期格式应为 YYYY-MM-DD"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                self.send_json({"error": "无效的日期"}, HTTPStatus.BAD_REQUEST)
                return
        force = bool(body.get("force"))
        script = self.server.config.screen_script
        if not script.is_file():
            self.send_json({"error": "找不到筛选脚本 screen_update.py"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        with self.server.screen_lock:
            if any(task["status"] == "running" for task in self.server.screen_tasks.values()):
                self.send_json({"error": "已有筛选任务在运行，请稍后再试"}, HTTPStatus.CONFLICT)
                return
            task = {"status": "running", "returncode": None, "force": force,
                    "started": time.time(), "user": user["username"]}
            self.server.screen_tasks[date] = task
        threading.Thread(target=self.run_screen_task, args=(date, force), daemon=True).start()
        self.send_json({"started": True, "date": date})

    def run_screen_task(self, date: str, force: bool) -> None:
        log_dir = self.server.config.screen_results.parent / "screen-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"screen-{date}.log"
        command = [sys.executable, str(self.server.config.screen_script)]
        command.append("--all" if date == "all" else date)
        if force:
            command.append("--force")
        with open(log_path, "w", encoding="utf-8") as log:
            process = subprocess.run(command, cwd=str(HERE), stdout=log, stderr=subprocess.STDOUT)
        task = self.server.screen_tasks.get(date)
        if task is None or task["status"] != "running":  # pragma: no cover
            return
        task["returncode"] = process.returncode
        task["status"] = "done" if process.returncode == 0 else "failed"

    def handle_screen_status(self) -> None:
        user = self.require_user()
        if user is None:
            return
        log_dir = self.server.config.screen_results.parent / "screen-logs"
        tasks = {}
        running = False
        for date, task in self.server.screen_tasks.items():
            item = dict(task)
            item["elapsed"] = round(time.time() - task["started"], 1)
            item["log"] = self.log_tail(log_dir / f"screen-{date}.log")
            tasks[date] = item
            running = running or task["status"] == "running"
        self.send_json({"running": running, "tasks": tasks})
