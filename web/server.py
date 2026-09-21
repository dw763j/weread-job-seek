#!/usr/bin/env python3
"""微信公众号招聘链接多人点击状态服务（仅使用 Python 标准库）。

模块拆分（都在本目录，扁平导入）：
  server_common   常量与纯工具函数（时间/哈希/校验）
  database        SQLite 连接、建表 schema、行读取与序列化
  stores          文章分组与 AI 筛选结果的 mtime 热重载
  api_session     HTTP 基座：路由分发、JSON/静态响应、会话与登录
  api_articles    文章看板端点（bootstrap/已读/偏好/AI 筛选任务）
  api_favorites   收藏与收藏分组端点
  server.py       本文件：组装 RequestHandler 与 AppServer、命令行入口
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from api_articles import ApiArticlesMixin
from api_favorites import ApiFavoritesMixin
from api_session import CoreHandler
from database import initialize_database, open_db  # noqa: F401  （open_db 供测试/运维导入）
from server_common import (
    DEFAULT_DATABASE,
    DEFAULT_SCREEN_RESULTS,
    DEFAULT_SCREEN_SCRIPT,
    DEFAULT_SOURCE,
    STATIC_DIR,
    USERNAME_RE,
    WECHAT_PREFIX,
    hash_access_code,
    iso_now,
    new_access_code,
)
from stores import ArticleStore, ScreenStore


class Config:
    def __init__(self, database: Path, source: Path, static_dir: Path = STATIC_DIR,
                 secure_cookie: bool = False,
                 screen_results: Path = DEFAULT_SCREEN_RESULTS,
                 screen_script: Path = DEFAULT_SCREEN_SCRIPT):
        self.database = database
        self.source = source
        self.static_dir = static_dir
        self.secure_cookie = secure_cookie
        self.screen_results = screen_results
        self.screen_script = screen_script


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: Config):
        initialize_database(config.database)
        self.config = config
        self.articles = ArticleStore(config.source)
        self.screens = ScreenStore(config.screen_results)
        self.login_attempts: dict[str, list[float]] = {}
        self.login_lock = threading.Lock()
        self.screen_tasks: dict[str, dict] = {}
        self.screen_lock = threading.Lock()
        super().__init__(address, RequestHandler)


class RequestHandler(ApiFavoritesMixin, ApiArticlesMixin, CoreHandler, BaseHTTPRequestHandler):
    server: AppServer


def add_user(database_path: Path, username: str, display_name: str, access_code: str | None) -> str:
    if not USERNAME_RE.fullmatch(username):
        raise SystemExit("用户名仅可包含字母、数字、点、下划线和短横线，最长 40 字符。")
    initialize_database(database_path)
    code = access_code or new_access_code()
    if len(code) < 8:
        raise SystemExit("访问码至少需要 8 个字符。")
    salt = secrets.token_bytes(16)
    with open_db(database_path) as database:
        database.execute(
            """
            INSERT INTO users(username, display_name, access_salt, access_hash, active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(username) DO UPDATE SET
              display_name = excluded.display_name,
              access_salt = excluded.access_salt,
              access_hash = excluded.access_hash,
              active = 1
            """,
            (username, display_name, salt.hex(), hash_access_code(code, salt), iso_now()),
        )
    return code


def import_clicks(database_path: Path, username: str, state_path: Path) -> int:
    initialize_database(database_path)
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    urls = raw.get("urls", raw) if isinstance(raw, dict) else {}
    rows = [
        (url, stamp if isinstance(stamp, str) and stamp else iso_now())
        for url, stamp in urls.items()
        if isinstance(url, str) and url.startswith(WECHAT_PREFIX)
    ]
    with open_db(database_path) as database:
        user = database.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            raise SystemExit(f"用户不存在：{username}")
        database.executemany(
            """
            INSERT INTO clicks(user_id, url, clicked_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id, url) DO UPDATE SET clicked_at = excluded.clicked_at
            """,
            [(user["id"], url, stamp) for url, stamp in rows],
        )
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化数据库")
    add = commands.add_parser("add-user", help="新增用户或重置访问码")
    add.add_argument("username")
    add.add_argument("--name", required=True, help="显示名称")
    add.add_argument("--access-code", help="指定访问码；留空则安全生成")
    commands.add_parser("list-users", help="列出用户（不显示访问码）")
    disable = commands.add_parser("disable-user", help="禁用用户并清除会话")
    disable.add_argument("username")
    importer = commands.add_parser("import-clicks", help="把旧版已点击 JSON 导入指定用户")
    importer.add_argument("username")
    importer.add_argument("state", type=Path)
    serve = commands.add_parser("serve", help="启动网页服务")
    serve.add_argument("--host", default="127.0.0.1", help="监听 IP；局域网可用 0.0.0.0 或指定网卡 IP")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--secure-cookie", action="store_true", help="通过 HTTPS 反向代理时启用")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "init":
        initialize_database(args.database)
        print(f"数据库已初始化：{args.database}")
    elif args.command == "add-user":
        code = add_user(args.database, args.username, args.name, args.access_code)
        print(f"用户已就绪：{args.username}（{args.name}）")
        print(f"访问码（仅此次显示）：{code}")
    elif args.command == "list-users":
        initialize_database(args.database)
        with open_db(args.database) as database:
            for row in database.execute("SELECT username, display_name, active, created_at FROM users ORDER BY username"):
                print(f"{row['username']}\t{row['display_name']}\t{'启用' if row['active'] else '禁用'}\t{row['created_at']}")
    elif args.command == "disable-user":
        initialize_database(args.database)
        with open_db(args.database) as database:
            user = database.execute("SELECT id FROM users WHERE username = ?", (args.username,)).fetchone()
            if not user:
                raise SystemExit(f"用户不存在：{args.username}")
            database.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
            database.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        print(f"用户已禁用：{args.username}")
    elif args.command == "import-clicks":
        count = import_clicks(args.database, args.username, args.state)
        print(f"已为 {args.username} 导入 {count} 条点击记录。")
    elif args.command == "serve":
        if not args.source.is_file():
            raise SystemExit(f"找不到文章分组文件：{args.source}")
        config = Config(args.database.resolve(), args.source.resolve(), secure_cookie=args.secure_cookie)
        server = AppServer((args.host, args.port), config)
        print(f"招聘链接服务已启动：http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。")
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
