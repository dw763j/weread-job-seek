#!/usr/bin/env python3
"""生成“全部公众号”汇总 Markdown。

规则：
- 默认汇总自 2026-07-10 起的全部结果（不截断）；--range YYYYMMDD-YYYYMMDD 指定起止（含两端）。
- 完全相同标题的文章合并为一组（保留组内全部链接）。
- 相似度较高的候选组再用 OpenAI 兼容接口做语义去重。
  端点与模型可用 DEDUP_API_BASE / DEDUP_MODEL 配置（当前指向本地 vLLM 部署的
  zai-org/GLM-5.3-Flash；默认值保留 DeepSeek 官网 deepseek-v4-flash）。
- Markdown 结构：日期 = 二级标题，公众号名 = 三级标题。

密钥只通过 --api-key 或 DEEPSEEK_API_KEY 读取，绝不写入输出文件。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import html
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import certifi


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output" / "weread_extract"
DEFAULT_INPUT_DIR = OUTPUT
HTML_DIR = ROOT / "output"
CLICKED_STATE = HTML_DIR / "已点击链接.json"
CLICKED_HTML_GLOB = "微信读书汇总-*.html"
TZ = timezone(timedelta(hours=8))
DEFAULT_RANGE_START = "20260710"  # 默认汇总起点：2026-07-10 起保留全部结果，不截断


def normalized_title(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).lower()


ENTITY_STOPWORDS = {
    "招聘", "校招", "校园招聘", "校园", "应届生", "毕业生", "秋招", "春招",
    "正式启动", "全面启动", "提前批", "开始", "开启", "启动", "计划", "项目",
    "招募", "网申", "通道", "发布", "岗位", "实习生", "实习", "全球", "面向",
    "邀请函", "通知", "公告", "进行中", "报名", "宣讲会", "双选会", "专场",
    "名企", "名企优选", "名企校招", "企业", "企业招聘", "国企", "国企招聘",
    "国防", "国防招聘", "国防军工", "就业信息", "重要", "官方",
    "2027届", "2026届", "2028届", "2027", "2026", "2025", "2024", "届",
}


def entity_text(value: str) -> str:
    """去掉模板词和符号，尽量留下公司/事件实体，用于判断是否同一家公司的同一篇。"""
    text = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    for word in sorted(ENTITY_STOPWORDS, key=len, reverse=True):
        text = text.replace(word, "")
    return text.lower()


# ---------------- 品牌族谱（防止同一集团下不同子公司的招聘被误合并） ----------------
# 背景：语义去重曾把「阿里巴巴集团招聘」「阿里云招聘」「淘天集团招聘」「盒马招聘」合并成
# 一个 26 条的大组——它们是同一集团下不同雇主的独立招聘。单纯分词只能拆出词，但无法判断
# 「阿里巴巴盒马」的招聘主体是盒马、「阿里巴巴阿里云智能集团」的主体是阿里云，这需要
# 母子品牌关系。因此：别名表 + 最长匹配提取标题中的品牌，再按族谱把“冗余的母品牌”剔除，
# 得到实际招聘主体；主体不同的两个标题强制判为非重复（硬否决），不进模型也不复用旧结论。
#
# strict 族（STRICT_ROOTS）：集团与各子公司确实各自独立发招聘（阿里系、军工集团/院院所），
# 母品牌与子品牌之间也硬否决；非 strict 族（网易、腾讯等）母子公司措辞差异常见于同一篇
# 转发（如「网易」vs「网易游戏」常指同一次校招），仅子品牌之间硬否决，母子交给模型。
BRAND_FAMILY: dict[str, dict] = {
    # 阿里巴巴系（strict：集团/阿里云/淘天/盒马等各自独立招聘）
    "ALIBABA": {"names": ["阿里巴巴集团", "阿里巴巴", "阿里"]},
    "ALIYUN": {"names": ["阿里云智能集团", "阿里云智能事业群", "阿里云智能", "阿里云"], "parent": "ALIBABA"},
    "TAOTIAN": {"names": ["淘天集团", "淘天"], "parent": "ALIBABA"},
    "HEMA": {"names": ["盒马鲜生", "盒马"], "parent": "ALIBABA"},
    "CAINIAO": {"names": ["菜鸟集团", "菜鸟网络", "菜鸟"], "parent": "ALIBABA"},
    "ELEME": {"names": ["饿了么"], "parent": "ALIBABA"},
    "AMAP": {"names": ["高德地图", "高德"], "parent": "ALIBABA"},
    "DINGTALK": {"names": ["钉钉"], "parent": "ALIBABA"},
    "FLYPIG": {"names": ["飞猪"], "parent": "ALIBABA"},
    "YOUKU": {"names": ["优酷"], "parent": "ALIBABA"},
    "ALIHEALTH": {"names": ["阿里健康"], "parent": "ALIBABA"},
    "INTIME": {"names": ["银泰商业", "银泰"], "parent": "ALIBABA"},
    "PINGTOUGE": {"names": ["平头哥"], "parent": "ALIBABA"},
    "ANTS": {"names": ["蚂蚁集团", "蚂蚁金服", "蚂蚁"]},  # 独立法人，与阿里巴巴各自招聘
    # 腾讯系（loose：统一校招为主，母子措辞差异常指同一次）
    "TENCENT": {"names": ["腾讯"]},
    "TENCENT_CLOUD": {"names": ["腾讯云"], "parent": "TENCENT"},
    "TENCENT_MUSIC": {"names": ["腾讯音乐"], "parent": "TENCENT"},
    # 华为系（loose）
    "HUAWEI": {"names": ["华为"]},
    "HUAWEI_CLOUD": {"names": ["华为云"], "parent": "HUAWEI"},
    "HISILICON": {"names": ["海思"], "parent": "HUAWEI"},
    # 字节系（loose）
    "BYTEDANCE": {"names": ["字节跳动", "字节"]},
    "DOUYIN": {"names": ["抖音"], "parent": "BYTEDANCE"},
    "FEISHU": {"names": ["飞书"], "parent": "BYTEDANCE"},
    "VOLCENGINE": {"names": ["火山引擎"], "parent": "BYTEDANCE"},
    "TOUTIAO": {"names": ["今日头条"], "parent": "BYTEDANCE"},
    # 京东系（loose）
    "JD": {"names": ["京东集团", "京东"]},
    "JD_LOGISTICS": {"names": ["京东物流"], "parent": "JD"},
    "JD_HEALTH": {"names": ["京东健康"], "parent": "JD"},
    "JD_TECH": {"names": ["京东科技"], "parent": "JD"},
    "JD_RETAIL": {"names": ["京东零售"], "parent": "JD"},
    # 网易系（loose：网易/网易游戏常指同一次校招）
    "NETEASE": {"names": ["网易公司", "网易"]},
    "NETEASE_GAMES": {"names": ["网易游戏", "网易雷火", "网易互娱"], "parent": "NETEASE"},
    "NETEASE_MUSIC": {"names": ["网易云音乐"], "parent": "NETEASE"},
    "NETEASE_YOUDAO": {"names": ["网易有道"], "parent": "NETEASE"},
    # 美团（loose）
    "MEITUAN": {"names": ["美团"]},
    "DIANPING": {"names": ["大众点评"], "parent": "MEITUAN"},
    # 百度 / 拼多多（无子公司混淆）
    "BAIDU": {"names": ["百度"]},
    "PDD": {"names": ["拼多多"]},
    # 航空工业（strict：集团与主机厂/研究所各自招聘）
    "AVIC": {"names": ["中国航空工业集团", "航空工业集团", "中航工业", "航空工业"]},
    "CHENGFEI": {"names": ["成飞集团", "成飞"], "parent": "AVIC"},
    "SHENFEI": {"names": ["沈飞集团", "沈飞"], "parent": "AVIC"},
    "XIFEI": {"names": ["西飞集团", "西飞"], "parent": "AVIC"},
    "HONGDU": {"names": ["洪都航空", "洪都"], "parent": "AVIC"},
    # 航天科技集团（strict：集团与各院各自招聘）
    "CASC": {"names": ["中国航天科技集团", "航天科技集团", "中国航天科技", "航天科技"]},
    "CASC_1": {"names": ["航天一院", "中国运载火箭技术研究院", "运载火箭技术研究院"], "parent": "CASC"},
    "CASC_5": {"names": ["航天五院", "中国空间技术研究院", "空间技术研究院"], "parent": "CASC"},
    "CASC_8": {"names": ["航天八院", "上海航天技术研究院"], "parent": "CASC"},
    "CASC_6": {"names": ["航天六院"], "parent": "CASC"},
    "CASC_9": {"names": ["航天九院"], "parent": "CASC"},
    "CASC_11": {"names": ["航天十一院"], "parent": "CASC"},
    # 航天科工集团（strict）
    "CASIC": {"names": ["中国航天科工集团", "航天科工集团", "中国航天科工", "航天科工"]},
    "CASIC_2": {"names": ["航天二院", "科工二院"], "parent": "CASIC"},
    "CASIC_3": {"names": ["航天三院", "科工三院"], "parent": "CASIC"},
    "CASIC_4": {"names": ["航天四院", "科工四院"], "parent": "CASIC"},
    "ZHIXIN": {"names": ["航天智信"], "parent": "CASIC_3"},
    # 中国电科（strict：集团与各研究所各自招聘）
    "CETC": {"names": ["中国电子科技集团", "中国电科集团", "中国电科", "中电科"]},
    "CETC_14": {"names": ["中电科十四所", "电科十四所", "十四所"], "parent": "CETC"},
    "CETC_28": {"names": ["中电科二十八所", "电科二十八所", "二十八所"], "parent": "CETC"},
    "CETC_38": {"names": ["中电科三十八所", "电科三十八所", "三十八所"], "parent": "CETC"},
    "CETC_55": {"names": ["中电科五十五所", "电科五十五所", "五十五所"], "parent": "CETC"},
    # 中国电子（与电科是两个不同集团）
    "CEC": {"names": ["中国电子信息产业集团", "中国电子信息", "中国电子"]},
}
STRICT_ROOTS = {"ALIBABA", "AVIC", "CASC", "CASIC", "CETC"}

# 最长别名优先（保证「阿里巴巴盒马」先匹配出 阿里巴巴 再匹配 盒马，
# 而「阿里云智能集团」整体命中而非拆成 阿里+云）。
_BRAND_MATCHER: list[tuple[str, str]] = sorted(
    ((name, bid) for bid, spec in BRAND_FAMILY.items() for name in spec["names"]),
    key=lambda item: -len(item[0]),
)


def brand_ancestors(brand_id: str) -> set[str]:
    """品牌的全部祖先（母品牌链）。"""
    seen: set[str] = set()
    current = BRAND_FAMILY.get(brand_id, {}).get("parent")
    while current and current not in seen:
        seen.add(current)
        current = BRAND_FAMILY.get(current, {}).get("parent")
    return seen


def brand_root(brand_id: str) -> str:
    chain = brand_ancestors(brand_id)
    if not chain:
        return brand_id
    # 最深的祖先 = root
    for candidate in chain:
        if not brand_ancestors(candidate):
            return candidate
    return brand_id


def brand_mentions(title: str) -> list[str]:
    """最长匹配扫描标题中出现的品牌（按出现顺序）。"""
    mentions: list[str] = []
    i = 0
    n = len(title)
    while i < n:
        for name, bid in _BRAND_MATCHER:
            if title.startswith(name, i):
                mentions.append(bid)
                i += len(name)
                break
        else:
            i += 1
    return mentions


def effective_brands(title: str) -> frozenset[str]:
    """标题的“实际招聘主体”：剔除被同标题内子品牌覆盖的冗余母品牌。

    例：「阿里巴巴盒马」→ {HEMA}；「阿里巴巴阿里云智能集团」→ {ALIYUN}；
    「阿里巴巴」→ {ALIBABA}；「腾讯字节双选会」→ {TENCENT, BYTEDANCE}（多品牌，不否决）。
    """
    mentions = set(brand_mentions(title))
    if not mentions:
        return frozenset()
    return frozenset(
        brand
        for brand in mentions
        # 母品牌被同标题内的子品牌覆盖 → 剔除
        if not any(other != brand and brand in brand_ancestors(other) for other in mentions)
    )


def brand_veto_sets(left: frozenset[str], right: frozenset[str]) -> bool:
    """brand_veto 的集合版（热路径用，先批量算好 effective_brands 再比较）。

    仅在双方主体都是单一品牌且不同才否决；任一方是多品牌同现（宣讲会/双选会类标题）
    或未识别出品牌时不否决，交给模型。同族父子仅在 strict 族否决。
    """
    if not left or not right or len(left) > 1 or len(right) > 1:
        return False
    a, b = next(iter(left)), next(iter(right))
    if a == b:
        return False
    if b in brand_ancestors(a) or a in brand_ancestors(b):
        # 同族父子品牌：仅严格族（各自独立招聘）才否决
        return brand_root(a) in STRICT_ROOTS and brand_root(b) in STRICT_ROOTS
    return True  # 不同品牌（不同族或同族兄弟）→ 一定否决


def brand_veto(left_title: str, right_title: str) -> bool:
    """两个标题的招聘主体是否确定不同（硬否决合并）。"""
    return brand_veto_sets(effective_brands(left_title), effective_brands(right_title))


def read_account_rows(input_dir: Path) -> list[dict[str, str]]:
    """读取输出目录里所有单号 CSV（跳过汇总文件）。"""
    if not input_dir.is_dir():
        raise SystemExit(f"找不到单号输出目录：{input_dir}（请先运行 weread_extract.mjs）")
    rows: list[dict[str, str]] = []
    for path in sorted(input_dir.glob("*.csv")):
        if path.name.startswith("汇总-"):
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            rows.extend(list(csv.DictReader(source)))
    return rows


def write_summary_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as source:
        writer = csv.DictWriter(
            source,
            fieldnames=["公众号", "发布日期", "文章标题", "文章链接", "来源"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "公众号": row.get("公众号", ""),
                    "发布日期": row.get("发布日期", ""),
                    "文章标题": row.get("文章标题", ""),
                    "文章链接": row.get("文章链接", ""),
                    "来源": "微信读书",
                }
            )


def date_key(value: str) -> str:
    return value.replace("-", "") if value else ""


def load_dotenv() -> None:
    """读取项目根目录 .env，且不覆盖已有环境变量。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value


def dedup_config() -> tuple[str, str, str]:
    """返回 (api_base, model, api_key)。api_base 为 OpenAI 兼容基础 URL（不含 /chat/completions）。"""
    api_base = os.environ.get("DEDUP_API_BASE", "https://api.deepseek.com")
    model = os.environ.get("DEDUP_MODEL", "deepseek-v4-flash")
    api_key = os.environ.get("DEDUP_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    return api_base, model, api_key


def parse_range(raw: str | None) -> tuple[str, str]:
    if not raw:
        return DEFAULT_RANGE_START, "99999999"
    m = re.fullmatch(r"(\d{8})(?:-(\d{8}))?", raw)
    if not m:
        raise SystemExit(f"--range 格式应为 YYYYMMDD-YYYYMMDD（如 20260710-20260805），收到：{raw}")
    return m.group(1), m.group(2) or m.group(1)


def in_window(row: dict[str, str], lo: str, hi: str) -> bool:
    key = date_key(row.get("发布日期", ""))
    return bool(key) and lo <= key <= hi


def load_clicked_state(path: Path | None = None) -> dict[str, str]:
    """读取已点击状态文件：{url: ISO 时间}。文件不存在时返回空 dict。"""
    target = path or CLICKED_STATE
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    urls = payload.get("urls", payload) if isinstance(payload, dict) else {}
    result: dict[str, str] = {}
    for url, stamp in urls.items():
        if isinstance(url, str) and url.startswith("https://mp.weixin.qq.com/"):
            result[url] = stamp if isinstance(stamp, str) else datetime.now(TZ).isoformat(timespec="seconds")
    return result


def clicked_state_from_html(text: str) -> dict[str, str]:
    """从旧 HTML 内嵌的 var CLICKED = {...}; 恢复已点 URL。"""
    match = re.search(r"var\s+CLICKED\s*=\s*(\{.*?\})\s*;", text, flags=re.S)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        url: stamp if isinstance(stamp, str) else datetime.now(TZ).isoformat(timespec="seconds")
        for url, stamp in payload.items()
        if isinstance(url, str) and url.startswith("https://mp.weixin.qq.com/")
    }


def merge_clicked_states(*states: dict[str, str]) -> dict[str, str]:
    """合并多个点击状态；同一 URL 保留较新的时间戳。"""
    merged: dict[str, str] = {}
    for state in states:
        for url, stamp in state.items():
            if url not in merged or (stamp or "") > (merged[url] or ""):
                merged[url] = stamp
    return merged


def save_clicked_state(state: dict[str, str], source: str = "auto-merge", path: Path | None = None) -> None:
    """把已点 URL 状态落盘，供下次生成汇总时复用。"""
    target = path or CLICKED_STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "source": source,
                "urls": dict(sorted(state.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def collect_clicked_state(html_dir: Path, state_path: Path | None = None) -> dict[str, str]:
    """复用旧结果：合并状态文件 + 所有旧 HTML 里已内嵌的点击状态。"""
    state = load_clicked_state(state_path)
    for html_path in sorted(html_dir.glob(CLICKED_HTML_GLOB)):
        try:
            state = merge_clicked_states(state, clicked_state_from_html(html_path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return state


def propagate_clicked_in_groups(
    clicked: dict[str, str],
    merged_groups: list[list[dict[str, str]]],
) -> dict[str, str]:
    """组内任一链接已点，则同组全部链接都视为已点。

    保留每个 URL 自身已有的时间戳；未点成员继承组内已点链接的最新时间戳。
    """
    propagated = dict(clicked)
    for group in merged_groups:
        group_urls = [member["文章链接"] for member in group]
        group_stamps = [propagated[url] for url in group_urls if url in propagated]
        if not group_stamps:
            continue
        newest = max(group_stamps, key=lambda stamp: stamp or "")
        for url in group_urls:
            if url not in propagated:
                propagated[url] = newest
    return propagated


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, node: int) -> int:
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


# 各公众号转发同一篇文章时惯加的栏目前缀 / 活动后缀：剥掉后完全相同的标题视为
# 同一篇，直接合并、不送模型。模型对「仅前缀不同」的对判定不稳（2026-08-28 一批
# 33 对同篇转发被判成非重复），故用确定性规则兜底。前缀按最长优先匹配。
WRAP_PREFIXES = (
    "名企校招", "名企优选", "国防军工", "国防招聘", "国企招聘", "企业招聘", "高校招聘",
    "就业信息", "就业指导", "校园招聘", "校招", "招聘",
)
# 后缀剥除要求剩余长度 ≥ 8，避免把「…启动」这类短标题剥成空串。
WRAP_SUFFIXES = ("正式启动", "正式开启", "正式发布", "重磅启动", "火热开启", "启动", "开启")
UNWRAP_MIN_LEN = 8


def unwrap_title(normalized: str) -> str:
    """剥掉转发前缀与启动类后缀，返回用于确定性归并的标题。"""
    title = normalized
    for prefix in WRAP_PREFIXES:
        if title.startswith(prefix) and len(title) - len(prefix) >= UNWRAP_MIN_LEN:
            title = title[len(prefix):]
            break
    for suffix in WRAP_SUFFIXES:
        if title.endswith(suffix) and len(title) - len(suffix) >= UNWRAP_MIN_LEN:
            title = title[: -len(suffix)]
            break
    return title


def candidate_pairs(rows: list[dict[str, str]], same_title: set[tuple[int, int]]) -> list[tuple[int, int]]:
    scored: list[tuple[float, int, int]] = []
    normalized = [normalized_title(row["文章标题"]) for row in rows]
    entities = [entity_text(row["文章标题"]) for row in rows]
    brand_sets = [effective_brands(row["文章标题"]) for row in rows]  # 预计算，O(n) 次扫描
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if (left, right) in same_title:
                continue
            # 品牌硬否决：招聘主体不同（如阿里云 vs 淘天）不可能是同一篇，也不送模型
            if brand_veto_sets(brand_sets[left], brand_sets[right]):
                continue
            full_ratio = SequenceMatcher(None, normalized[left], normalized[right]).ratio()
            shorter, longer = sorted((normalized[left], normalized[right]), key=len)
            contained = len(shorter) >= 5 and shorter in longer
            ent_left, ent_right = entities[left], entities[right]
            ent_ratio = (
                SequenceMatcher(None, ent_left, ent_right).ratio()
                if ent_left and ent_right
                else 0.0
            )
            # 三路任一命中即送模型复核：整题高度相似 / 短题被包含（前缀后缀差异） / 实体一致
            if ent_ratio >= 0.8 or full_ratio >= 0.85 or contained:
                scored.append((max(ent_ratio, full_ratio, 1.0 if contained else 0.0), left, right))
    scored.sort(reverse=True)
    return [(left, right) for _, left, right in scored]


def _deepseek_one_batch(
    batch: list[tuple[int, int]],
    rows: list[dict[str, str]],
    endpoint: str,
    model: str,
    api_key: str,
    batch_no: int,
) -> tuple[int, set[tuple[int, int]]]:
    """请求单个批次（40 对），返回 (批次号, 确认重复对)。失败重试 3 次（指数退避）。"""
    options = [
        {"a": left, "a_title": rows[left]["文章标题"], "b": right, "b_title": rows[right]["文章标题"]}
        for left, right in batch
    ]
    instruction = {
        "task": "Identify only pairs that are the same underlying WeChat article: the same company/organization and the same event, reposted under a slightly different title. Titles that merely follow the same template (for example different companies each posting their own 2027 campus recruitment) are NOT duplicates. Do not merge different companies, different batches, or different events. CRITICAL: subsidiaries/business groups of the same parent company are SEPARATE employers with separate recruitment programs — for example 阿里云、淘天集团、盒马、菜鸟 are each different from 阿里巴巴集团 and from each other; 航天科技集团 and 航天科工集团 are different groups; a research institute (研究所/院) is different from its parent group. Never merge titles whose hiring entity differs, even if the parent company name matches. IMPORTANT: university employment accounts often prepend their own column label (企业招聘、校招、国企招聘、名企优选、就业信息、国防招聘 etc.) or append 启动/正式启动 to the same reposted article — when the company AND event match, such prefix/suffix-only differences ARE duplicates (e.g. 「企业招聘 | 小米2027届全球校园招聘正式启动」 and 「招聘 | 小米2027届全球校园招聘正式启动」 are the same article). When uncertain, do not mark as duplicate.",
        "pairs": options,
        "output": {"duplicate_pairs": [["a", "b"]]},
    }
    payload = json.dumps(
        {
            "model": model,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)},
            ],
            "temperature": 0,
            # 本地 vLLM 端点即使 thinking 关闭也可能输出推理 token，4096 会被吃满导致
            # content 为空，放宽到 16384。
            "max_tokens": 16384,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers=headers,
        method="POST",
    )
    result = None
    for attempt in range(3):
        try:
            tls_context = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(request, timeout=120, context=tls_context) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            if not content:
                raise ValueError("模型返回 content 为空（推理 token 可能被截断，可调大 max_tokens）")
            result = json.loads(content)
            break
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as error:
            if attempt < 2:
                delay = 5 * (attempt + 1)
                print(f"    批次 {batch_no} 失败（{error}），{delay}s 后重试", flush=True)
                time.sleep(delay)
            else:
                raise RuntimeError(f"DeepSeek 请求失败（批次 {batch_no}）：{error}") from error
    if result is None:
        raise RuntimeError(f"DeepSeek 请求失败（批次 {batch_no}）")
    allowed = {tuple(pair) for pair in batch}
    confirmed: set[tuple[int, int]] = set()
    for item in result.get("duplicate_pairs", []):
        if isinstance(item, list) and len(item) == 2 and all(isinstance(x, int) for x in item):
            pair = tuple(sorted(item))
            if pair in allowed:
                confirmed.add(pair)
    return batch_no, confirmed


def deepseek_pairs(
    rows: list[dict[str, str]],
    pairs: list[tuple[int, int]],
    api_base: str,
    model: str,
    api_key: str,
    concurrency: int = 4,
) -> set[tuple[int, int]]:
    """并发请求模型判断候选对。concurrency 路并发（默认 4，可用
    --concurrency / WR_DEDUP_CONCURRENCY 调整），单批 40 对、失败指数退避重试 3 次；
    任一批最终失败则整体失败（与串行版一致，避免静默丢对）。"""
    endpoint = f"{api_base.rstrip('/')}/chat/completions"
    confirmed: set[tuple[int, int]] = set()
    batches = [
        (batch_no, pairs[start : start + 40])
        for batch_no, start in enumerate(range(0, len(pairs), 40), 1)
    ]
    total_batches = len(batches)
    print(
        f"  语义去重：{total_batches} 批（共 {len(pairs)} 对候选），并发 {concurrency} 路",
        flush=True,
    )
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_deepseek_one_batch, batch, rows, endpoint, model, api_key, batch_no): batch_no
            for batch_no, batch in batches
        }
        for future in concurrent.futures.as_completed(futures):
            batch_no, batch_confirmed = future.result()  # 失败在这里抛出 → 整体失败
            confirmed |= batch_confirmed
            done += 1
            if done % 5 == 0 or done == total_batches:
                print(f"    已完成 {done}/{total_batches} 批", flush=True)
    return confirmed


def load_previous_groups(
    path: Path,
) -> tuple[dict[str, list[dict[str, str]]], dict[tuple[str, str], dict[str, Any]]]:
    """读取上次 汇总-去重组.json，返回 (groups, 已判断候选对缓存)。"""
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    groups: dict[str, list[dict[str, str]]] = {}
    for index, group in enumerate(payload.get("groups", [])):
        if not isinstance(group, dict):
            continue
        members = group.get("members", [])
        if isinstance(members, list):
            groups[str(index)] = [member for member in members if isinstance(member, dict)]
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload.get("decided_pairs", []):
        if not isinstance(item, dict):
            continue
        urls = item.get("urls")
        if not isinstance(urls, list) or len(urls) != 2:
            continue
        left_url, right_url = urls
        if isinstance(left_url, str) and isinstance(right_url, str):
            decisions[tuple(sorted((left_url, right_url)))] = item
    return groups, decisions


def reuse_previous_semantic_pairs(
    rows: list[dict[str, str]],
    pairs: list[tuple[int, int]],
    previous_groups_path: Path,
) -> tuple[set[tuple[int, int]], list[tuple[int, int]], dict[tuple[str, str], dict[str, Any]]]:
    """复用上次分组结论，返回 (确认重复对, 仍需重新判断的对, 已判断候选对缓存)。

    上次同组且标题没变的对直接沿用；旧版结果（无逐对缓存）中不同组也视为已判非重复。
    新格式下只有明确判断过的不同组对才跳过；新链接、标题变化或未判断过的对需要重新判断。
    """
    previous_groups, decisions = load_previous_groups(previous_groups_path)
    if not previous_groups and not decisions:
        return set(), list(pairs), {}
    had_decision_cache = bool(decisions)

    group_of_url: dict[str, str] = {}
    title_of_url: dict[str, str] = {}
    for group_id, members in previous_groups.items():
        for member in members:
            url = member.get("文章链接")
            title = member.get("文章标题")
            if isinstance(url, str) and url:
                group_of_url[url] = group_id
                title_of_url[url] = normalized_title(title or "")

    index_by_url = {row["文章链接"]: index for index, row in enumerate(rows)}
    confirmed: set[tuple[int, int]] = set()
    for members in previous_groups.values():
        indexes: list[int] = []
        for member in members:
            url = member.get("文章链接")
            index = index_by_url.get(url) if isinstance(url, str) else None
            if index is None:
                continue
            if url in title_of_url and title_of_url[url] == normalized_title(rows[index]["文章标题"]):
                indexes.append(index)
        for offset, left in enumerate(indexes):
            confirmed.update((left, right) for right in indexes[offset + 1 :])

    to_ask: list[tuple[int, int]] = []
    for left, right in pairs:
        left_url = rows[left]["文章链接"]
        right_url = rows[right]["文章链接"]
        left_unchanged = (
            left_url in title_of_url
            and title_of_url[left_url] == normalized_title(rows[left]["文章标题"])
        )
        right_unchanged = (
            right_url in title_of_url
            and title_of_url[right_url] == normalized_title(rows[right]["文章标题"])
        )
        if left_unchanged and right_unchanged:
            pair_key = tuple(sorted((left_url, right_url)))
            if group_of_url.get(left_url) == group_of_url.get(right_url):
                # 同组已由 confirmed 覆盖，无需再问。
                if not had_decision_cache:
                    urls = [left_url, right_url]
                    titles = [
                        normalized_title(rows[left]["文章标题"]),
                        normalized_title(rows[right]["文章标题"]),
                    ]
                    if urls[0] > urls[1]:
                        urls.reverse()
                        titles.reverse()
                    decisions[pair_key] = {
                        "urls": urls,
                        "titles": titles,
                        "duplicate": True,
                        "decided_at": datetime.now(TZ).isoformat(timespec="seconds"),
                    }
                continue
            if pair_key in decisions:
                if decisions[pair_key].get("duplicate"):
                    confirmed.add((left, right))
                continue
            if not had_decision_cache:
                # 旧版结果没有逐对缓存：按上次分组推断，不同组即上次已判为非重复。
                urls = [left_url, right_url]
                titles = [
                    normalized_title(rows[left]["文章标题"]),
                    normalized_title(rows[right]["文章标题"]),
                ]
                if urls[0] > urls[1]:
                    urls.reverse()
                    titles.reverse()
                decisions[pair_key] = {
                    "urls": urls,
                    "titles": titles,
                    "duplicate": False,
                    "decided_at": datetime.now(TZ).isoformat(timespec="seconds"),
                }
                continue
        to_ask.append((left, right))
    return confirmed, to_ask, decisions


def row_sort_key(row: dict[str, str]) -> tuple[str, str]:
    return (row.get("发布日期", ""), row.get("文章标题", ""))


def build_date_account_groups(merged_groups: list[list[dict[str, str]]]) -> dict[str, dict[str, list[list[dict[str, str]]]]]:
    """date → account → [groups]，组内按日期/标题倒序。"""
    result: dict[str, dict[str, list[list[dict[str, str]]]]] = defaultdict(dict)
    for group in merged_groups:
        date_text = group[0]["发布日期"]
        account = group[0]["公众号"]
        result[date_text].setdefault(account, []).append(group)
    return result


def write_html(
    path: Path,
    date_account_groups: dict[str, dict[str, list[list[dict[str, str]]]]],
    stats: dict[str, Any],
    clicked: dict[str, str] | None = None,
) -> None:
    esc = html.escape
    clicked = clicked or {}
    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>全部公众号文章汇总（微信读书）</title>",
        "<style>",
        "*{box-sizing:border-box}",
        'body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;',
        "max-width:980px;margin:0 auto;padding:28px 20px 80px;color:#1f2329;background:#f7f8fa;line-height:1.65}",
        "h1{font-size:22px;margin:0 0 6px}",
        ".meta{color:#7d8798;font-size:13px;margin-bottom:8px}",
        ".stats{color:#5b6472;font-size:13px;margin-bottom:26px;padding:10px 14px;background:#fff;border:1px solid #e3e7ee;border-radius:10px}",
        "h2.date{font-size:18px;margin:34px 0 2px;padding-top:22px;border-top:1px solid #e3e7ee}",
        "h3.account{font-size:15px;color:#46506a;margin:16px 0 8px}",
        "ul.articles{list-style:none;margin:0;padding:0}",
        "li.article{margin:7px 0;padding:9px 12px;background:#fff;border:1px solid #e3e7ee;border-radius:9px}",
        "a.main{color:#1458c8;text-decoration:none;font-weight:600;font-size:15px}",
        "a.main:visited{color:#7b4bb8}",
        "a.main:hover{text-decoration:underline}",
        "details.dups{margin-top:6px;font-size:13px}",
        "summary.dups-summary{color:#8a94a6;cursor:pointer;user-select:none;display:inline-block}",
        "summary.dups-summary:hover{color:#5b6472}",
        "ul.dups-list{list-style:none;margin:8px 0 0;padding:9px 12px;background:#f3f5f9;border-radius:7px}",
        "ul.dups-list li{margin:5px 0;font-size:12.5px}",
        ".dup-date{color:#98a2b3;margin-right:8px}",
        "a.dup-link{color:#6d7889;text-decoration:none}",
        "a.dup-link:hover{text-decoration:underline;color:#1458c8}",
        "a.clicked{opacity:.5}",
        "a.clicked::after{content:\" ✓已点\";font-size:11px;color:#9aa3b2;text-decoration:none}",
        ".footer{margin-top:44px;color:#8a94a6;font-size:12px}",
        "</style></head><body>",
        "<h1>全部公众号文章汇总（微信读书）</h1>",
        f'<div class="meta">时间范围：{esc(stats["range_text"])} ｜ 生成于 {esc(stats["generated_at"])}</div>',
        '<div class="stats">',
        (
            f'窗口内原始 {stats["source_rows"]} 条 → 去重后 {stats["groups"]} 组；'
            f'完全同名对 {stats["exact_pairs"]}；前缀归并对 {stats["unwrap_pairs"]}；'
            f'语义重复对 {stats["semantic_pairs"]}'
            + (f'（复用上次 {stats["reused_pairs"]} 对）' if stats.get("reused_pairs") else "")
            + f'；已点链接 {len(clicked)} 条。'
        ),
        "</div>",
    ]
    for date_text in sorted(date_account_groups, reverse=True):
        parts.append(f'<h2 class="date">{esc(date_text)}</h2>')
        for account in sorted(date_account_groups[date_text]):
            parts.append(f'<h3 class="account">{esc(account)}</h3>')
            parts.append('<ul class="articles">')
            for group in date_account_groups[date_text][account]:
                primary = group[0]
                parts.append(
                    f'<li class="article"><a class="main" target="_blank" rel="noopener" '
                    f'href="{esc(primary["文章链接"])}">'
                    f'{esc(primary["文章标题"])}</a>'
                )
                if len(group) > 1:
                    parts.append(
                        f'<details class="dups"><summary class="dups-summary">同题 {len(group)} 篇，点击展开</summary>'
                        '<ul class="dups-list">'
                    )
                    for member in group[1:]:
                        parts.append(
                            f'<li><span class="dup-date">{esc(member.get("发布日期", ""))} · '
                            f'{esc(member.get("公众号", ""))}</span>'
                            f'<a class="dup-link" target="_blank" rel="noopener" '
                            f'href="{esc(member["文章链接"])}">{esc(member["文章标题"])}</a></li>'
                        )
                    parts.append("</ul></details>")
                parts.append("</li>")
            parts.append("</ul>")
    parts.append(
        '<div class="footer">来源：微信读书公众号收录；链接均为 mp.weixin.qq.com 原文直链。'
        "相同/近似文章合并为组，主链接为最新一篇，其余成员链接弱化显示。</div>"
    )
    parts.append(
        "<script>(function(){"
        f"var CLICKED={json.dumps(clicked, ensure_ascii=False)};"
        "var KEY='wr-clicked:';"
        "function key(h){return KEY+h;}"
        "function stamp(){return new Date().toISOString();}"
        "function groupLinks(a){var li=a.closest('li.article');"
        "return li?Array.prototype.slice.call(li.querySelectorAll('a[href^=\"https://mp.weixin.qq.com/\"]')):[a];}"
        "function mark(a){groupLinks(a).forEach(function(x){x.classList.add('clicked');CLICKED[x.href]=stamp();"
        "try{localStorage.setItem(key(x.href),'1');}catch(e){}});}"
        "function applyClicked(){"
        "document.querySelectorAll('a[href^=\"https://mp.weixin.qq.com/\"]').forEach(function(a){"
        "var links=groupLinks(a);var hit=false;var ts='';"
        "links.forEach(function(x){if(Object.prototype.hasOwnProperty.call(CLICKED,x.href)){"
        "hit=true;if((CLICKED[x.href]||'')>ts)ts=CLICKED[x.href]||'';}});"
        "if(!hit){links.forEach(function(x){try{if(localStorage.getItem(key(x.href))==='1'){"
        "hit=true;if(!ts)ts='imported';}}catch(e){}});}"
        "if(hit){links.forEach(function(x){x.classList.add('clicked');"
        "if(!Object.prototype.hasOwnProperty.call(CLICKED,x.href))CLICKED[x.href]=ts||'imported';"
        "try{localStorage.setItem(key(x.href),'1');}catch(e){}});}"
        "});}"
        "applyClicked();"
        "document.querySelectorAll('a[href^=\"https://mp.weixin.qq.com/\"]').forEach(function(a){"
        "a.addEventListener('click',function(){mark(a);});"
        "});"
        "function statePayload(){var out={version:1,updated_at:stamp(),source:'html-export',urls:{}};"
        "document.querySelectorAll('a[href^=\"https://mp.weixin.qq.com/\"]').forEach(function(a){"
        "if(a.classList.contains('clicked'))out.urls[a.href]=CLICKED[a.href]||stamp();});"
        "return JSON.stringify(out,null,2);}"
        "var bar=document.createElement('div');"
        "bar.style.cssText='position:fixed;right:16px;bottom:16px;z-index:99;display:flex;gap:8px;"
        "padding:8px;background:#fff;border:1px solid #d5dae3;border-radius:10px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.08)';"
        "function makeBtn(text,fn){var b=document.createElement('button');b.textContent=text;"
        "b.style.cssText='padding:7px 12px;border:1px solid #d5dae3;border-radius:8px;background:#fff;"
        "color:#5b6472;font-size:13px;cursor:pointer';b.addEventListener('click',fn);return b;}"
        "var btnClear=makeBtn('清除已点标记',function(){"
        "document.querySelectorAll('a.clicked').forEach(function(a){a.classList.remove('clicked');"
        "delete CLICKED[a.href];});"
        "try{Object.keys(localStorage).filter(function(k){return k.indexOf(KEY)===0;})"
        ".forEach(function(k){localStorage.removeItem(k);});}catch(e){}"
        "});"
        "var btnExport=makeBtn('导出已点链接',function(){"
        "try{var blob=new Blob([statePayload()],{type:'application/json'});"
        "var a=document.createElement('a');a.href=URL.createObjectURL(blob);"
        "a.download='已点击链接.json';document.body.appendChild(a);a.click();"
        "setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},1000);}catch(e){alert('导出失败：'+e.message);}"
        "});"
        "var input=document.createElement('input');input.type='file';input.accept='application/json,.json';"
        "input.style.display='none';document.body.appendChild(input);"
        "input.addEventListener('change',function(){var f=input.files[0];if(!f)return;"
        "var reader=new FileReader();reader.onload=function(){try{"
        "var obj=JSON.parse(reader.result);var urls=obj.urls||obj;"
        "Object.keys(urls).forEach(function(u){if(u.indexOf('https://mp.weixin.qq.com/')===0){"
        "CLICKED[u]=urls[u]||stamp();try{localStorage.setItem(key(u),'1');}catch(e){}}});"
        "applyClicked();"
        "}catch(e){alert('导入失败：'+e.message);}};reader.readAsText(f);input.value='';});"
        "var btnImport=makeBtn('导入已点链接',function(){input.click();});"
        "bar.appendChild(btnClear);bar.appendChild(btnExport);bar.appendChild(btnImport);"
        "document.body.appendChild(bar);"
        "})();</script>"
    )
    parts.append("</body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--range", default=None, help="YYYYMMDD-YYYYMMDD；缺省为 20260710 起全部结果")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--html-dir", type=Path, default=HTML_DIR)
    parser.add_argument("--api-key", default=None, help="覆盖 DEDUP_API_KEY / DEEPSEEK_API_KEY")
    parser.add_argument("--max-pairs", type=int, default=2000, help="每次最多发送给 DeepSeek 的新候选对数（按相似度取前 N）")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("WR_DEDUP_CONCURRENCY", "4")),
        help="语义去重请求并发数（默认 4，环境变量 WR_DEDUP_CONCURRENCY 可配；API 端点可承受一定并发）",
    )
    parser.add_argument("--no-reuse-groups", action="store_true", help="忽略上次 汇总-去重组.json，重新全量判断语义重复")
    args = parser.parse_args()
    api_base, model, env_api_key = dedup_config()
    api_key = args.api_key or env_api_key
    if not api_key:
        print("  未提供 API 密钥，将不带 Authorization 调用（适用于无需鉴权的本地端点）。", flush=True)

    lo, hi = parse_range(args.range)
    rows = [row for row in read_account_rows(args.input_dir) if in_window(row, lo, hi)]
    rows.sort(key=row_sort_key, reverse=True)
    if not rows:
        raise SystemExit("窗口内没有数据（检查 --range 或先运行提取脚本）。")
    write_summary_csv(args.output_dir / "汇总-全部公众号.csv", rows)

    by_title: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_title[normalized_title(row["文章标题"])].append(index)
    exact_pairs: set[tuple[int, int]] = set()
    for indexes in by_title.values():
        for offset, left in enumerate(indexes):
            exact_pairs.update((left, right) for right in indexes[offset + 1 :])

    # 确定性前缀归并：剥掉转发前缀/启动后缀后同名（但原文标题不同）的对直接合并。
    by_unwrap: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        unwrapped = unwrap_title(normalized_title(row["文章标题"]))
        if len(unwrapped) >= UNWRAP_MIN_LEN:
            by_unwrap[unwrapped].append(index)
    wrap_pairs: set[tuple[int, int]] = set()
    for indexes in by_unwrap.values():
        for offset, left in enumerate(indexes):
            wrap_pairs.update((left, right) for right in indexes[offset + 1 :])
    wrap_pairs -= exact_pairs
    if wrap_pairs:
        print(f"  前缀归并：{len(wrap_pairs)} 对仅差转发前缀/启动后缀，直接合并。", flush=True)

    candidates = [
        pair
        for pair in candidate_pairs(rows, exact_pairs)
        if pair not in wrap_pairs
    ][: args.max_pairs]
    reused_semantic_pairs: set[tuple[int, int]] = set()
    pairs_to_ask: list[tuple[int, int]] = list(candidates)
    decision_cache: dict[tuple[str, str], dict[str, Any]] = {}
    if args.no_reuse_groups:
        _, decision_cache = load_previous_groups(args.output_dir / "汇总-去重组.json")
    else:
        reused_semantic_pairs, pairs_to_ask, decision_cache = reuse_previous_semantic_pairs(
            rows, candidates, args.output_dir / "汇总-去重组.json"
        )
    reused_semantic_pairs -= exact_pairs
    semantic_pairs: set[tuple[int, int]] = set(reused_semantic_pairs)
    # 前缀归并对写回判定缓存（覆盖历史上可能的「非重复」结论），后续复用才一致。
    for left, right in wrap_pairs:
        urls = [rows[left]["文章链接"], rows[right]["文章链接"]]
        titles = [
            normalized_title(rows[left]["文章标题"]),
            normalized_title(rows[right]["文章标题"]),
        ]
        if urls[0] > urls[1]:
            urls.reverse()
            titles.reverse()
        decision_cache[tuple(urls)] = {
            "urls": urls,
            "titles": titles,
            "duplicate": True,
            "rule": "unwrap",
            "decided_at": datetime.now(TZ).isoformat(timespec="seconds"),
        }
    if pairs_to_ask:
        new_semantic_pairs = deepseek_pairs(rows, pairs_to_ask, api_base, model, api_key, args.concurrency)
        semantic_pairs |= new_semantic_pairs
        now = datetime.now(TZ).isoformat(timespec="seconds")
        for left, right in pairs_to_ask:
            urls = [rows[left]["文章链接"], rows[right]["文章链接"]]
            titles = [
                normalized_title(rows[left]["文章标题"]),
                normalized_title(rows[right]["文章标题"]),
            ]
            if urls[0] > urls[1]:
                urls.reverse()
                titles.reverse()
            decision_cache[tuple(urls)] = {
                "urls": urls,
                "titles": titles,
                "duplicate": (left, right) in semantic_pairs,
                "decided_at": now,
            }
        print(
            f"  复用上次语义结果 {len(reused_semantic_pairs)} 对，"
            f"本次新增判断 {len(new_semantic_pairs)} 对。",
            flush=True,
        )
    else:
        print(
            f"  候选对均可复用上次分组结果，未调用 DeepSeek（复用 {len(reused_semantic_pairs)} 对）。",
            flush=True,
        )
    semantic_pairs -= exact_pairs

    # 品牌硬否决（中心位置）：对“完全同名 + 语义重复（含从上次分组复用的确认对）”的
    # 全部合并对做一次品牌主体校验，否决招聘主体不同的对。这一步同时修正历史误合并——
    # 上次分组里 26 条的阿里大组就是被“同组即复用”的传递闭包带进来的。
    all_pairs = exact_pairs | semantic_pairs | wrap_pairs
    row_brands = [effective_brands(row["文章标题"]) for row in rows]
    vetoed_pairs = {
        pair
        for pair in all_pairs
        if brand_veto_sets(row_brands[pair[0]], row_brands[pair[1]])
    }
    if vetoed_pairs:
        print(f"  品牌否决：{len(vetoed_pairs)} 对招聘主体不同，强制不合并。", flush=True)
        now_text = datetime.now(TZ).isoformat(timespec="seconds")
        for left, right in vetoed_pairs:
            urls = [rows[left]["文章链接"], rows[right]["文章链接"]]
            titles = [
                normalized_title(rows[left]["文章标题"]),
                normalized_title(rows[right]["文章标题"]),
            ]
            if urls[0] > urls[1]:
                urls.reverse()
                titles.reverse()
            decision_cache[tuple(urls)] = {
                "urls": urls,
                "titles": titles,
                "duplicate": False,
                "veto": "brand",
                "decided_at": now_text,
            }
        all_pairs -= vetoed_pairs

    groups = UnionFind(len(rows))
    for left, right in all_pairs:
        groups.union(left, right)
    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        components[groups.find(index)].append(index)
    merged_groups = [
        sorted([rows[i] for i in indexes], key=row_sort_key, reverse=True)
        for indexes in components.values()
    ]
    merged_groups.sort(key=lambda group: row_sort_key(group[0]), reverse=True)

    # 事后审计：理论上品牌否决后组内主体应当一致；若仍出现多品牌混组（别名表未覆盖的
    # 写法、或模型把不同公司判重），打印出来让人工检查，避免静默错分。
    mixed_groups = []
    for group in merged_groups:
        brands: set[str] = set()
        for member in group:
            effective = effective_brands(member["文章标题"])
            if len(effective) == 1:
                brands |= effective
        if len(brands) > 1:
            mixed_groups.append((brands, group[0]["文章标题"], len(group)))
    if mixed_groups:
        print(f"  ⚠️ 有 {len(mixed_groups)} 个组内出现多个招聘主体（疑似仍误合并，请检查）：", flush=True)
        for brands, title, size in mixed_groups[:10]:
            print(f"     {'/'.join(sorted(brands))} × {size} 条 ← {title[:40]}", flush=True)

    clicked = propagate_clicked_in_groups(collect_clicked_state(args.html_dir), merged_groups)
    save_clicked_state(clicked, source="collect+group-propagation")

    canonical_by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    for group in merged_groups:
        canonical_by_date[group[0]["发布日期"]].append(group)

    date_account_groups = build_date_account_groups(merged_groups)
    semantic_summary = f"语义重复对 {len(semantic_pairs)}"
    if reused_semantic_pairs:
        semantic_summary += f"（复用上次 {len(reused_semantic_pairs)} 对）"
    lines = [
        "# 全部公众号文章汇总（微信读书）",
        "",
        f"时间范围：{lo[:4]}-{lo[4:6]}-{lo[6:]} ~ {hi[:4]}-{hi[4:6]}-{hi[6:]}"
        if args.range
        else f"全部结果（自 2026-07-10 起，不截断）",
        f"窗口内原始 {len(rows)} 条；合并同名后 {len(merged_groups)} 组；"
        f"完全同名候选对 {len(exact_pairs)}；前缀归并对 {len(wrap_pairs)}；{semantic_summary}。",
        "",
    ]
    for date_text in sorted(canonical_by_date, reverse=True):
        lines.append(f"## {date_text}")
        lines.append("")
        by_account: dict[str, list[list[dict[str, str]]]] = defaultdict(list)
        for group in canonical_by_date[date_text]:
            by_account[group[0]["公众号"]].append(group)
        for account in sorted(by_account):
            lines.append(f"### {account}")
            lines.append("")
            for group in by_account[account]:
                primary = group[0]
                if len(group) == 1:
                    lines.append(f"- [{primary['文章标题']}]({primary['文章链接']})")
                else:
                    extras = "、".join(
                        f"{member.get('发布日期', '')} · {member.get('公众号', '')} "
                        f"[{member['文章标题']}]({member['文章链接']})"
                        for member in group[1:]
                    )
                    lines.append(f"- [{primary['文章标题']}]({primary['文章链接']})")
                    lines.append(f"  - 同题 {len(group)} 篇：{extras}")
            lines.append("")
        lines.append("")
    lines.append("")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.output_dir / "汇总-全部公众号.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    stats = {
        "range_text": f"{lo[:4]}-{lo[4:6]}-{lo[6:]} ~ {hi[:4]}-{hi[4:6]}-{hi[6:]}"
        if args.range
        else f"全部结果（自 2026-07-10 起，不截断）",
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "source_rows": len(rows),
        "groups": len(merged_groups),
        "exact_pairs": len(exact_pairs),
        "unwrap_pairs": len(wrap_pairs),
        "semantic_pairs": len(semantic_pairs),
        "reused_pairs": len(reused_semantic_pairs),
        "clicked": len(clicked),
    }
    html_path = args.html_dir / f"微信读书汇总-{lo}-{hi}.html"
    write_html(html_path, date_account_groups, stats, clicked)
    (args.output_dir / "汇总-去重组.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
                "range": {"start": lo, "end": hi},
                "source_rows": len(rows),
                "exact_pairs": len(exact_pairs),
                "unwrap_pairs": len(wrap_pairs),
                "semantic_pairs": len(semantic_pairs),
                "reused_pairs": len(reused_semantic_pairs),
                "decided_pairs": list(decision_cache.values()),
                "groups": [
                    {
                        "canonical": group[0],
                        "members": group,
                    }
                    for group in merged_groups
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "range": f"{lo}-{hi}",
                "source_rows": len(rows),
                "groups": len(merged_groups),
                "exact_title_pairs": len(exact_pairs),
                "semantic_pairs_confirmed": len(semantic_pairs),
                "semantic_pairs_reused": len(reused_semantic_pairs),
                "markdown": str(md_path),
                "html": str(html_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
