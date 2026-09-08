#!/usr/bin/env python3
"""对汇总去重后的招聘文章做 AI 筛选（识别计算机类岗位）。

用法:
    uv run python web/screen_update.py 2026-08-31          # 筛选某一天（已分析过的组复用旧结果，只补缺）
    uv run python web/screen_update.py --all               # 批量补筛全部日期（每日更新后跑一次兜底）
    uv run python web/screen_update.py 2026-08-31 --force  # 忽略旧结果，重新分析该日期
    uv run python web/screen_update.py --all --force       # 全部重析（花费大，慎用）

流程:
  1. 直接读取 output/weread_extract/汇总-去重组.json（与网页服务同源）；
  2. 逐组抓取微信文章：正文文本、图片、阅读原文链接（msg_source_url）；
  3. 正文较短（海报式推送）时下载图片，zxing-cpp 解码二维码（整图→缩图→滑窗）；
  4. 调 GLM 结构化识别，判定是否招软件工程/计算机类岗位，产出岗位/地点/报名方式；
  5. 结果合并写入 web/data/screen_results.json（按日期组织 kept/skipped），逐日落盘、
     断点可续，网页服务监听该文件变化、下次请求自动读到新结果；
  6. 已分析过的同链接/同标题文章（含跨日期重复）直接复用旧结果，不重复识别。

抓取、二维码解码与 GLM 识别的具体实现在同目录 screen_lib.py；本文件只做按日期的
编排、结果复用与落盘。

分析结果对所有用户通用；每位用户在网页端保存的意向城市/方向关键词只影响
自己的高亮与「符合我的筛选」视图，不改变这里的判定。

GLM 端点复用 .env 里的 DEDUP_API_BASE / DEDUP_MODEL / DEDUP_API_KEY，
也可用 SCREEN_API_BASE / SCREEN_MODEL / SCREEN_API_KEY 单独指定。
并发数默认 10，可用环境变量 WR_SCREEN_CONCURRENCY 或 --concurrency 调整。
zxing-cpp 与 pillow 未安装时自动降级为纯文本分析（海报式推送不读图）。
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from screen_lib import (  # noqa: F401  （sanitize_apply_url 供测试导入）
    Image,
    POSTER_MIN,
    api_config,
    concurrency_config,
    decode_qr,
    download_images,
    glm_analyze,
    sanitize_apply_url,
    scrape_article,
    slice_image,
    zxingcpp,
)
from stores import ArticleStore

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SOURCE_PATH = PROJECT_ROOT / "output" / "weread_extract" / "汇总-去重组.json"
RESULTS_PATH = HERE / "data" / "screen_results.json"
CACHE_ROOT = HERE / "data" / "screen-cache"


# ---------- 结果复用（跨日期的重复文章不再分析） ----------

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
    for url in entry.get("urls") or ([entry["article_url"]] if entry.get("article_url") else []):
        by_url.setdefault(url, (kind, entry))
    title = (entry.get("title") or "").strip()
    if title:
        by_title.setdefault(title, (kind, entry))


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
    """抓取并分析一个组，返回 (保留?, 条目)。图片缓存放该组专属子目录，避免并发互相覆盖。"""
    info = scrape_article(group["url"])
    local_imgs: list = []
    qr_urls: list[str] = []
    if len(info["text"]) < POSTER_MIN and Image is not None and zxingcpp is not None:
        for path in download_images(info["images"], day_cache / group["id"][:12] / "img"):
            local_imgs.extend(slice_image(path))
            for u in decode_qr(path):
                if u not in qr_urls:
                    qr_urls.append(u)
    info["qr_urls"] = qr_urls
    parsed = glm_analyze(api_base, model, api_key, date, group["account"], group["title"], info, local_imgs)
    accounts = list(dict.fromkeys(
        [group["account"]] + [m["account"] for m in group["members"]]))
    if parsed.get("保留"):
        return True, {
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
        }
    return False, {
        "account": group["account"],
        "title": group["title"],
        "article_url": group["url"],
        "urls": group["urls"],
        "reason": parsed.get("跳过原因") or "非计算机类岗位",
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


def save_results(results: dict) -> None:
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
        if hit is not None:
            kind, entry = hit
            merged = merge_entry(entry, group)
            (kept if kind == "kept" else skipped).append(merged)
            index_add(by_url, by_title, kind, merged)
        else:
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
        print("提示：未安装 zxing-cpp / pillow，海报式推送将不读图、不解码二维码。", flush=True)
    api_base, model, api_key = api_config()
    print(f"GLM 端点：{api_base}（模型 {model}）", flush=True)

    # 每个日期都过一遍：已分析过的组由复用索引秒回，真正没筛过的组才送去识别。
    # 每日更新会把新链接回填到 14 天窗口内的旧日期，因此不能按「日期已有结果」整日跳过，
    # 否则回填到旧日期的文章永远筛不到；全量重析仍用 --force。
    overall_total = sum(len(groups_by_date.get(date, [])) for date in dates)

    offset = 0
    for date in dates:
        day_groups = groups_by_date.get(date, [])
        screen_date_concurrent(date, day_groups, results, by_url, by_title,
                               api_base, model, api_key, concurrency, offset, overall_total)
        offset += len(day_groups)

    kept_total = sum(len(day.get("kept", [])) for day in results["days"].values())
    skip_total = sum(len(day.get("skipped", [])) for day in results["days"].values())
    print(f"全部完成：累计保留 {kept_total}，跳过 {skip_total}", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
