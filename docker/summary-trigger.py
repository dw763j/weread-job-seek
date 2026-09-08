#!/usr/bin/env python3
"""汇总服务触发器：常驻的轻量 HTTP 服务，手动触发 generate_summary.py。

- GET  /healthz         健康检查，返回汇总脚本是否存在。
- POST /run[?range=...] 触发一次汇总生成；range 可选（YYYYMMDD-YYYYMMDD，
                         缺省等价于 generate_summary.py 的默认「自 2026-07-10 起全部」）。
  请求体可传 JSON：{"range": "...", "args": ["--no-reuse-groups"]}，
  args 会原样追加到命令行。

仅使用 Python 标准库，无第三方依赖。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parent.parent  # /app
SUMMARY_PY = APP / "generate_summary.py"
HOST = os.environ.get("SUMMARY_TRIGGER_HOST", "0.0.0.0")
PORT = int(os.environ.get("SUMMARY_TRIGGER_PORT", "8090"))

MAX_CAPTURE = 16_000  # 单次返回的 stdout/stderr 截断长度


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[trigger] {self.address_string()} {fmt % args}", flush=True)

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/healthz":
            self._reply(200, {"ok": True, "summary_py": str(SUMMARY_PY), "exists": SUMMARY_PY.exists()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/run":
            self._reply(404, {"error": "not found"})
            return
        query = parse_qs(urlparse(self.path).query)
        range_arg = (query.get("range") or [""])[0] or None
        extra_args: list[str] = []
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length:
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else {}
                if isinstance(body, dict):
                    if not range_arg and body.get("range"):
                        range_arg = str(body["range"])
                    for item in body.get("args") or []:
                        extra_args.append(str(item))
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "请求体不是合法 JSON"})
            return

        cmd = [sys.executable, str(SUMMARY_PY)]
        if range_arg:
            cmd += ["--range", range_arg]
        cmd += extra_args

        print(f"[trigger] 开始汇总：{cmd}", flush=True)
        proc = subprocess.run(cmd, cwd=str(APP), capture_output=True, text=True)
        self._reply(
            200,
            {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "command": cmd,
                "stdout": proc.stdout[-MAX_CAPTURE:],
                "stderr": proc.stderr[-MAX_CAPTURE:],
            },
        )


if __name__ == "__main__":
    print(f"汇总触发器已监听 http://{HOST}:{PORT}（POST /run 触发汇总）", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
