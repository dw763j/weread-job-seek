from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import AppServer, Config, add_user, import_clicks, open_db  # noqa: E402


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "clicks.sqlite3"
        self.source = root / "groups.json"
        self.static = root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("ok", encoding="utf-8")
        self.screen_results = root / "screen_results.json"
        self.screen_script = root / "screen_update.py"
        self.urls = ["https://mp.weixin.qq.com/s/one", "https://mp.weixin.qq.com/s/two"]
        self.source.write_text(
            json.dumps(
                {
                    "generated_at": "2026-08-07T10:00:00+08:00",
                    "range": {"start": "20260801", "end": "20260807"},
                    "source_rows": 2,
                    "groups": [
                        {
                            "canonical": {"公众号": "测试就业", "发布日期": "2026-08-07", "文章标题": "测试招聘", "文章链接": self.urls[0]},
                            "members": [
                                {"公众号": "测试就业", "发布日期": "2026-08-07", "文章标题": "测试招聘", "文章链接": self.urls[0]},
                                {"公众号": "另一就业", "发布日期": "2026-08-06", "文章标题": "测试招聘", "文章链接": self.urls[1]},
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        add_user(self.database, "alice", "Alice", "alice-code")
        add_user(self.database, "bob", "Bob", "bob-code-1")
        self.server = AppServer(
            ("127.0.0.1", 0),
            Config(self.database, self.source, self.static,
                   screen_results=self.screen_results, screen_script=self.screen_script),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def call(self, method: str, path: str, body=None, cookie: str | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        set_cookie = response.getheader("Set-Cookie")
        connection.close()
        return response.status, data, set_cookie

    def login(self, username: str, code: str) -> str:
        status, _, header = self.call("POST", "/api/login", {"username": username, "access_code": code})
        self.assertEqual(status, 200)
        return header.split(";", 1)[0]

    def test_authentication_and_user_isolation(self) -> None:
        self.assertEqual(self.call("GET", "/api/bootstrap")[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": "alice", "access_code": "wrong-code"})[0], 401)
        alice = self.login("alice", "alice-code")
        bob = self.login("bob", "bob-code-1")
        status, payload, _ = self.call("GET", "/api/bootstrap", cookie=alice)
        self.assertEqual(status, 200)
        group_id = payload["groups"][0]["id"]
        self.assertIsNone(payload["groups"][0]["clicked_at"])
        status, _, _ = self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": True}, alice)
        self.assertEqual(status, 200)
        self.assertIsNotNone(self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]["clicked_at"])
        self.assertIsNone(self.call("GET", "/api/bootstrap", cookie=bob)[1]["groups"][0]["clicked_at"])

    def test_group_click_persists_all_member_urls_and_can_clear(self) -> None:
        alice = self.login("alice", "alice-code")
        group_id = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]["id"]
        self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": True}, alice)
        with open_db(self.database) as database:
            rows = database.execute("SELECT url FROM clicks ORDER BY url").fetchall()
        self.assertEqual([row["url"] for row in rows], self.urls)
        self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": False}, alice)
        with open_db(self.database) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM clicks").fetchone()[0], 0)

    def test_import_legacy_clicks(self) -> None:
        legacy = Path(self.temp.name) / "legacy.json"
        legacy.write_text(json.dumps({"urls": {self.urls[0]: "2026-08-01T00:00:00+08:00"}}), encoding="utf-8")
        self.assertEqual(import_clicks(self.database, "alice", legacy), 1)
        alice = self.login("alice", "alice-code")
        group = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]
        self.assertEqual(group["clicked_at"], "2026-08-01T00:00:00+08:00")

    def test_health_and_origin_check(self) -> None:
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "POST", "/api/login", body=json.dumps({"username": "alice", "access_code": "alice-code"}),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        connection.close()

    def write_screen_results(self, kind: str, mtime_offset: float) -> None:
        entry = {
            "kept": {"unit": "测试公司", "intro": "一句话简介", "title": "测试招聘",
                     "accounts": ["测试就业"], "recruit_target": "2027届",
                     "positions": [{"name": "软件工程师", "category": "计算机类", "location": "成都"}],
                     "apply_url": "https://example.com/apply", "article_url": self.urls[0], "urls": self.urls},
            "skipped": {"account": "测试就业", "title": "测试招聘",
                        "article_url": self.urls[0], "urls": self.urls, "reason": "非计算机类岗位"},
        }[kind]
        days = ({} if kind == "empty"
                else {"kept": [entry] if kind == "kept" else [], "skipped": [entry] if kind == "skipped" else []})
        self.screen_results.write_text(
            json.dumps({"days": {"2026-08-07": days}}, ensure_ascii=False), encoding="utf-8")
        stamp = time.time() + mtime_offset
        os.utime(self.screen_results, (stamp, stamp))

    def test_screen_results_merged_into_bootstrap(self) -> None:
        alice = self.login("alice", "alice-code")
        group = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]
        self.assertIsNone(group["screen"])
        self.write_screen_results("kept", 100)
        screen = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]["screen"]
        self.assertEqual(screen["kind"], "kept")
        self.assertEqual(screen["unit"], "测试公司")
        self.assertEqual(screen["positions"][0]["name"], "软件工程师")
        self.assertEqual(screen["screened_date"], "2026-08-07")
        self.write_screen_results("skipped", 200)
        screen = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]["screen"]
        self.assertEqual(screen["kind"], "skipped")
        self.assertEqual(screen["reason"], "非计算机类岗位")

    def test_screen_trigger_validation_and_lifecycle(self) -> None:
        self.assertEqual(self.call("POST", "/api/screen", {"date": "2026-08-07"})[0], 401)
        alice = self.login("alice", "alice-code")
        self.assertEqual(self.call("POST", "/api/screen", {"date": "2026/08/07"}, alice)[0], 400)
        self.assertEqual(self.call("POST", "/api/screen", {"date": "2026-13-40"}, alice)[0], 400)
        # 筛选脚本不存在时应报 500，而不是启动任务
        status, payload, _ = self.call("POST", "/api/screen", {"date": "2026-08-07"}, alice)
        self.assertEqual(status, 500)
        self.assertIn("screen_update.py", payload["error"])
        # 提供一个会短暂休眠的假脚本，验证任务状态流转与并发拒绝
        self.screen_script.write_text("import time\ntime.sleep(0.8)\nprint('DONE')\n", encoding="utf-8")
        status, payload, _ = self.call("POST", "/api/screen", {"date": "2026-08-07"}, alice)
        self.assertEqual(status, 200)
        self.assertTrue(payload["started"])
        status, _, _ = self.call("POST", "/api/screen", {"date": "2026-08-06"}, alice)
        self.assertEqual(status, 409)
        status, payload, _ = self.call("GET", "/api/screen-status", cookie=alice)
        self.assertTrue(payload["running"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, payload, _ = self.call("GET", "/api/screen-status", cookie=alice)
            task = payload["tasks"]["2026-08-07"]
            if task["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["returncode"], 0)
        self.assertIn("DONE", task["log"])
        log_file = self.screen_results.parent / "screen-logs" / "screen-2026-08-07.log"
        self.assertTrue(log_file.is_file())

    def test_screen_all_trigger(self) -> None:
        alice = self.login("alice", "alice-code")
        self.assertEqual(self.call("POST", "/api/screen", {"all": True})[0], 401)
        self.screen_script.write_text("print('ALL DONE')\n", encoding="utf-8")
        status, payload, _ = self.call("POST", "/api/screen", {"all": True}, alice)
        self.assertEqual(status, 200)
        self.assertEqual(payload["date"], "all")
        deadline = time.monotonic() + 10
        task = None
        while time.monotonic() < deadline:
            _, payload, _ = self.call("GET", "/api/screen-status", cookie=alice)
            task = payload["tasks"]["all"]
            if task["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(task["status"], "done")
        self.assertIn("ALL DONE", task["log"])

    def test_preferences_per_user(self) -> None:
        self.assertEqual(self.call("POST", "/api/preferences", {"cities": ["成都"]})[0], 401)
        alice = self.login("alice", "alice-code")
        default = self.call("GET", "/api/bootstrap", cookie=alice)[1]["preferences"]
        self.assertIn("杭州", default["cities"])  # 默认意向城市包含杭州
        self.assertEqual(default["keywords"], [])
        # 非数组输入应被拒绝
        self.assertEqual(self.call("POST", "/api/preferences", {"cities": "成都"}, alice)[0], 400)
        status, payload, _ = self.call(
            "POST", "/api/preferences", {"cities": ["杭州", "成都", "成都", " "], "keywords": ["人工智能", "软件"]}, alice)
        self.assertEqual(status, 200)
        self.assertEqual(payload["preferences"]["cities"], ["杭州", "成都"])
        self.assertEqual(payload["preferences"]["keywords"], ["人工智能", "软件"])
        prefs = self.call("GET", "/api/bootstrap", cookie=alice)[1]["preferences"]
        self.assertEqual(prefs["cities"], ["杭州", "成都"])
        # 偏好按用户隔离：bob 仍是默认值
        bob = self.login("bob", "bob-code-1")
        bob_prefs = self.call("GET", "/api/bootstrap", cookie=bob)[1]["preferences"]
        self.assertIn("杭州", bob_prefs["cities"])
        # 显式清空后不再回落到默认
        status, payload, _ = self.call("POST", "/api/preferences", {"cities": [], "keywords": []}, alice)
        self.assertEqual(payload["preferences"], {"cities": [], "keywords": []})

    def test_favorites_and_collections(self) -> None:
        self.assertEqual(self.call("GET", "/api/favorites")[0], 401)
        self.assertEqual(self.call("POST", "/api/favorites", {"group_id": "0" * 24, "favorited": True})[0], 401)
        self.assertEqual(self.call("POST", "/api/collections", {"action": "create", "name": "成都"})[0], 401)
        alice = self.login("alice", "alice-code")
        group_id = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]["id"]
        # 收藏到未分组，bootstrap 携带收藏索引与分组
        status, payload, _ = self.call("POST", "/api/favorites", {"group_id": group_id, "favorited": True}, alice)
        self.assertEqual(status, 200)
        self.assertEqual(payload["favorites"], {group_id: None})
        boot = self.call("GET", "/api/bootstrap", cookie=alice)[1]
        self.assertEqual(boot["favorites"], {group_id: None})
        self.assertEqual(boot["collections"], [])
        # 建分组并收藏到分组（再收藏即移动分组）
        status, payload, _ = self.call("POST", "/api/collections", {"action": "create", "name": "成都"}, alice)
        self.assertEqual(status, 200)
        collection_id = payload["collections"][0]["id"]
        status, payload, _ = self.call(
            "POST", "/api/favorites", {"group_id": group_id, "favorited": True, "collection_id": collection_id}, alice)
        self.assertEqual(payload["favorites"], {group_id: collection_id})
        # 同名分组 409；收藏行带标题/链接快照
        self.assertEqual(self.call("POST", "/api/collections", {"action": "create", "name": "成都"}, alice)[0], 409)
        listing = self.call("GET", "/api/favorites", cookie=alice)[1]
        self.assertEqual(listing["items"][0]["title"], "测试招聘")
        self.assertEqual(listing["items"][0]["url"], self.urls[0])
        self.assertEqual(listing["favorites"], {group_id: collection_id})
        # 重命名分组
        status, payload, _ = self.call(
            "POST", "/api/collections", {"action": "rename", "id": collection_id, "name": "西安"}, alice)
        self.assertEqual(payload["collections"][0]["name"], "西安")
        self.assertEqual(self.call(
            "POST", "/api/collections", {"action": "rename", "id": 999, "name": "x"}, alice)[0], 404)
        # 删除分组后收藏退回未分组，收藏本身保留
        status, payload, _ = self.call("POST", "/api/collections", {"action": "delete", "id": collection_id}, alice)
        self.assertEqual(payload["favorites"], {group_id: None})
        self.assertEqual(len(payload["items"]), 1)
        # 非法请求：不存在的文章组 409，不存在的分组 404，坏的分组 id 400
        self.assertEqual(self.call("POST", "/api/favorites", {"group_id": "f" * 24, "favorited": True}, alice)[0], 409)
        self.assertEqual(self.call(
            "POST", "/api/favorites", {"group_id": group_id, "favorited": True, "collection_id": 999}, alice)[0], 404)
        self.assertEqual(self.call(
            "POST", "/api/favorites", {"group_id": group_id, "favorited": True, "collection_id": "x"}, alice)[0], 400)
        # 取消收藏
        status, payload, _ = self.call("POST", "/api/favorites", {"group_id": group_id, "favorited": False}, alice)
        self.assertEqual(payload["favorites"], {})
        self.assertEqual(payload["items"], [])
        # 用户隔离：bob 看不到 alice 的收藏
        bob = self.login("bob", "bob-code-1")
        self.assertEqual(self.call("GET", "/api/favorites", cookie=bob)[1]["items"], [])

    def test_applications_lifecycle(self) -> None:
        self.assertEqual(self.call("GET", "/api/applications")[0], 401)
        self.assertEqual(self.call("POST", "/api/applications", {"company": "腾讯"})[0], 401)
        alice = self.login("alice", "alice-code")
        # 手动添加公司（带招聘网站链接，一键跳转）
        status, payload, _ = self.call(
            "POST", "/api/applications", {"company": "腾讯", "job_url": "https://join.qq.com/x?y=1"}, alice)
        self.assertEqual(status, 200)
        first_id = payload["application"]["id"]
        self.assertEqual(payload["application"]["status"], "applied")
        self.assertEqual(len(payload["application"]["history"]), 1)
        # 校验：缺公司、坏链接、坏状态、坏文章组标识
        self.assertEqual(self.call("POST", "/api/applications", {"company": ""}, alice)[0], 400)
        self.assertEqual(self.call(
            "POST", "/api/applications", {"company": "x", "job_url": "javascript:alert(1)"}, alice)[0], 400)
        self.assertEqual(self.call(
            "POST", "/api/applications", {"company": "x", "status": "nope"}, alice)[0], 400)
        self.assertEqual(self.call(
            "POST", "/api/applications", {"company": "x", "article_group_id": "zzz"}, alice)[0], 400)
        # 从公众号文章添加：服务端写入文章标题/链接快照
        group_id = self.call("GET", "/api/bootstrap", cookie=alice)[1]["groups"][0]["id"]
        status, payload, _ = self.call(
            "POST", "/api/applications", {"company": "测试公司", "article_group_id": group_id}, alice)
        second_id = payload["application"]["id"]
        self.assertEqual(payload["application"]["article_title"], "测试招聘")
        self.assertEqual(payload["application"]["article_url"], self.urls[0])

        def update(body):
            return self.call("POST", "/api/applications/update", body, alice)

        # 状态推进：笔试两轮（同状态再点 test 追加一轮）、一面带备注
        status, payload, _ = update({"id": second_id, "status": "test"})
        status, payload, _ = update({"id": second_id, "status": "test"})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["application"]["history"]), 3)
        status, payload, _ = update({"id": second_id, "status": "interview1", "status_note": "技术面"})
        self.assertEqual(payload["application"]["status"], "interview1")
        self.assertEqual(payload["application"]["history"][-1]["note"], "技术面")
        # 撤销最后一条进度，状态回退
        status, payload, _ = update({"id": second_id, "undo": True})
        self.assertEqual(payload["application"]["status"], "test")
        self.assertEqual(len(payload["application"]["history"]), 3)
        # 编辑基本信息
        status, payload, _ = update({"id": first_id, "company": "字节跳动", "note": "内推", "job_url": ""})
        self.assertEqual(payload["application"]["company"], "字节跳动")
        self.assertEqual(payload["application"]["note"], "内推")
        self.assertEqual(payload["application"]["job_url"], "")
        # 终止状态
        status, payload, _ = update({"id": first_id, "status": "rejected"})
        self.assertEqual(payload["application"]["status"], "rejected")
        self.assertEqual(self.call("POST", "/api/applications/update", {"id": first_id, "status": "x"}, alice)[0], 400)
        self.assertEqual(self.call("POST", "/api/applications/update", {"id": 999, "status": "offer"}, alice)[0], 404)
        # 列表 + 用户隔离
        self.assertEqual(len(self.call("GET", "/api/applications", cookie=alice)[1]["applications"]), 2)
        bob = self.login("bob", "bob-code-1")
        self.assertEqual(self.call("GET", "/api/applications", cookie=bob)[1]["applications"], [])
        status, _, _ = self.call("POST", "/api/applications/update", {"id": first_id, "status": "offer"}, bob)
        self.assertEqual(status, 404)  # bob 无权改 alice 的记录
        # 删除
        self.assertEqual(self.call("POST", "/api/applications/delete", {"id": first_id}, alice)[0], 200)
        self.assertEqual(self.call("POST", "/api/applications/delete", {"id": first_id}, alice)[0], 404)
        self.assertEqual(len(self.call("GET", "/api/applications", cookie=alice)[1]["applications"]), 1)


if __name__ == "__main__":
    unittest.main()
