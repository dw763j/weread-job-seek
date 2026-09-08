#!/usr/bin/env python3
"""原子记录已打开的公众号文章，供可恢复的历史扫描使用。"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_LEDGER = Path(__file__).resolve().parent / "output" / "扫描运行台账.json"


def normalized_title(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).lower()


def load_ledger(path: Path, boundary_date: str | None) -> dict[str, Any]:
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("accounts", {})
                return value
        except (OSError, json.JSONDecodeError):
            pass
        raise SystemExit(f"无法读取现有台账：{path}")
    if not boundary_date:
        raise SystemExit("新建台账时必须提供 --boundary-date")
    return {
        "run_id": f"{datetime.now().astimezone():%Y-%m-%d}-history-through-{boundary_date}",
        "boundary_date": boundary_date,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "accounts": {},
    }


def atomic_save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="公众号精确名称")
    parser.add_argument("--publication-date", required=True, help="文章显示日期，YYYY-MM-DD")
    parser.add_argument("--title", action="append", required=True, help="已打开文章的精确标题；可重复传入")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--boundary-date", help="仅在新建台账时使用")
    parser.add_argument("--visible-newest-date", help="该公众号历史中看到的最新日期")
    args = parser.parse_args()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.publication_date):
        raise SystemExit("--publication-date 必须是 YYYY-MM-DD")
    ledger = load_ledger(args.ledger, args.boundary_date)
    accounts = ledger["accounts"]
    account = accounts.setdefault(args.account, {"status": "scanning", "articles": []})
    account["status"] = "scanning"
    if args.visible_newest_date:
        account["visible_newest_date"] = args.visible_newest_date
    articles = account.setdefault("articles", [])
    existing = {
        (str(item.get("publication_date", "")), normalized_title(str(item.get("title", "")))): item
        for item in articles
        if isinstance(item, dict)
    }
    for title in args.title:
        title = title.strip()
        if not title:
            continue
        key = (args.publication_date, normalized_title(title))
        article = existing.get(key)
        if article is None:
            article = {}
            articles.append(article)
            existing[key] = article
        article.update(
            {
                "publication_date": args.publication_date,
                "title": title,
                "normalized_title": key[1],
                "source": "history",
                "state": "opened",
                "date_status": "confirmed",
                "provenance": "article",
            }
        )
    account["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    ledger["updated_at"] = account["updated_at"]
    atomic_save(args.ledger, ledger)
    print(f"已写入 {args.account}：{len(args.title)} 条，台账：{args.ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
