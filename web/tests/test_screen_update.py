from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screen_lib import FetchBlockedError, build_fair, parse_fair_date, scrape_article  # noqa: E402
from screen_update import is_reusable, sanitize_apply_url  # noqa: E402


class SanitizeApplyUrlTest(unittest.TestCase):
    def test_glued_label_url_and_email(self) -> None:
        # GLM 实际输出过的一类污染串：标签 + 链接 + 空参 + 邮箱拼在一起
        raw = "网申链接：https://hongshan.jobs.feishu.cn/674992/?spread=GWPKWJT&sessionid= ；联系邮箱：humancapital@hongshan.com"
        self.assertEqual(
            sanitize_apply_url(raw),
            "https://hongshan.jobs.feishu.cn/674992/?spread=GWPKWJT&sessionid=",
        )

    def test_two_ascii_urls_picks_longer_path(self) -> None:
        raw = "https://job.hlmg.tech/网申链接：https://a.example.com/campus/apply?code=x"
        self.assertEqual(sanitize_apply_url(raw), "https://a.example.com/campus/apply?code=x")

    def test_clean_url_unchanged(self) -> None:
        raw = "https://xinhecheng1.zhiye.com/campus"
        self.assertEqual(sanitize_apply_url(raw), raw)

    def test_email_only_becomes_mailto(self) -> None:
        self.assertEqual(sanitize_apply_url("简历投递：hr@example.com"), "mailto:hr@example.com")

    def test_empty_falls_back_to_readmore(self) -> None:
        self.assertEqual(sanitize_apply_url("", "https://mp.weixin.qq.com/readmore"), "https://mp.weixin.qq.com/readmore")
        self.assertEqual(sanitize_apply_url("详见公众号菜单", ""), "")

    def test_garbage_without_url_or_email(self) -> None:
        self.assertEqual(sanitize_apply_url("请关注公众号后续通知"), "")


class ParseFairDateTest(unittest.TestCase):
    def test_common_formats(self) -> None:
        self.assertEqual(parse_fair_date("9月12日 14:00-16:30", "2026-09-08"), "2026-09-12")
        self.assertEqual(parse_fair_date("2026-09-12 14:00", "2026-09-08"), "2026-09-12")
        self.assertEqual(parse_fair_date("2026年9月12日", "2026-09-08"), "2026-09-12")
        self.assertEqual(parse_fair_date("9.12 下午两点", "2026-09-08"), "2026-09-12")
        self.assertEqual(parse_fair_date("09-12", "2026-09-08"), "2026-09-12")

    def test_cross_year_and_invalid(self) -> None:
        # 12 月发布的"1月5日"应进一年
        self.assertEqual(parse_fair_date("1月5日 14:00", "2026-12-28"), "2027-01-05")
        # 发布前就已开场的系列活动保持当年（9月8日发的"9月7日起"不进年）
        self.assertEqual(parse_fair_date("9月7日-9月24日 多场", "2026-09-08"), "2026-09-07")
        # 纯时间段 / 非法月日 / 空文本解析不出
        self.assertEqual(parse_fair_date("14:00-16:30", "2026-09-08"), "")
        self.assertEqual(parse_fair_date("13月5日", "2026-09-08"), "")
        self.assertEqual(parse_fair_date("", "2026-09-08"), "")


class BuildFairTest(unittest.TestCase):
    def test_full_fair(self) -> None:
        parsed = {"宣讲会": {"是宣讲会": True, "时间": "9月7日 19:00",
                            "地点": "四川省成都市四川大学就业指导中心", "多公司": False}}
        self.assertEqual(
            build_fair(parsed, "2026-09-06", ["川大就业"]),
            {"is_fair": True, "time": "9月7日 19:00", "location": "四川省成都市四川大学就业指导中心",
             "province": "四川", "multi_company": False, "date": "2026-09-07"},
        )

    def test_province_fallbacks(self) -> None:
        # 地点只写城市 → 城市映射；只写场馆 → 公众号所属高校兜底
        parsed = {"宣讲会": {"是宣讲会": True, "时间": "2026年9月9日 14:20",
                            "地点": "中国人民大学世纪馆（主馆）", "多公司": True}}
        fair = build_fair(parsed, "2026-09-05", ["人大就业创业"])
        self.assertEqual(fair["province"], "北京")
        parsed2 = {"宣讲会": {"是宣讲会": True, "时间": "9月10日 18:30",
                             "地点": "中关村校区教学楼 N308", "多公司": False}}
        self.assertEqual(build_fair(parsed2, "2026-09-07", ["国科大就业"])["province"], "北京")

    def test_not_fair_variants(self) -> None:
        self.assertIsNone(build_fair({"宣讲会": {"是宣讲会": False}}, "2026-09-06", []))
        self.assertIsNone(build_fair({}, "2026-09-06", []))
        self.assertIsNone(build_fair({"宣讲会": "线上招聘"}, "2026-09-06", []))


class IsReusableTest(unittest.TestCase):
    def test_v2_always_reusable(self) -> None:
        self.assertTrue(is_reusable("kept", {"v": 2}, "任意标题"))
        self.assertTrue(is_reusable("skipped", {"v": 2}, "任意标题"))

    def test_v1_kept_reusable_unless_fair_title(self) -> None:
        # v1（旧 prompt）kept 的提取字段同构，可复用……
        self.assertTrue(is_reusable("kept", {}, "中国电子2027届校园招聘"))
        # ……但标题命中宣讲关键词的缺宣讲会字段，需要重析
        self.assertFalse(is_reusable("kept", {}, "线下宣讲 | OPPO2027届全球校园招聘"))
        self.assertFalse(is_reusable("kept", {}, "XX大学2027届双选会"))

    def test_v1_skipped_and_failures_rerun(self) -> None:
        # v1 skipped：旧标准把非计算机类招聘整体跳过且没有提取信息，必须重析
        self.assertFalse(is_reusable("skipped", {}, "某高校教师招聘"))
        # 失败条目（需人工复核/重试）永不复用
        self.assertFalse(is_reusable("skipped", {"reason": "自动分析失败，需人工复核：超时"}, "x"))


class ScrapeBlockedTest(unittest.TestCase):
    def test_wechat_verify_page_raises_after_retries(self) -> None:
        # 模拟微信反爬验证页：正常 200 但没有正文；抓取应抛 FetchBlockedError 而不是
        # 被当成"无文本无图片"的空文章永久跳过（用猴子补丁绕开真实网络与退避等待）
        import screen_lib

        blocked_page = "<html><body>当前环境异常，完成验证后即可继续访问</body></html>"
        original_get, original_sleep = screen_lib.http_get, screen_lib.time.sleep
        screen_lib.http_get = lambda url, timeout=25: blocked_page
        screen_lib.time.sleep = lambda seconds: None
        try:
            with self.assertRaises(FetchBlockedError):
                scrape_article("https://mp.weixin.qq.com/s/blocked")
        finally:
            screen_lib.http_get, screen_lib.time.sleep = original_get, original_sleep


if __name__ == "__main__":
    unittest.main()
