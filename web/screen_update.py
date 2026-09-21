#!/usr/bin/env python3
"""对汇总去重后的招聘文章做 AI 筛选与结构化提取（不限专业方向，另识别宣讲会）。

用法:
    uv run python web/screen_update.py 2026-08-31          # 筛选某一天（可复用的旧结果直接沿用，只补缺/重析过时项）
    uv run python web/screen_update.py --all               # 批量筛全部日期（每日更新后跑一次兜底）
    uv run python web/screen_update.py 2026-08-31 --force  # 忽略旧结果，重新分析该日期
    uv run python web/screen_update.py --all --force       # 全部重析（花费大，慎用）

流程:
  1. 直接读取 output/weread_extract/汇总-去重组.json（与网页服务同源）；
  2. 逐组抓取微信文章：正文文本、图片、阅读原文链接（msg_source_url）；
     抓取命中微信验证/删除页时退避重试，仍失败记为可重试失败条目（下次运行自动补析）；
  3. 有图必读：下载全部文章图片（带上限），zxing-cpp 解码二维码，长图切片后连同正文
     一起送 GLM（不再按正文字数决定是否读图——岗位表和宣讲会时间地点常在图片里）；
  4. 调 GLM 结构化识别：凡招聘信息（任意专业方向）都保留并完整提取岗位/地点/报名方式，
     另提取宣讲会信息（是否宣讲会/双选会/组团招聘、时间、地点、多公司），仅纯活动通知、
     政策宣传等非招聘内容跳过；
  5. 结果合并写入 web/data/screen_results.json（按日期组织 kept/skipped），带 v:2 版本
     标记，逐日落盘、断点可续，网页服务监听该文件变化、下次请求自动读到新结果；
  6. 已分析过的同链接/同标题文章（含跨日期重复）直接复用旧结果，不重复识别。
     复用规则见 is_reusable：v1 旧结果里"被旧标准跳过的招聘"与"缺宣讲会字段的宣讲类文章"
     会在常规运行中自动重析，无需 --force（首次升级后的一次 --all 会补析较多旧条目，属预期）。

抓取、图片处理、二维码解码、宣讲会解析与 GLM 识别的具体实现在同目录 screen_lib.py；
本文件只做按日期的编排、结果复用与落盘。

分析结果对所有用户通用；每位用户在网页端保存的意向城市/方向关键词只影响
自己的高亮与「符合我的筛选」视图，不改变这里的判定。

GLM 端点复用 .env 里的 DEDUP_API_BASE / DEDUP_MODEL / DEDUP_API_KEY，
也可用 SCREEN_API_BASE / SCREEN_MODEL / SCREEN_API_KEY 单独指定。
并发数默认 10，可用环境变量 WR_SCREEN_CONCURRENCY 或 --concurrency 调整。
zxing-cpp 与 pillow 未安装时自动降级为纯文本分析（不读图、不解码二维码）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from screen_lib import (  # noqa: F401  （sanitize_apply_url 供测试导入）
    Image,
    zxingcpp,
    api_config,
    build_fair,
    collect_image_inputs,
    concurrency_config,
    glm_analyze,
    sanitize_apply_url,
    scrape_article,
)
from stores import ArticleStore

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SOURCE_PATH = PROJECT_ROOT / "output" / "weread_extract" / "汇总-去重组.json"
RESULTS_PATH = HERE / "data" / "screen_results.json"
CACHE_ROOT = HERE / "data" / "screen-cache"
CACHE_RETENTION_DAYS = 14   # 图片缓存只服务当次分析（重析会重新抓取），过期即清理
SAVE_INTERVAL_SECONDS = 60  # 结果落盘节流：断点粒度从"每日期"放宽到"每分钟"，结束时强制落盘


def cleanup_cache() -> None:
    """删除过期的图片缓存目录。有图必读之后缓存增长很快，而分析完成后图片
    几乎不会复用（重析会重新抓页面），留着只占磁盘。
    按目录名（YYYYMMDD）判断而不是 mtime：重析会在旧日期目录下新建子目录，
    把目录 mtime 刷成当天。"""
    if not CACHE_ROOT.exists():
        return
    cutoff = (datetime.now() - timedelta(days=CACHE_RETENTION_DAYS)).strftime("%Y%m%d")
    removed = 0
    for child in CACHE_ROOT.iterdir():
        try:
            if child.is_dir() and child.name.isdigit() and len(child.name) == 8 and child.name < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        print(f"已清理 {removed} 个超过 {CACHE_RETENTION_DAYS} 天的图片缓存目录", flush=True)


# ---------- 结果复用（跨日期的重复文章不再分析） ----------

# 标题命中这些关键词的文章按宣讲会处理：v1 旧结果缺宣讲会字段，需要按新 prompt 重析；
# 前端对未筛选/旧数据也用同一组关键词兜底识别宣讲会。
FAIR_TITLE_RE = re.compile(r"宣讲|双选|组团|线下招聘|招聘会")

# 现行结果 schema 版本：v2 = 不分计算机类、全量读图、带宣讲会字段。
# v1 结果按 is_reusable 的规则部分复用，其余在常规 --all 运行中自动补析（无需 --force）。
RESULT_VERSION = 2


def is_reusable(kind: str, entry: dict, title: str) -> bool:
    """判断一条已有结果能否直接复用。

    - v2 结果：可复用；
    - v1 kept（旧 prompt，但提取字段同构）且标题不含宣讲关键词：可复用；
    - v1 skipped（旧标准把非计算机类招聘错误跳过、且没有提取信息）、
      失败条目（无 v 标记，需人工复核/重试）、标题命中宣讲关键词的 v1 kept：重析。
    """
    if entry.get("v") == RESULT_VERSION:
        return True
    return kind == "kept" and not FAIR_TITLE_RE.search(title)


def build_reuse_index(results: dict) -> tuple[dict, dict]:
    """返回 (url -> (kind, entry), 标题 -> (kind, entry))，基于已有全部结果。"""
    by_url: dict[str, tuple[str, dict]] = {}
    by_title: dict[str, tuple[str, dict]] = {}
    for day in results.get("days", {}).values():
        for kind in ("kept", "skipped"):
            for entry in day.get(kind, []):
                pair = (kind, entry)
                for url in entry.get("urls") or ([entry["article_url"]] if entry.get("article_url") else []):
                    by_url.setdefault(url, pair)
                title = (entry.get("title") or "").strip()
                if title:
                    by_title.setdefault(title, pair)
    return by_url, by_title


def index_lookup(by_url: dict, by_title: dict, group: dict) -> tuple[str, dict] | None:
    for url in group["urls"]:
        if url in by_url:
            return by_url[url]
    return by_title.get(group["title"].strip())


def index_add(by_url: dict, by_title: dict, kind: str, entry: dict) -> None:
    # 直接覆盖而不是 setdefault：重析后的新结果要替换索引里的旧 v1 条目，
    # 否则同链接的跨日期重复组还会复用到已判定过时的结果。
    for url in entry.get("urls") or ([entry["article_url"]] if entry.get("article_url") else []):
        by_url[url] = (kind, entry)
    title = (entry.get("title") or "").strip()
    if title:
        by_title[title] = (kind, entry)


def merge_entry(entry: dict, group: dict) -> dict:
    """复用旧结果时并入当前组的来源与链接，保证网页端两边都能对上。"""
    merged = dict(entry)
    merged["urls"] = sorted(set(entry.get("urls", [])) | set(group["urls"]))
    merged["accounts"] = list(dict.fromkeys(
        entry.get("accounts", []) + [group["account"]] + [m["account"] for m in group["members"]]))
    return merged


# ---------- 主流程 ----------

def analyze_group(api_base: str, model: str, api_key: str,
                  date: str, group: dict, day_cache: Path) -> tuple[bool, dict]:
    """抓取并分析一个组，返回 (保留?, 条目)。图片缓存放该组专属子目录，避免并发互相覆盖。

    抓取被微信验证页拦截时抛 FetchBlockedError，由上层记为可重试的失败条目。
    """
    info = scrape_article(group["url"])
    local_imgs, qr_urls = collect_image_inputs(info["images"], day_cache / group["id"][:12] / "img")
    info["qr_urls"] = qr_urls
    parsed = glm_analyze(api_base, model, api_key, date, group["account"], group["title"], info, local_imgs)
    accounts = list(dict.fromkeys(
        [group["account"]] + [m["account"] for m in group["members"]]))
    if parsed.get("保留"):
        entry = {
            "unit": parsed.get("招聘单位") or group["title"],
            "intro": parsed.get("摘要") or "",
            "title": group["title"],
            "accounts": accounts,
            "recruit_target": parsed.get("招聘对象") or "",
            "positions": [
                {"name": p.get("岗位", ""), "category": p.get("类别", ""), "location": p.get("地点", "")}
                for p in (parsed.get("岗位列表") or [])
            ],
            "locations": parsed.get("工作地点") or "",
            "apply_url": sanitize_apply_url(parsed.get("报名方式") or "", info["readmore"]),
            "qr_decoded": qr_urls,
            "article_url": group["url"],
            "urls": group["urls"],
            "note": "",
            "v": RESULT_VERSION,
        }
        fair = build_fair(parsed, date, accounts)
        if fair:
            entry["fair"] = fair
        return True, entry
    return False, {
        "account": group["account"],
        "title": group["title"],
        "article_url": group["url"],
        "urls": group["urls"],
        "reason": parsed.get("跳过原因") or "非招聘信息",
        "v": RESULT_VERSION,
    }


def failure_entry(group: dict, err: Exception) -> dict:
    return {
        "account": group["account"],
        "title": group["title"],
        "article_url": group["url"],
        "urls": group["urls"],
        "reason": "自动分析失败，需人工复核：" + str(err)[:120],
    }


def load_results() -> dict:
    if RESULTS_PATH.exists():
        try:
            results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
            if isinstance(results, dict) and isinstance(results.get("days"), dict):
                return results
        except (OSError, json.JSONDecodeError) as err:
            print(f"已有结果文件读取失败（将重建）：{err}", flush=True)
    return {"days": {}}


_last_save = 0.0


def save_results(results: dict, force: bool = False) -> None:
    """落盘筛选结果；按时间节流（全量重写整个文件，逐日期都写是 O(天数×体积)）。

    force=True 在运行结束/异常退出时调用，保证断点不丢：中途被杀最多丢一个
    节流窗口内的进度，下次运行会自动补析。
    """
    global _last_save
    if not force and time.time() - _last_save < SAVE_INTERVAL_SECONDS:
        return
    _last_save = time.time()
    results["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def screen_date_concurrent(date: str, day_groups: list[dict], results: dict, by_url: dict, by_title: dict,
                           api_base: str, model: str, api_key: str, concurrency: int,
                           overall_offset: int, overall_total: int) -> None:
    """screen_date 的并发版本：pending 组交给线程池，完成即打印进度，全部结束后落盘。"""
    kept: list[dict] = []
    skipped: list[dict] = []
    pending: list[dict] = []
    for group in day_groups:
        hit = index_lookup(by_url, by_title, group)
        if hit is not None and is_reusable(hit[0], hit[1], group["title"]):
            kind, entry = hit
            merged = merge_entry(entry, group)
            (kept if kind == "kept" else skipped).append(merged)
            index_add(by_url, by_title, kind, merged)
        else:
            # 旧 v1 结果不可直接复用（旧标准跳过的招聘、失败条目、缺宣讲会字段的宣讲类）
            pending.append(group)

    done = 0
    day_cache = CACHE_ROOT / date.replace("-", "")

    def report(group: dict, tail: str) -> None:
        nonlocal done
        done += 1
        prefix = f"[{overall_offset + done}/{overall_total}]" if overall_total else f"[{done}/{len(pending)}]"
        print(f"{prefix} {date} {group['account']} | {group['title']}", flush=True)
        print(f"    → {tail}", flush=True)

    if pending:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(analyze_group, api_base, model, api_key, date, group, day_cache): group
                for group in pending
            }
            for future in as_completed(futures):
                group = futures[future]
                try:
                    keep, entry = future.result()
                except Exception as err:
                    keep, entry = False, failure_entry(group, err)
                    report(group, f"分析失败: {str(err)[:100]}")
                else:
                    action = "保留" if keep else "跳过"
                    detail = entry.get("unit") if keep else entry.get("reason", "")[:50]
                    report(group, f"{action}：{detail}")
                # 无论成败都要落盘：失败组记为需人工复核的 skipped，不能静默丢弃
                (kept if keep else skipped).append(entry)
                index_add(by_url, by_title, "kept" if keep else "skipped", entry)

    results["days"][date] = {"kept": kept, "skipped": skipped}
    save_results(results)
    print(f"{date} 完成：保留 {len(kept)}，跳过 {len(skipped)}（其中复用 {len(day_groups) - len(pending)} 条）", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按日期或全量对去重后的招聘文章做 AI 筛选")
    parser.add_argument("date", nargs="?", help="YYYY-MM-DD；与 --all 二选一")
    parser.add_argument("--all", action="store_true", help="批量筛选全部日期（已有结果的日期跳过）")
    parser.add_argument("--force", action="store_true", help="重新分析并覆盖（默认复用已有结果）")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="抓取与 GLM 识别的并发数（默认 10，或环境变量 WR_SCREEN_CONCURRENCY）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.all and not args.date:
        print("用法: python web/screen_update.py <YYYY-MM-DD> [--force] 或 --all")
        return 2
    if args.date and not args.all:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print("日期格式应为 YYYY-MM-DD")
            return 2

    concurrency = concurrency_config(args.concurrency)
    payload, _ = ArticleStore(SOURCE_PATH).load()
    groups_by_date: dict[str, list[dict]] = {}
    for group in payload["groups"]:
        groups_by_date.setdefault(group["date"], []).append(group)

    if args.all:
        dates = sorted(groups_by_date)
        print(f"批量模式：共 {len(dates)} 个日期、{sum(len(v) for v in groups_by_date.values())} 组，并发 {concurrency}", flush=True)
    else:
        dates = [args.date]
        print(f"{args.date} 共 {len(groups_by_date.get(args.date, []))} 组，并发 {concurrency}", flush=True)

    results = load_results()
    # --force 时忽略历史结果（完全重析）；正常运行时基于已有结果复用，只补缺
    if args.force:
        by_url, by_title = {}, {}
    else:
        by_url, by_title = build_reuse_index(results)

    if Image is None or zxingcpp is None:
        print("提示：未安装 zxing-cpp / pillow，文章图片将不读图、不解码二维码。", flush=True)
    api_base, model, api_key = api_config()
    print(f"GLM 端点：{api_base}（模型 {model}）", flush=True)

    # 每个日期都过一遍：已分析过的组由复用索引秒回，真正没筛过的组才送去识别。
    # 每日更新会把新链接回填到 14 天窗口内的旧日期，因此不能按「日期已有结果」整日跳过，
    # 否则回填到旧日期的文章永远筛不到；全量重析仍用 --force。
    overall_total = sum(len(groups_by_date.get(date, [])) for date in dates)

    cleanup_cache()

    offset = 0
    try:
        for date in dates:
            day_groups = groups_by_date.get(date, [])
            screen_date_concurrent(date, day_groups, results, by_url, by_title,
                                   api_base, model, api_key, concurrency, offset, overall_total)
            offset += len(day_groups)
    finally:
        # 结束或异常中断都强制落盘一次，保住断点
        save_results(results, force=True)

    kept_total = sum(len(day.get("kept", [])) for day in results["days"].values())
    skip_total = sum(len(day.get("skipped", [])) for day in results["days"].values())
    print(f"全部完成：累计保留 {kept_total}，跳过 {skip_total}", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
