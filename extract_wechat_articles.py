#!/usr/bin/env python3
"""从微信桌面端导出的 Share Data SQLite 缓存中提取公众号文章链接。

macOS 可自动发现微信缓存；Linux/Windows 请通过 --database 明确指定
复制到本机的 SQLite 文件。微信 Linux 客户端没有与 macOS 相同的缓存路径。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ACCOUNTS = [
    "成功就业",
    "国科大就业",
    "北航就业",
    "成电就业",
    "北大就业",
    "西安交大就业创业",
    "川大就业",
    "人大就业创业",
    "浙大就业",
    "西电科大就业指导服务中心",
    "西工大就业",
]
STATE_FILE_NAME = "已发现链接.json"
DAILY_DIR_NAME = "每日更新"
SUPPLEMENTS_FILE_NAME = "补充链接.json"
ACCOUNTS_FILE_NAME = "目标公众号.txt"
LEDGER_FILE_NAME = "扫描运行台账.json"


def find_macos_share_data_files() -> list[Path]:
    profile_root = (
        Path.home()
        / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles"
    )
    return sorted(profile_root.glob("*/Share Data"))


def read_rows(database: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="wechat-job-links-") as temp_dir:
        snapshot = Path(temp_dir) / "share-data.sqlite"
        shutil.copy2(database, snapshot)
        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            query = "SELECT url, author, share_data FROM share_data_table"
            for url, author, share_data in connection.execute(query):
                try:
                    if isinstance(share_data, bytes):
                        share_data = share_data.decode("utf-8")
                    metadata = json.loads(share_data or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                    metadata = {}

                title = str(metadata.get("title", "")).strip()
                account = str(author or metadata.get("brandName", "")).strip()
                short_url = str(url or "").strip()
                if title and account and short_url.startswith("https://mp.weixin.qq.com/"):
                    rows.append(
                        {
                            "公众号": account,
                            "文章标题": title,
                            "文章链接": short_url,
                        }
                    )
        finally:
            connection.close()
    return rows


def deduplicate(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        unique[row["文章链接"]] = row
    return sorted(unique.values(), key=lambda row: (row["公众号"], row["文章标题"]))


def normalized_title(value: str) -> str:
    """Normalize only presentation differences for ledger reconciliation."""
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).lower()


def read_ledger_publication_dates(path: Path) -> dict[tuple[str, str], str]:
    """Return unambiguous UI-confirmed dates keyed by account and title.

    A title can legitimately be reused by one account on different dates.  Such
    ambiguous ledger entries are intentionally not backfilled automatically.
    """
    if not path.exists():
        return {}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    candidates: dict[tuple[str, str], set[str]] = {}
    for account, account_data in (ledger.get("accounts") or {}).items():
        if not isinstance(account, str) or not isinstance(account_data, dict):
            continue
        for article in account_data.get("articles") or []:
            if not isinstance(article, dict):
                continue
            title = str(article.get("title", "")).strip()
            publication_date = str(article.get("publication_date", "")).strip()
            if title and re.fullmatch(r"\d{4}-\d{2}-\d{2}", publication_date):
                key = (account.strip(), normalized_title(title))
                candidates.setdefault(key, set()).add(publication_date)
    return {key: next(iter(dates)) for key, dates in candidates.items() if len(dates) == 1}


def enrich_publication_dates(
    rows: list[dict[str, str]],
    records: dict[str, dict[str, str]],
    ledger_dates: dict[tuple[str, str], str],
) -> None:
    """Prefer durable data, then copy an unambiguous UI date from the ledger."""
    for row in rows:
        url = str(row.get("文章链接", "")).strip()
        existing = records.get(url, {})
        date = str(row.get("发布日期", "")).strip() or str(
            existing.get("发布日期", "")
        ).strip()
        if not date:
            key = (str(row.get("公众号", "")).strip(), normalized_title(str(row.get("文章标题", ""))))
            date = ledger_dates.get(key, "")
        if date:
            row["发布日期"] = date


def read_supplemental_rows(path: Path) -> list[dict[str, str]]:
    """读取未进入 Share Data 表、但已从微信浏览器缓存恢复的永久链接。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [
        row
        for row in data
        if isinstance(row, dict)
        and row.get("公众号")
        and row.get("文章标题")
        and str(row.get("文章链接", "")).startswith("https://mp.weixin.qq.com/")
    ]


def classify(title: str) -> str:
    lowered = title.lower()
    if "实习" in title:
        return "实习"
    if "双选" in title or "宣讲" in title:
        return "双选会/宣讲会"
    if "校招" in title or "校园招聘" in title or "应届生" in title:
        return "校园招聘"
    if "人才" in title or "博士" in title or "开放日" in title or "openday" in lowered:
        return "人才引进/开放日"
    return "招聘"


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["公众号", "发布日期", "首次发现日期", "类型", "文章标题", "文章链接"]
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def publication_sort_key(row: dict[str, str]) -> tuple[str, str, str]:
    """Keep newest published articles first; unknown dates follow dated items."""
    published = str(row.get("发布日期", "")).strip()
    return (published, str(row.get("首次发现日期", "")), str(row.get("文章标题", "")))


def sorted_by_publication_desc(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    dated = [row for row in rows if str(row.get("发布日期", "")).strip()]
    undated = [row for row in rows if not str(row.get("发布日期", "")).strip()]
    return sorted(dated, key=publication_sort_key, reverse=True) + sorted(
        undated,
        key=lambda row: (str(row.get("首次发现日期", "")), str(row.get("文章标题", ""))),
        reverse=True,
    )


def write_markdown(path: Path, rows: list[dict[str, str]], extracted_at: str) -> None:
    lines = [
        "# 微信公众号招聘链接",
        "",
        f"提取时间：{extracted_at}",
        "",
        f"共 {len(rows)} 条。链接来自本机微信内置浏览器缓存。",
        "",
    ]
    for row in sorted_by_publication_desc(rows):
        date_label = row.get("发布日期") or "发布日期待确认"
        lines.append(
            f"- **{date_label} · {row['公众号']}** "
            f"[{row['文章标题']}]({row['文章链接']}) `#{row['类型']}`"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "records": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "records": {}}
    if not isinstance(data.get("records"), dict):
        data["records"] = {}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def merge_daily_csv(path: Path, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                url = str(row.get("文章链接", "")).strip()
                if url:
                    merged[url] = dict(row)
    for row in rows:
        merged[row["文章链接"]] = row
    return sorted_by_publication_desc(list(merged.values()))


def write_daily_outputs(output_dir: Path, date_text: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    daily_dir = output_dir / DAILY_DIR_NAME
    daily_dir.mkdir(parents=True, exist_ok=True)
    csv_path = daily_dir / f"{date_text}.csv"
    merged = merge_daily_csv(csv_path, rows)
    write_csv(csv_path, merged)
    write_daily_markdown(daily_dir / f"{date_text}.md", date_text, merged)


def write_daily_markdown(path: Path, date_text: str, rows: list[dict[str, str]]) -> None:
    lines = [
        f"# {date_text} 微信公众号招聘链接",
        "",
        f"共 {len(rows)} 条；同一链接跨次运行自动去重。",
        "",
    ]
    for row in sorted_by_publication_desc(rows):
        date_label = row.get("发布日期") or "发布日期待确认"
        lines.append(
            f"- **{date_label} · {row['公众号']}** "
            f"[{row['文章标题']}]({row['文章链接']}) `#{row['类型']}`"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def refresh_daily_outputs(
    output_dir: Path,
    records: dict[str, dict[str, str]],
    ledger_dates: dict[tuple[str, str], str],
) -> None:
    """Backfill prior daily CSVs too, then apply the current Markdown format."""
    daily_dir = output_dir / DAILY_DIR_NAME
    if not daily_dir.exists():
        return
    for csv_path in daily_dir.glob("*.csv"):
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        enrich_publication_dates(rows, records, ledger_dates)
        write_csv(csv_path, sorted_by_publication_desc(rows))
        write_daily_markdown(daily_dir / f"{csv_path.stem}.md", csv_path.stem, rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    accounts_file = Path(__file__).resolve().parent / ACCOUNTS_FILE_NAME
    configured_accounts = DEFAULT_ACCOUNTS
    if accounts_file.exists():
        configured_accounts = [
            line.strip()
            for line in accounts_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ] or DEFAULT_ACCOUNTS
    parser.add_argument(
        "--accounts",
        nargs="+",
        default=configured_accounts,
        help=f"需要收录的公众号名称；默认读取 {ACCOUNTS_FILE_NAME}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "output",
        help="输出目录",
    )
    parser.add_argument(
        "--database",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="Share Data SQLite 文件；可重复指定。Linux/Windows 必须显式提供。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    databases = args.database
    if not databases and sys.platform == "darwin":
        databases = find_macos_share_data_files()
    if not databases:
        raise SystemExit(
            "未找到 Share Data SQLite 缓存。macOS 请先登录并打开至少一篇公众号文章；"
            "Linux/Windows 请用 --database /path/to/'Share Data' 指定缓存副本。"
        )
    missing = [str(path) for path in databases if not path.is_file()]
    if missing:
        raise SystemExit(f"以下 --database 文件不存在或不是普通文件：{', '.join(missing)}")

    all_rows: list[dict[str, str]] = []
    for database in databases:
        all_rows.extend(read_rows(database))
    supplements_path = Path(__file__).resolve().parent / SUPPLEMENTS_FILE_NAME
    all_rows.extend(read_supplemental_rows(supplements_path))

    accounts = set(args.accounts)
    selected = deduplicate([row for row in all_rows if row["公众号"] in accounts])
    filtered = [
        {**row, "类型": classify(row["文章标题"])}
        for row in selected
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    extracted_at = now.strftime("%Y-%m-%d %H:%M:%S %z")
    today = now.strftime("%Y-%m-%d")

    state_path = args.output_dir / STATE_FILE_NAME
    state_existed = state_path.exists()
    state = load_state(state_path)
    records: dict[str, dict[str, str]] = state["records"]
    ledger_dates = read_ledger_publication_dates(args.output_dir / LEDGER_FILE_NAME)
    enrich_publication_dates(filtered, records, ledger_dates)
    new_rows: list[dict[str, str]] = []
    for row in filtered:
        url = row["文章链接"]
        if url not in records:
            row["首次发现日期"] = today
            records[url] = {**row, "首次发现时间": extracted_at}
            if state_existed:
                new_rows.append(dict(row))
        else:
            row["首次发现日期"] = records[url].get("首次发现日期", today)
            records[url].update(row)
            records[url]["最后发现时间"] = extracted_at

    state["updated_at"] = extracted_at
    save_state(state_path, state)
    write_csv(args.output_dir / "微信公众号招聘链接.csv", filtered)
    write_markdown(args.output_dir / "微信公众号招聘链接.md", filtered, extracted_at)
    new_rows_by_date: dict[str, list[dict[str, str]]] = {}
    for row in new_rows:
        date_text = row.get("发布日期") or today
        new_rows_by_date.setdefault(date_text, []).append(row)
    for date_text, rows in new_rows_by_date.items():
        write_daily_outputs(args.output_dir, date_text, rows)
    refresh_daily_outputs(args.output_dir, records, ledger_dates)

    print(f"扫描缓存数据库：{len(databases)} 个")
    print(f"目标公众号文章：{len(selected)} 条")
    print(f"目标公众号消息收录：{len(filtered)} 条（不做关键词筛选）")
    if state_existed:
        print(f"本次新增链接：{len(new_rows)} 条")
    else:
        print("首次运行：已建立历史基线；从下次运行开始记录每日新增")
    print(f"输出目录：{args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
