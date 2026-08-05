import json
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["STOCK_DASHBOARD_DISABLE_NETWORK"] = "1"
os.environ["SENTIBOARD_LLM_PROVIDER"] = "local-keywords"

from llm import LLMAnalyzer
from providers import AgentReachXHSProvider, XHSImageOCRProvider, is_xhs_post_today, parse_xhs_published_date
from service import DashboardService


class DashboardServiceTests(unittest.TestCase):
    def test_dashboard_has_required_sections_and_at_most_three_posts(self):
        data = DashboardService().current()
        self.assertEqual(len(data["sectors"]), 6)
        self.assertEqual(len(data["stocks"]), 10)
        self.assertTrue(all(0 <= len(item["topPosts"]) <= 3 for item in data["sectors"]))
        self.assertTrue(all(0 <= len(item["topPosts"]) <= 3 for item in data["stocks"]))
        self.assertEqual(data["meta"]["window"], "今日")

    def test_summary_is_consistent(self):
        service = DashboardService()
        data = service.current()
        summary = service._build_summary(data)
        self.assertEqual(summary["posts"], sum(item["posts"] for item in data["sectors"]))
        self.assertGreaterEqual(summary["sentimentScore"], -100)
        self.assertLessEqual(summary["sentimentScore"], 100)

    def test_note_detail_normalizes_tags_and_engagement(self):
        raw = """[
          {"field":"title","value":"半导体设备起飞"},
          {"field":"likes","value":"1.2万"},
          {"field":"comments","value":"174"},
          {"field":"tags","value":"#半导体设备, #CPO兑现, #股票"}
        ]"""
        detail = AgentReachXHSProvider._normalize_detail(raw)
        self.assertEqual(detail["likes"], 12000)
        self.assertEqual(detail["comments"], 174)
        self.assertEqual(detail["tags"], ["半导体设备", "CPO兑现", "股票"])

    def test_browser_detail_keeps_only_xhs_images(self):
        raw = """{
          "title":"中际旭创今日观察",
          "content":"订单改善",
          "author":"测试作者",
          "likes":"1.2万",
          "comments":"174",
          "tags":["CPO"],
          "images":[
            "https://sns-webpic-qc.xhscdn.com/path/a.webp",
            "https://www.xiaohongshu.com/path/b.webp",
            "https://example.com/tracker.png",
            "javascript:alert(1)"
          ]
        }"""
        detail = AgentReachXHSProvider._normalize_browser_detail(raw)
        self.assertEqual(detail["likes"], 12000)
        self.assertEqual(len(detail["images"]), 2)
        self.assertTrue(all(url.startswith("https://") for url in detail["images"]))

    def test_broad_post_is_classified_by_tag(self):
        service = DashboardService()
        data = service.current()
        post = {
            "title": "今天的产业链观察",
            "content": "高多层板订单继续改善",
            "author": "测试作者",
            "likes": 100,
            "comments": 20,
            "tags": ["PCB", "AI服务器"],
            "url": "https://example.invalid/post",
            "published": "刷新时采集",
            "isDetailed": True,
        }
        merged = service._merge_live_posts(data, [post])
        pcb = next(item for item in merged["sectors"] if item["name"] == "PCB")
        self.assertEqual(pcb["dataSource"], "live")
        self.assertEqual(pcb["posts"], 1)
        self.assertEqual(pcb["topPosts"][0]["url"], post["url"])

    def test_image_ocr_classifies_sector_and_sentiment(self):
        service = DashboardService()
        data = service.current()
        post = {
            "title": "今天盘面记录",
            "content": "详细观点见配图",
            "author": "测试作者",
            "likes": 100,
            "comments": 20,
            "tags": [],
            "imageOcrText": "存储芯片半导体CPO大反弹",
            "ocrImageCount": 1,
            "url": "https://example.invalid/image-post",
            "published": "今天",
            "isDetailed": True,
        }
        merged = service._merge_live_posts(data, [post])
        semiconductor = next(item for item in merged["sectors"] if item["name"] == "半导体")
        cpo = next(item for item in merged["sectors"] if item["name"] == "CPO")
        self.assertEqual(semiconductor["posts"], 1)
        self.assertEqual(cpo["posts"], 1)
        self.assertEqual(post["sentiment"], "看多")

    def test_date_parser_accepts_only_current_china_day(self):
        now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(parse_xhs_published_date("2026-08-05 09:31", now).isoformat(), "2026-08-05")
        self.assertEqual(parse_xhs_published_date("2小时前", now).isoformat(), "2026-08-05")
        self.assertEqual(parse_xhs_published_date("昨天 22:00", now).isoformat(), "2026-08-04")
        self.assertIsNone(parse_xhs_published_date("刷新时采集", now))
        self.assertTrue(is_xhs_post_today({"published": "今天"}, now))
        self.assertFalse(is_xhs_post_today({"published": "日期不明"}, now))

    def test_daily_view_excludes_old_and_unknown_posts(self):
        service = DashboardService()
        now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        base = {
            "title": "PCB 今日观察",
            "content": "高多层板订单改善",
            "author": "测试作者",
            "likes": 100,
            "comments": 20,
            "tags": ["PCB"],
            "isDetailed": True,
        }
        service._xhs_samples = {
            "today": {**base, "url": "today", "published": "2026-08-05"},
            "old": {**base, "url": "old", "published": "2026-08-04"},
            "unknown": {**base, "url": "unknown", "published": "日期不明"},
        }
        data = service._prepare_daily_view(service._load_initial(), now=now)
        pcb = next(item for item in data["sectors"] if item["name"] == "PCB")
        self.assertEqual(data["summary"]["posts"], 1)
        self.assertEqual(pcb["posts"], 1)
        self.assertEqual([post["url"] for post in pcb["topPosts"]], ["today"])

    def test_sector_stock_ranking_uses_text_tags_and_image_ocr(self):
        service = DashboardService()
        service._sector_universe = {
            "CPO": [
                {"name": "中际旭创", "code": "300308"},
                {"name": "新易盛", "code": "300502"},
                {"name": "天孚通信", "code": "300394"},
            ]
        }
        posts = [
            {
                "targetType": "sector", "targetName": "CPO",
                "title": "中际旭创 中际旭创", "content": "CPO", "tags": [],
                "imageOcrText": "新易盛 新易盛", "ocrImageCount": 2,
                "likes": 100, "comments": 20,
            },
            {
                "targetType": "sector", "targetName": "CPO",
                "title": "今天的光模块", "content": "产业跟踪", "tags": ["新易盛"],
                "imageOcrText": "", "ocrImageCount": 0, "likes": 50, "comments": 10,
            },
        ]
        data = service._load_initial()
        service._rank_sector_stocks(data, posts)
        cpo = next(item for item in data["sectors"] if item["name"] == "CPO")
        self.assertEqual([item["name"] for item in cpo["topStocks"]], ["新易盛", "中际旭创"])
        self.assertEqual(cpo["topStocks"][0]["postMentions"], 2)
        self.assertEqual(cpo["topStocks"][0]["imageMentions"], 1)
        self.assertEqual(cpo["topStocks"][0]["engagement"], 180)
        self.assertEqual(cpo["topStocks"][1]["postMentions"], 1)
        self.assertEqual(cpo["topStocks"][1]["imageMentions"], 0)
        self.assertEqual(cpo["stockRankingCoverage"]["ocrImages"], 2)

    def test_image_prefilter_keeps_a_few_words_and_skips_blank_image(self):
        import numpy as np
        from PIL import Image, ImageDraw

        short_text = Image.new("RGB", (420, 180), "#fffde8")
        draw = ImageDraw.Draw(short_text)
        draw.text((40, 70), "CPO 300308", fill="#202020", stroke_width=1)
        blank = Image.new("RGB", (420, 180), "#fffde8")

        self.assertTrue(XHSImageOCRProvider._has_any_text(short_text, np))
        self.assertFalse(XHSImageOCRProvider._has_any_text(blank, np))

    def test_ocr_normalizes_common_cpo_punctuation_error(self):
        value = XHSImageOCRProvider._normalize_ocr_text("半 导 体，cp。存 储 芯 片")
        self.assertIn("半导体", value)
        self.assertIn("CPO", value)
        self.assertIn("存储芯片", value)

    def test_llm_adapter_sends_no_author_or_url_and_applies_structured_result(self):
        class FakeAnalyzer(LLMAnalyzer):
            def __init__(self):
                self.provider = "fake"
                self.prompt = ""

            def _invoke(self, prompt: str) -> str:
                self.prompt = prompt
                return json.dumps(
                    {
                        "items": [
                            {
                                "id": "0",
                                "sentiment": "看多",
                                "sentimentScore": 66,
                                "sectors": ["CPO"],
                                "stocks": ["中际旭创"],
                                "summary": "订单预期改善",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

        posts = [
            {
                "title": "光模块观察",
                "content": "中际旭创订单改善",
                "tags": ["CPO"],
                "imageOcrText": "800G",
                "author": "不应发送的作者",
                "url": "https://example.invalid/private-signed-url",
            }
        ]
        analyzer = FakeAnalyzer()
        state = analyzer.enrich_posts(posts)
        self.assertEqual(state["status"], "ok")
        self.assertEqual(posts[0]["aiSentimentScore"], 66)
        self.assertEqual(posts[0]["aiStocks"], ["中际旭创"])
        self.assertNotIn("不应发送的作者", analyzer.prompt)
        self.assertNotIn("private-signed-url", analyzer.prompt)


if __name__ == "__main__":
    unittest.main()
