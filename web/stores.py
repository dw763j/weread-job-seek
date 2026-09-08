"""按 mtime 自动重载的数据源：文章分组（抓取器产出）与 AI 筛选结果。"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from server_common import WECHAT_PREFIX


class ArticleStore:
    """按 mtime 自动重载抓取器产出的分组文件。"""

    def __init__(self, source: Path):
        self.source = source
        self._mtime_ns = -1
        self._payload: dict[str, Any] = {}
        self._groups_by_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        try:
            mtime_ns = self.source.stat().st_mtime_ns
        except FileNotFoundError as error:
            raise RuntimeError(f"找不到文章分组文件：{self.source}") from error
        with self._lock:
            if mtime_ns != self._mtime_ns:
                raw = json.loads(self.source.read_text(encoding="utf-8"))
                groups: list[dict[str, Any]] = []
                by_id: dict[str, dict[str, Any]] = {}
                for raw_group in raw.get("groups", []):
                    members = [
                        member
                        for member in raw_group.get("members", [])
                        if isinstance(member, dict)
                        and isinstance(member.get("文章链接"), str)
                        and member["文章链接"].startswith(WECHAT_PREFIX)
                    ]
                    if not members:
                        continue
                    canonical = raw_group.get("canonical") or members[0]
                    urls = sorted({member["文章链接"] for member in members})
                    group_id = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()[:24]
                    group = {
                        "id": group_id,
                        "title": str(canonical.get("文章标题", "")),
                        "url": str(canonical.get("文章链接", urls[0])),
                        "account": str(canonical.get("公众号", "")),
                        "date": str(canonical.get("发布日期", "")),
                        "urls": urls,
                        "members": [
                            {
                                "title": str(member.get("文章标题", "")),
                                "url": member["文章链接"],
                                "account": str(member.get("公众号", "")),
                                "date": str(member.get("发布日期", "")),
                            }
                            for member in members
                        ],
                    }
                    groups.append(group)
                    by_id[group_id] = group
                self._payload = {
                    "generated_at": raw.get("generated_at", ""),
                    "range": raw.get("range", {}),
                    "source_rows": raw.get("source_rows", 0),
                    "groups": groups,
                }
                self._groups_by_id = by_id
                self._mtime_ns = mtime_ns
            return self._payload, self._groups_by_id


class ScreenStore:
    """按 mtime 自动重载 AI 筛选结果（screen_update.py 产出），并建立 URL 索引供 bootstrap 合并。"""

    def __init__(self, path: Path):
        self.path = path
        self._mtime_ns = -1
        self._index: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[str, Any]]:
        """返回 文章 URL -> 筛选信息（kind=kept/skipped） 的索引；文件缺失或损坏时返回空索引。"""
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            with self._lock:
                self._mtime_ns = -1
                self._index = {}
            return self._index
        with self._lock:
            if mtime_ns != self._mtime_ns:
                index: dict[str, dict[str, Any]] = {}
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    for date, day in raw.get("days", {}).items():
                        for kind in ("kept", "skipped"):
                            for entry in day.get(kind, []):
                                screen = {key: value for key, value in entry.items() if key != "urls"}
                                screen["kind"] = kind
                                screen["screened_date"] = date
                                urls = entry.get("urls") or ([entry["article_url"]] if entry.get("article_url") else [])
                                for url in urls:
                                    index.setdefault(url, screen)
                except (OSError, json.JSONDecodeError, AttributeError):
                    index = {}
                self._index = index
                self._mtime_ns = mtime_ns
            return self._index
