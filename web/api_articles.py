"""文章看板 API：bootstrap 聚合、看板筛选分页查询、宣讲会数据、已读状态、筛选偏好、AI 筛选任务。"""

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
from urllib.parse import parse_qs, urlparse

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
    PROVINCES,
    CITY_PROVINCES,
    ACCOUNT_PROVINCES,
    iso_now,
    normalize_str_list,
    now,
)

BOARD_PAGE_SIZE = 40
SCREEN_KEYS = ("cs", "keywords", "cities", "skipped", "none")
# 与前端 fair-core.js / screen-info.js 同一套判定
CS_CATEGORY_RE = re.compile(r"计算机|软件|人工智能|大数据|算法|网络安全|开发|IT")
FAIR_TITLE_RE = re.compile(r"宣讲|双选|组团|线下招聘|招聘会")
EMPLOYER_INVITE_RE = re.compile(r"用人单位邀请|致用人单位|企业邀请函|诚邀(各)?(用人)?单位|参会单位邀请|单位邀请函|诚邀企业")


# ---------- 分组级工具：已读 / 筛选信息 / 宣讲会判定（与前端逻辑同构） ----------

def group_clicked_at(clicks: dict[str, str], group: dict) -> str | None:
    stamps = [clicks[url] for url in group["urls"] if url in clicks]
    return max(stamps) if stamps else None


def group_screen(screen_index: dict, group: dict) -> dict | None:
    return next((screen_index[url] for url in group["urls"] if url in screen_index), None)


def is_fair_group(group: dict, screen: dict | None) -> bool:
    """AI 的 fair 字段优先；v2 已判非招聘（含用人单位邀请函）不兜底；否则按标题关键词。"""
    if (screen or {}).get("fair", {}).get("is_fair"):
        return True
    if (screen or {}).get("kind") == "skipped" and (screen or {}).get("v") == 2:
        return False
    titles = [group["title"]] + [m["title"] for m in group["members"]]
    return any(FAIR_TITLE_RE.search(t) and not EMPLOYER_INVITE_RE.search(t) for t in titles)


def has_cs_position(screen: dict | None) -> bool:
    for position in (screen or {}).get("positions") or []:
        if CS_CATEGORY_RE.search(position.get("category") or "") or CS_CATEGORY_RE.search(position.get("name") or ""):
            return True
    return False


def keyword_hit(screen: dict | None, group: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    screen = screen or {}
    fields = " ".join(
        [f"{p.get('name', '')} {p.get('category', '')}" for p in screen.get("positions") or []]
        + [screen.get("intro") or "", screen.get("unit") or "", group["title"]]
    ).lower()
    return any(keyword.lower() in fields for keyword in keywords)


def city_hit(screen: dict | None, cities: list[str]) -> bool:
    if not cities:
        return True
    screen = screen or {}
    texts = [p.get("location") or "" for p in screen.get("positions") or []] + [screen.get("locations") or ""]
    return any(city in text for text in texts for city in cities)


def board_item(group: dict, clicked_at: str | None, screen: dict | None) -> dict:
    return {key: value for key, value in group.items() if key != "urls"} | {
        "clicked_at": clicked_at, "screen": screen,
    }


class ApiArticlesMixin:
    """挂在 CoreHandler 之上的文章看板端点。"""

    # ---------- 全量扫描：统计 + 公众号列表（bootstrap 与 clicks 轮询共用） ----------

    def scan_board(self, user_id: int) -> tuple[dict, list[dict]]:
        """一遍扫描产出看板统计（非宣讲）与宣讲会统计、公众号列表、最新发布日期。"""
        payload, _ = self.server.articles.load()
        clicks = clicked_urls(self.server.config.database, user_id)
        screen_index = self.server.screens.load()
        today = now().strftime("%Y-%m-%d")
        stats = {"total": 0, "read": 0, "unread": 0, "today_read": 0,
                 "fair_total": 0, "fair_upcoming": 0}
        accounts: dict[str, int] = {}
        latest_date = ""
        for group in payload["groups"]:
            screen = group_screen(screen_index, group)
            clicked = group_clicked_at(clicks, group)
            latest_date = max(latest_date, group["date"])
            if is_fair_group(group, screen):
                stats["fair_total"] += 1
                fair_date = ((screen or {}).get("fair") or {}).get("date") or ""
                if not fair_date or fair_date >= today:
                    stats["fair_upcoming"] += 1
                continue
            stats["total"] += 1
            accounts[group["account"]] = accounts.get(group["account"], 0) + 1
            if clicked:
                stats["read"] += 1
                if clicked[:10] == today:
                    stats["today_read"] += 1
        stats["unread"] = stats["total"] - stats["read"]
        account_list = [{"account": name, "count": count} for name, count in sorted(accounts.items())]
        return {"stats": stats, "accounts": account_list, "latest_date": latest_date}

    def handle_bootstrap(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            payload, _ = self.server.articles.load()
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        with open_db(self.server.config.database) as database:
            collections = collection_rows(database, user["id"])
            favorites = favorite_index(database, user["id"])
        scan = self.scan_board(user["id"])
        # bootstrap 只带轻量全局信息；文章明细按筛选条件经 /api/articles 分页拉取，
        # 宣讲会经 /api/fairs 拉取，避免全量数据一次性下发
        self.send_json(
            {
                "user": {"username": user["username"], "display_name": user["display_name"]},
                "generated_at": payload["generated_at"],
                "range": payload["range"],
                "source_rows": payload["source_rows"],
                # 宣讲会省份推断映射（单一事实源在 server_common），前端 fair-core 使用
                "geo": {
                    "provinces": list(PROVINCES),
                    "city_provinces": CITY_PROVINCES,
                    "account_provinces": ACCOUNT_PROVINCES,
                },
                "preferences": user_preferences(self.server.config.database, user["id"]),
                "collections": collections,
                "favorites": favorites,
                **scan,
            }
        )

    # ---------- 看板：筛选 + 分页在后端完成 ----------

    def handle_articles(self) -> None:
        user = self.require_user()
        if user is None:
            return
        try:
            payload, _ = self.server.articles.load()
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        query = parse_qs(urlparse(self.path).query)
        status = query.get("status", ["unread"])[0]
        if status not in ("unread", "read", "all"):
            status = "unread"
        account = query.get("account", [""])[0][:60]
        screens = [key for key in query.get("screens", [""])[0].split(",") if key in SCREEN_KEYS]
        search = query.get("q", [""])[0].strip().lower()[:100]
        exclude = set(query.get("exclude", [""])[0].split(",")) - {""}
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
        except ValueError:
            page = 1
        try:
            page_size = min(100, max(1, int(query.get("page_size", [str(BOARD_PAGE_SIZE)])[0])))
        except ValueError:
            page_size = BOARD_PAGE_SIZE

        prefs = user_preferences(self.server.config.database, user["id"])
        clicks = clicked_urls(self.server.config.database, user["id"])
        screen_index = self.server.screens.load()

        # chips 计数跟随本次查询的上下文（状态/公众号/搜索，不含 chips 本身）：
        # 每个 chip 显示"再叠加该条件还剩多少条"，方便决定是否勾选
        counts = {key: 0 for key in SCREEN_KEYS}
        context: list[tuple[dict, dict | None]] = []
        for group in payload["groups"]:
            screen = group_screen(screen_index, group)
            clicked = group_clicked_at(clicks, group)
            if is_fair_group(group, screen):
                continue
            if status == "read" and not clicked:
                continue
            # exclude = 本会话点开过的文章（客户端传上来）：已读但留在未读列表变暗展示
            if status == "unread" and (clicked or group["id"] in exclude):
                continue
            if account and group["account"] != account:
                continue
            if search:
                haystack = " ".join(
                    [group["title"], group["account"]]
                    + [f"{m['title']} {m['account']}" for m in group["members"]]
                ).lower()
                if search not in haystack:
                    continue
            context.append((group, screen))
            kept = screen is not None and screen.get("kind") == "kept"
            if kept:
                if has_cs_position(screen):
                    counts["cs"] += 1
                if keyword_hit(screen, group, prefs["keywords"]):
                    counts["keywords"] += 1
                if city_hit(screen, prefs["cities"]):
                    counts["cities"] += 1
            elif screen is not None and screen.get("kind") == "skipped":
                counts["skipped"] += 1
            else:
                counts["none"] += 1

        # 上下文命中后再应用 chips 条件（多选交集），分页返回
        visible = [
            board_item(group, group_clicked_at(clicks, group), screen)
            for group, screen in context
            if self._screens_match(screens, screen, group, prefs)
        ]

        total = len(visible)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        items = visible[(page - 1) * page_size: page * page_size]
        self.send_json({"items": items, "page": page, "pages": pages,
                        "total": total, "page_size": page_size, "counts": counts})

    @staticmethod
    def _screens_match(screens: list[str], screen: dict | None, group: dict, prefs: dict) -> bool:
        kept = screen is not None and screen.get("kind") == "kept"
        for key in screens:
            if key == "cs" and not (kept and has_cs_position(screen)):
                return False
            if key == "keywords" and not (kept and keyword_hit(screen, group, prefs["keywords"])):
                return False
            if key == "cities" and not (kept and city_hit(screen, prefs["cities"])):
                return False
            if key == "skipped" and not (screen is not None and screen.get("kind") == "skipped"):
                return False
            if key == "none" and screen is not None:
                return False
        return True

    # ---------- 宣讲会：数据量小（数百条），整包下发由前端聚合 ----------

    def handle_fairs(self) -> None:
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
        today = now().strftime("%Y-%m-%d")
        items = []
        upcoming = 0
        for group in payload["groups"]:
            screen = group_screen(screen_index, group)
            if not is_fair_group(group, screen):
                continue
            fair = (screen or {}).get("fair") or None
            if fair:
                date = fair.get("date") or ""
                if not date or date >= today:
                    upcoming += 1
            items.append(board_item(group, group_clicked_at(clicks, group),
                                    {"fair": fair} if fair else None))
        self.send_json({"items": items, "total": len(items), "upcoming": upcoming})

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
        # stats 随轮询刷新：看板进度条/悬浮统计与 chips 计数保持最新
        self.send_json({"states": states, "updated_at": iso_now(),
                        **self.scan_board(user["id"])})

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
        # 部分更新：只更新请求里实际携带的字段，避免不同保存入口互相覆盖
        updates: dict[str, str] = {}
        if "cities" in body:
            cities = normalize_str_list(body.get("cities"), MAX_CITIES, MAX_CITY_LENGTH)
            if cities is None:
                self.send_json({"error": "意向城市应是字符串数组"}, HTTPStatus.BAD_REQUEST)
                return
            updates["cities"] = json.dumps(cities, ensure_ascii=False)
        if "keywords" in body:
            keywords = normalize_str_list(body.get("keywords"), MAX_KEYWORDS, MAX_KEYWORD_LENGTH)
            if keywords is None:
                self.send_json({"error": "方向关键词应是字符串数组"}, HTTPStatus.BAD_REQUEST)
                return
            updates["keywords"] = json.dumps(keywords, ensure_ascii=False)
        if not updates:
            self.send_json({"error": "没有要保存的偏好字段"}, HTTPStatus.BAD_REQUEST)
            return
        columns = ", ".join(f"{key} = excluded.{key}" for key in updates)
        with open_db(self.server.config.database) as database:
            database.execute(
                f"""
                INSERT INTO preferences(user_id, {", ".join(updates)}, updated_at)
                VALUES (?, {", ".join("?" for _ in updates)}, ?)
                ON CONFLICT(user_id) DO UPDATE SET {columns}, updated_at = excluded.updated_at
                """,
                [user["id"], *updates.values(), iso_now()],
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
