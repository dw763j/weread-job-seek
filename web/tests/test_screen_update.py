from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screen_update import sanitize_apply_url  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
