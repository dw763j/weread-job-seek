"""服务端公共常量与纯工具函数（无状态，供各 API 模块复用）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE.parent / "output" / "weread_extract" / "汇总-去重组.json"
DEFAULT_DATABASE = HERE / "data" / "clicks.sqlite3"
DEFAULT_SCREEN_RESULTS = HERE / "data" / "screen_results.json"
DEFAULT_SCREEN_SCRIPT = HERE / "screen_update.py"
STATIC_DIR = HERE / "static"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 新用户未自定义意向城市时的默认列表（前端用于地点高亮与「符合我的筛选」）
DEFAULT_CITIES = ["成都", "西安", "北京", "上海", "深圳", "东莞", "广州", "杭州"]
MAX_CITIES = 40
MAX_CITY_LENGTH = 24
MAX_KEYWORDS = 30
MAX_KEYWORD_LENGTH = 40
MAX_COLLECTIONS = 30
MAX_COLLECTION_NAME = 24
MAX_COMPANY_LENGTH = 100
MAX_URL_LENGTH = 600
MAX_NOTE_LENGTH = 500
MAX_STATUS_NOTE_LENGTH = 200
GROUP_ID_RE = re.compile(r"[0-9a-f]{24}")
# 投递进度：按推进顺序排列的阶段 + 随时可落入的终止状态；
# 「笔试」允许重复推进（第 N 轮笔试由历史记录区分轮次），前端渲染为步骤条而非下拉框。
APPLICATION_STAGES = (
    ("planned", "待投递"),
    ("applied", "已投递"),
    ("test", "笔试"),
    ("interview1", "一面"),
    ("interview2", "二面"),
    ("interview3", "三面"),
    ("final", "终面/HR面"),
    ("offer", "已录用"),
)
APPLICATION_CLOSED = (
    ("rejected", "未通过"),
    ("withdrawn", "已放弃"),
)
APPLICATION_STATUS_KEYS = frozenset(key for key, _ in APPLICATION_STAGES + APPLICATION_CLOSED)
TZ = timezone(timedelta(hours=8))
COOKIE_NAME = "job_links_session"
SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,40}$")
WECHAT_PREFIX = "https://mp.weixin.qq.com/"


def now() -> datetime:
    return datetime.now(TZ)


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def hash_access_code(code: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, 240_000).hex()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def parse_json_str_list(raw: str) -> list[str]:
    """把偏好表里的 JSON 字符串安全解析为字符串列表，损坏数据按空处理。"""
    try:
        value = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def normalize_str_list(value: Any, item_limit: int, item_max_length: int) -> list[str] | None:
    """整理用户提交的字符串数组：转 str、去空白、去重、限量；类型不对返回 None。"""
    if not isinstance(value, list):
        return None
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in items:
            items.append(text[:item_max_length])
    return items[:item_limit]


def clean_web_url(value: Any) -> str | None:
    """规范用户提交的链接：只接受 http(s):// 与 mailto:；空值返回空串，非法返回 None。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return ""
    if len(text) > MAX_URL_LENGTH:
        return None
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return text
    if parsed.scheme == "mailto" and re.fullmatch(r"mailto:\S+@\S+", text):
        return text
    return None


def body_int(body: dict[str, Any], key: str) -> int | None:
    """从请求体取整数 id；类型不对（含布尔）返回 None。"""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def new_access_code() -> str:
    return secrets.token_urlsafe(10)
