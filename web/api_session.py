"""HTTP 基础 mixin：路由分发、JSON/静态文件响应、会话与登录登出。

各业务 API（文章看板 / 收藏 / 投递管理）以 mixin 形式挂在 CoreHandler 之上，
最终在 server.py 组装成完整的 RequestHandler。
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import time
from datetime import timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import unquote, urlparse

from database import open_db
from server_common import (
    COOKIE_NAME,
    SESSION_DAYS,
    USERNAME_RE,
    hash_access_code,
    hash_token,
    iso_now,
    now,
)


class CoreHandler:
    """请求处理公共基座（不含业务端点；do_GET/do_POST 分发到各 mixin 的 handle_* 方法）。"""

    server: Any  # AppServer，在 server.py 里组装时由真正基类提供

    protocol_version = "HTTP/1.1"

    # ---- 通用响应与请求解析 ----

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, payload: Any, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def read_json(self, max_bytes: int = 16_384) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("无效的请求长度") from error
        if length <= 0 or length > max_bytes:
            raise ValueError("请求内容为空或过大")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("仅接受 JSON 请求")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON 顶层必须是对象")
        return payload

    # ---- 会话与登录 ----

    def origin_is_valid(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host", "")

    def session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def current_user(self) -> Any:
        token = self.session_token()
        if not token:
            return None
        with open_db(self.server.config.database) as database:
            user = database.execute(
                """
                SELECT users.id, users.username, users.display_name
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1
                """,
                (hash_token(token), iso_now()),
            ).fetchone()
        return user

    def require_user(self) -> Any:
        user = self.current_user()
        if user is None:
            self.send_json({"error": "请先登录"}, HTTPStatus.UNAUTHORIZED)
        return user

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            self.handle_bootstrap()
        elif path == "/api/clicks":
            self.handle_get_clicks()
        elif path == "/api/favorites":
            self.handle_favorites()
        elif path == "/api/applications":
            self.handle_applications()
        elif path == "/api/screen-status":
            self.handle_screen_status()
        elif path == "/healthz":
            self.handle_health()
        elif path == "/favicon.ico":
            # 部分浏览器/书签工具不看 <link>，直接请求根路径的 favicon.ico
            self.handle_static("/static/favicon.ico")
        elif path == "/" or path.startswith("/static/"):
            self.handle_static(path)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not self.origin_is_valid():
            # 不再复用包含未消费请求体的 HTTP/1.1 连接。
            self.close_connection = True
            self.send_json({"error": "请求来源校验失败"}, HTTPStatus.FORBIDDEN)
            return
        path = urlparse(self.path).path
        if path == "/api/login":
            self.handle_login()
        elif path == "/api/logout":
            self.handle_logout()
        elif path == "/api/clicks":
            self.handle_set_click()
        elif path == "/api/favorites":
            self.handle_set_favorite()
        elif path == "/api/collections":
            self.handle_collections()
        elif path == "/api/applications":
            self.handle_application_add()
        elif path == "/api/applications/update":
            self.handle_application_update()
        elif path == "/api/applications/delete":
            self.handle_application_delete()
        elif path == "/api/preferences":
            self.handle_preferences()
        elif path == "/api/screen":
            self.handle_screen_start()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    # ---- 静态文件与健康检查 ----

    def handle_health(self) -> None:
        try:
            payload, _ = self.server.articles.load()
            with open_db(self.server.config.database) as database:
                database.execute("SELECT 1").fetchone()
            self.send_json({"ok": True, "groups": len(payload["groups"])})
        except Exception as error:
            self.send_json({"ok": False, "error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)

    def handle_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path.removeprefix("/static/"))
        target = (self.server.config.static_dir / relative).resolve()
        static_root = self.server.config.static_dir.resolve()
        if static_root not in target.parents and target != static_root:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8" if media_type.startswith("text/") else media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if target.name == "index.html" else "public, max-age=300")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---- 登录 / 登出 ----

    def handle_login(self) -> None:
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        username = str(body.get("username", "")).strip()
        access_code = str(body.get("access_code", ""))
        if not USERNAME_RE.fullmatch(username) or not (8 <= len(access_code) <= 200):
            self.send_json({"error": "用户名或访问码错误"}, HTTPStatus.UNAUTHORIZED)
            return
        client = self.client_address[0]
        timestamp = time.monotonic()
        with self.server.login_lock:
            recent = [item for item in self.server.login_attempts.get(client, []) if timestamp - item < 300]
            if len(recent) >= 10:
                self.send_json({"error": "尝试次数过多，请 5 分钟后再试"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            recent.append(timestamp)
            self.server.login_attempts[client] = recent
        with open_db(self.server.config.database) as database:
            user = database.execute(
                "SELECT id, username, display_name, access_salt, access_hash FROM users WHERE username = ? AND active = 1",
                (username,),
            ).fetchone()
            valid = False
            if user:
                calculated = hash_access_code(access_code, bytes.fromhex(user["access_salt"]))
                valid = hmac.compare_digest(calculated, user["access_hash"])
            if not valid:
                self.send_json({"error": "用户名或访问码错误"}, HTTPStatus.UNAUTHORIZED)
                return
            token = secrets.token_urlsafe(32)
            expires = now() + timedelta(days=SESSION_DAYS)
            database.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso_now(),))
            database.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (hash_token(token), user["id"], iso_now(), expires.isoformat(timespec="seconds")),
            )
        cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"
        if self.server.config.secure_cookie:
            cookie += "; Secure"
        self.send_json(
            {"ok": True, "user": {"username": user["username"], "display_name": user["display_name"]}},
            headers={"Set-Cookie": cookie},
        )

    def handle_logout(self) -> None:
        token = self.session_token()
        if token:
            with open_db(self.server.config.database) as database:
                database.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
        self.send_json(
            {"ok": True},
            headers={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
        )
