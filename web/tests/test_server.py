from __future__ import annotations

import gzip
import http.client
from urllib.parse import quote
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

    def board(self, cookie, query="status=all"):
        """看板分页查询（筛选与分页在后端完成）。"""
        status, payload, _ = self.call("GET", f"/api/articles?{query}", cookie=cookie)
        self.assertEqual(status, 200)
        return payload

    def test_authentication_and_user_isolation(self) -> None:
        self.assertEqual(self.call("GET", "/api/bootstrap")[0], 401)
        self.assertEqual(self.call("GET", "/api/articles?status=all")[0], 401)
        self.assertEqual(self.call("GET", "/api/fairs")[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": "alice", "access_code": "wrong-code"})[0], 401)
        alice = self.login("alice", "alice-code")
        bob = self.login("bob", "bob-code-1")
        status, payload, _ = self.call("GET", "/api/bootstrap", cookie=alice)
        self.assertEqual(status, 200)
        self.assertNotIn("groups", payload)  # bootstrap 只带轻量全局信息，明细走分页端点
        board = self.board(alice)
        group_id = board["items"][0]["id"]
        self.assertIsNone(board["items"][0]["clicked_at"])
        status, _, _ = self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": True}, alice)
        self.assertEqual(status, 200)
        self.assertIsNotNone(self.board(alice, "status=read")["items"][0]["clicked_at"])
        self.assertEqual(self.board(bob, "status=read")["items"], [])

    def test_board_query_filtering_and_pagination(self) -> None:
        alice = self.login("alice", "alice-code")
        group_id = self.board(alice)["items"][0]["id"]
        # 状态过滤
        self.assertEqual(len(self.board(alice, "status=unread")["items"]), 1)
        self.assertEqual(self.board(alice, "status=read")["items"], [])
        self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": True}, alice)
        self.assertEqual(self.board(alice, "status=unread")["items"], [])
        self.assertEqual(len(self.board(alice, "status=read")["items"]), 1)
        self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": False}, alice)
        # exclude：本会话点开过的文章保留在未读查询里（前端"变暗保留"语义）
        self.assertEqual(self.board(alice, f"status=unread&exclude={group_id}")["items"], [])
        # chips 过滤：未筛选命中 / 非招聘信息不命中；screens 为 kept 类条件时不命中
        self.assertEqual(len(self.board(alice, "screens=none")["items"]), 1)
        self.assertEqual(self.board(alice, "screens=skipped")["items"], [])
        self.assertEqual(self.board(alice, "screens=cs")["items"], [])
        # 搜索（中文需 URL 编码后发给 http.client）
        self.assertEqual(len(self.board(alice, f"q={quote('测试招聘')}")["items"]), 1)
        self.assertEqual(self.board(alice, f"q={quote('完全不存在的词')}")["items"], [])
        # 分页元信息
        board = self.board(alice, "page_size=1")
        self.assertEqual((board["page"], board["pages"], board["total"], len(board["items"])), (1, 1, 1, 1))
        # chips 计数跟随状态：已读后，未读查询的计数归零、已读查询恢复
        self.assertEqual(board["counts"]["none"], 1)
        self.assertEqual(self.board(alice)["counts"]["skipped"], 0)
        self.call("POST", "/api/clicks", {"group_id": group_id, "clicked": True}, alice)
        self.assertEqual(self.board(alice, "status=unread")["counts"]["none"], 0)
        self.assertEqual(self.board(alice, "status=read")["counts"]["none"], 1)

    def test_group_click_persists_all_member_urls_and_can_clear(self) -> None:
        alice = self.login("alice", "alice-code")
        group_id = self.board(alice)["items"][0]["id"]
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
        board = self.board(alice, "status=read")
        self.assertEqual(board["items"][0]["clicked_at"], "2026-08-01T00:00:00+08:00")

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
                     "positions": [{"name": "软件工程师", "category": "软件开发", "location": "成都"}],
                     "apply_url": "https://example.com/apply", "article_url": self.urls[0], "urls": self.urls,
                     "v": 2},
            "skipped": {"account": "测试就业", "title": "测试招聘",
                        "article_url": self.urls[0], "urls": self.urls, "reason": "非招聘信息", "v": 2},
        }[kind]
        days = ({} if kind == "empty"
                else {"kept": [entry] if kind == "kept" else [], "skipped": [entry] if kind == "skipped" else []})
        self.screen_results.write_text(
            json.dumps({"days": {"2026-08-07": days}}, ensure_ascii=False), encoding="utf-8")
        stamp = time.time() + mtime_offset
        os.utime(self.screen_results, (stamp, stamp))

    def write_fair_results(self, mtime_offset: float) -> None:
        """把唯一一组文章标记为宣讲会（AI fair 字段），验证看板/宣讲会的分流。"""
        entry = {"unit": "测试公司", "intro": "一句话简介", "title": "测试招聘",
                 "accounts": ["测试就业"], "recruit_target": "2027届",
                 "positions": [], "apply_url": "https://example.com/apply",
                 "article_url": self.urls[0], "urls": self.urls, "v": 2,
                 "fair": {"is_fair": True, "time": "8月7日 14:00", "location": "四川省成都市",
                          "province": "四川", "multi_company": True, "date": "2026-08-07"}}
        self.screen_results.write_text(
            json.dumps({"days": {"2026-08-07": {"kept": [entry], "skipped": []}}}, ensure_ascii=False),
            encoding="utf-8")
        stamp = time.time() + mtime_offset
        os.utime(self.screen_results, (stamp, stamp))

    def test_screen_results_merged_into_board(self) -> None:
        alice = self.login("alice", "alice-code")
        board = self.board(alice)
        self.assertIsNone(board["items"][0]["screen"])
        self.write_screen_results("kept", 100)
        screen = self.board(alice)["items"][0]["screen"]
        self.assertEqual(screen["kind"], "kept")
        self.assertEqual(screen["unit"], "测试公司")
        self.assertEqual(screen["positions"][0]["name"], "软件工程师")
        self.assertEqual(screen["screened_date"], "2026-08-07")
        self.write_screen_results("skipped", 200)
        screen = self.board(alice)["items"][0]["screen"]
        self.assertEqual(screen["kind"], "skipped")
        self.assertEqual(screen["reason"], "非招聘信息")

    def test_fairs_endpoint_and_board_split(self) -> None:
        alice = self.login("alice", "alice-code")
        # 无筛选数据：普通文章在看板、宣讲会端点为空
        self.assertEqual(len(self.board(alice)["items"]), 1)
        status, fairs, _ = self.call("GET", "/api/fairs", cookie=alice)
        self.assertEqual((status, fairs["total"]), (200, 0))
        # AI 标记为宣讲会后：从看板消失，进入宣讲会端点（带 fair 字段与已读状态）
        self.write_fair_results(100)
        self.assertEqual(self.board(alice)["items"], [])
        status, fairs, _ = self.call("GET", "/api/fairs", cookie=alice)
        self.assertEqual(fairs["total"], 1)
        self.assertEqual(fairs["items"][0]["screen"]["fair"]["date"], "2026-08-07")
        self.assertIsNone(fairs["items"][0]["clicked_at"])
        # 统计随 bootstrap / clicks 轮询下发：宣讲会不计入看板总数
        boot = self.call("GET", "/api/bootstrap", cookie=alice)[1]
        self.assertEqual(boot["stats"]["total"], 0)
        self.assertEqual(boot["stats"]["fair_total"], 1)
        self.assertEqual(boot["accounts"], [])

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
        # 部分更新：只提交 keywords 不影响已保存的 cities
        status, payload, _ = self.call("POST", "/api/preferences", {"keywords": ["安全"]}, alice)
        self.assertEqual(status, 200)
        self.assertEqual(payload["preferences"]["cities"], ["杭州", "成都"])
        self.assertEqual(payload["preferences"]["keywords"], ["安全"])
        # 什么字段都不带应被拒绝
        self.assertEqual(self.call("POST", "/api/preferences", {}, alice)[0], 400)
        # 偏好按用户隔离：bob 仍是默认值
        bob = self.login("bob", "bob-code-1")
        bob_prefs = self.call("GET", "/api/bootstrap", cookie=bob)[1]["preferences"]
        self.assertIn("杭州", bob_prefs["cities"])
        # 显式清空后不再回落到默认
        status, payload, _ = self.call("POST", "/api/preferences", {"cities": [], "keywords": []}, alice)
        self.assertEqual(payload["preferences"], {"cities": [], "keywords": []})

    def test_bootstrap_gzip_and_geo(self) -> None:
        alice = self.login("alice", "alice-code")
        # 声明 gzip 的大响应应压缩（Content-Encoding: gzip，解压后可解析）
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/api/bootstrap", headers={"Cookie": alice, "Accept-Encoding": "gzip"})
        response = connection.getresponse()
        raw = response.read()
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        payload = json.loads(gzip.decompress(raw))
        self.assertIn("stats", payload)
        self.assertIn("geo", payload)
        connection.close()
        # 不声明 gzip 的客户端拿原始 JSON（老脚本兼容）
        boot = self.call("GET", "/api/bootstrap", cookie=alice)[1]
        # geo 单一事实源：省份/城市/公众号映射随 bootstrap 下发
        self.assertIn("北京", boot["geo"]["provinces"])
        self.assertEqual(boot["geo"]["city_provinces"]["成都"], "四川")
        self.assertEqual(boot["geo"]["account_provinces"]["人大就业创业"], "北京")

    def test_favorites_and_collections(self) -> None:
        self.assertEqual(self.call("GET", "/api/favorites")[0], 401)
        self.assertEqual(self.call("POST", "/api/favorites", {"group_id": "0" * 24, "favorited": True})[0], 401)
        self.assertEqual(self.call("POST", "/api/collections", {"action": "create", "name": "成都"})[0], 401)
        alice = self.login("alice", "alice-code")
        group_id = self.board(alice)["items"][0]["id"]
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

    def test_favorite_apply_url_snapshot_and_applied_toggle(self) -> None:
        self.assertEqual(self.call("POST", "/api/groups/applied", {"group_id": "0" * 24, "status": "applied"})[0], 401)
        alice = self.login("alice", "alice-code")
        group_id = self.board(alice)["items"][0]["id"]
        self.call("POST", "/api/favorites", {"group_id": group_id, "favorited": True}, alice)
        listing = self.call("GET", "/api/favorites", cookie=alice)[1]
        self.assertEqual(listing["items"][0]["apply_url"], "")
        # AI 筛选产出报名链接后再收藏（移动分组同路径），快照随 upsert 刷新
        self.write_screen_results("kept", 100)
        self.call("POST", "/api/favorites", {"group_id": group_id, "favorited": True}, alice)
        listing = self.call("GET", "/api/favorites", cookie=alice)[1]
        self.assertEqual(listing["items"][0]["apply_url"], "https://example.com/apply")
        # 收藏时同时快照 AI 提取的结构化信息，收藏页可展示完整岗位
        snapshot = listing["items"][0]["screen_snapshot"]
        self.assertEqual(snapshot["unit"], "测试公司")
        self.assertEqual(snapshot["positions"][0]["name"], "软件工程师")
        # 投递标记挂在分组级（group_states），无需先收藏；已投递 / 不投递互斥、可取消
        status, payload, _ = self.call(
            "POST", "/api/groups/applied", {"group_id": group_id, "status": "applied"}, alice)
        self.assertEqual(status, 200)
        self.assertEqual(payload["group_states"], {group_id: 1})
        # 未收藏的组也可以标记（看板卡片直接标记）
        other_id = "a" * 24
        status, payload, _ = self.call(
            "POST", "/api/groups/applied", {"group_id": other_id, "status": "skipped"}, alice)
        self.assertEqual(status, 200)
        self.assertEqual(payload["group_states"], {group_id: 1, other_id: 2})
        # 标记已投递/不投递 = 已做决定：整组顺带标已读（从未读列表消失）
        self.assertEqual(len(self.board(alice, "status=read")["items"]), 1)
        self.assertEqual(self.board(alice, "status=unread")["items"], [])
        # 直接切到不投递：与已投递互斥，单列覆盖
        status, payload, _ = self.call(
            "POST", "/api/groups/applied", {"group_id": group_id, "status": "skipped"}, alice)
        self.assertEqual(payload["group_states"][group_id], 2)
        # 再点一次不投递 = 前端发 none 清除标记
        status, payload, _ = self.call(
            "POST", "/api/groups/applied", {"group_id": group_id, "status": "none"}, alice)
        self.assertEqual(payload["group_states"].get(group_id, 0), 0)
        # 校验：坏状态值 400、用户隔离（bob 的 group_states 里没有 alice 的标记）
        self.assertEqual(self.call(
            "POST", "/api/groups/applied", {"group_id": group_id, "status": "zzz"}, alice)[0], 400)
        bob = self.login("bob", "bob-code-1")
        self.assertEqual(
            self.call("GET", "/api/favorites", cookie=bob)[1]["group_states"], {})

    def test_favorite_marks_group_read(self) -> None:
        alice = self.login("alice", "alice-code")
        group = self.board(alice)["items"][0]
        self.assertIsNone(group["clicked_at"])
        # 收藏即已读：收藏后整组标记为已读（与点击标题相同效果）
        self.call("POST", "/api/favorites", {"group_id": group["id"], "favorited": True}, alice)
        boot = self.call("GET", "/api/bootstrap", cookie=alice)[1]
        self.assertIsNotNone(self.board(alice, "status=read")["items"][0]["clicked_at"])
        # 取消收藏不会变回未读
        self.call("POST", "/api/favorites", {"group_id": group["id"], "favorited": False}, alice)
        boot = self.call("GET", "/api/bootstrap", cookie=alice)[1]
        self.assertIsNotNone(self.board(alice, "status=read")["items"][0]["clicked_at"])


if __name__ == "__main__":
    unittest.main()
