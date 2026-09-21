"""服务端公共常量与纯工具函数（无状态，供各 API 模块复用）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
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
# 宣讲会省份推断的唯一事实源（省级区划 / 常见城市 / 公众号所属高校）：
# screen_lib 供筛选脚本使用，bootstrap 的 geo 字段下发给前端 fair-core.js 使用
PROVINCES = ("北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
             "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
             "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
             "甘肃", "青海", "宁夏", "新疆", "香港", "澳门", "台湾")
CITY_PROVINCES = {
    "成都": "四川", "武汉": "湖北", "西安": "陕西", "南京": "江苏", "广州": "广东",
    "深圳": "广东", "东莞": "广东", "杭州": "浙江", "长沙": "湖南", "合肥": "安徽",
    "天津": "天津", "重庆": "重庆", "沈阳": "辽宁", "大连": "辽宁", "哈尔滨": "黑龙江",
    "长春": "吉林", "青岛": "山东", "济南": "山东", "郑州": "河南", "厦门": "福建",
    "福州": "福建", "昆明": "云南", "贵阳": "贵州", "兰州": "甘肃", "石家庄": "河北",
    "太原": "山西", "南昌": "江西", "南宁": "广西", "海口": "海南", "西宁": "青海",
    "银川": "宁夏", "拉萨": "西藏", "呼和浩特": "内蒙古", "乌鲁木齐": "新疆",
    "苏州": "江苏", "无锡": "江苏", "宁波": "浙江", "珠海": "广东", "佛山": "广东",
}
ACCOUNT_PROVINCES = {
    "北大就业": "北京", "北航就业": "北京", "国科大就业": "北京",
    "人大就业创业": "北京", "成功就业": "北京",
    "川大就业": "四川", "成电就业": "四川",
    "西安交大就业创业": "陕西", "西电科大就业指导服务中心": "陕西", "西工大就业": "陕西",
    "浙大就业": "浙江",
}
MAX_COLLECTIONS = 30
MAX_COLLECTION_NAME = 24
MAX_URL_LENGTH = 600
GROUP_ID_RE = re.compile(r"[0-9a-f]{24}")
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


def body_int(body: dict[str, Any], key: str) -> int | None:
    """从请求体取整数 id；类型不对（含布尔）返回 None。"""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def new_access_code() -> str:
    return secrets.token_urlsafe(10)
