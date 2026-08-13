import json
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["STOCK_DASHBOARD_DISABLE_NETWORK"] = "1"
os.environ["SENTIBOARD_LLM_PROVIDER"] = "local-keywords"

from llm import LLMAnalyzer
from history import HistoryArchive, evaluate_market_alignment
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

    def test_note_detail_accepts_opencli_yaml_output_on_windows(self):
        raw = """- field: title
  value: CPO主力流出
- field: author
  value: 天线宝宝
- field: content
  value: '扩产带来压力。#CPO #股票'
- field: likes
  value: '21'
- field: comments
  value: '53'
- field: tags
  value: '#CPO, #股票'

'xsec_source' is not recognized as an internal or external command
"""
        detail = AgentReachXHSProvider._normalize_detail(raw)
        self.assertEqual(detail["content"], "扩产带来压力。#CPO #股票")
        self.assertEqual(detail["comments"], 53)
        self.assertEqual(detail["tags"], ["CPO", "股票"])

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

    def test_search_result_keeps_missing_comment_count_as_unknown(self):
        raw = json.dumps([{
            "title": "CPO 今日观察", "author": "作者甲", "likes": "88",
            "published_at": "今天", "url": "https://example.invalid/1",
        }], ensure_ascii=False)
        posts = AgentReachXHSProvider._normalize_search_result(
            raw, {"query": "CPO", "targetType": "sector", "targetName": "CPO"}, 10
        )
        self.assertIsNone(posts[0]["comments"])
        self.assertFalse(posts[0]["commentCountAvailable"])

    def test_global_detail_selection_covers_targets_before_filling_by_likes(self):
        specs = [
            {"targetType": "sector", "targetName": "CPO"},
            {"targetType": "sector", "targetName": "PCB"},
            {"targetType": "sector", "targetName": "机器人"},
        ]
        posts = [
            {"url": "cpo-high", "likes": 1000, "matchedTargets": [specs[0]]},
            {"url": "cpo-low", "likes": 900, "matchedTargets": [specs[0]]},
            {"url": "pcb", "likes": 20, "matchedTargets": [specs[1]]},
            {"url": "robot", "likes": 10, "matchedTargets": [specs[2]]},
        ]
        selected = AgentReachXHSProvider._select_detail_candidates(posts, specs, 3)
        self.assertEqual({post["url"] for post in selected}, {"cpo-high", "pcb", "robot"})

    def test_evidence_levels_and_unknown_comments(self):
        posts = [
            {"title": "标题", "content": "正文观点", "tags": [], "comments": 0, "isDetailed": True},
            {"title": "标题", "content": "", "tags": [], "imageOcrText": "图片观点"},
            {"title": "标题", "content": "", "tags": [], "comments": 0, "isDetailed": False},
        ]
        DashboardService._annotate_evidence(posts)
        self.assertEqual([post["evidenceLevel"] for post in posts], ["A", "B", "C"])
        self.assertIsNone(posts[2]["comments"])
        self.assertFalse(posts[2]["commentCountAvailable"])

    def test_signal_marks_two_evidence_posts_as_preliminary(self):
        posts = [
            {"evidenceLevel": "A", "evidenceWeight": 1.0, "author": "甲", "likes": 100, "comments": None, "sentimentScore": 80},
            {"evidenceLevel": "B", "evidenceWeight": .85, "author": "乙", "likes": 80, "comments": None, "sentimentScore": 70},
        ]
        signal = DashboardService._entity_signal(posts)
        self.assertEqual(signal["sentiment"], "初步看多")
        self.assertEqual(signal["signalTier"], "preliminary")
        self.assertLess(signal["confidence"], 50)

    def test_one_viral_post_cannot_outweigh_three_opposite_posts(self):
        posts = [
            {"evidenceLevel": "A", "evidenceWeight": 1.0, "author": "甲", "likes": 1_000_000, "comments": None, "sentimentScore": 80},
            *[
                {"evidenceLevel": "A", "evidenceWeight": 1.0, "author": f"空方{i}", "likes": 30, "comments": None, "sentimentScore": -70}
                for i in range(3)
            ],
        ]
        signal = DashboardService._entity_signal(posts)
        self.assertEqual(signal["sentiment"], "看空")

    def test_dynamic_stock_pool_can_promote_non_static_constituent(self):
        service = DashboardService()
        service._sector_universe = {"CPO": [{"name": "测试股份", "code": "300999"}]}
        posts = [
            {
                "title": "测试股份 CPO", "content": "订单增长看好", "tags": ["CPO"],
                "author": f"作者{i}", "likes": 30 + i, "comments": None,
                "commentCountAvailable": False, "url": f"p{i}", "isDetailed": True,
            }
            for i in range(3)
        ]
        data = service._load_initial()
        service._merge_live_posts(data, posts)
        service._rank_sector_stocks(data, posts)
        self.assertEqual(data["stocks"][0]["name"], "测试股份")
        self.assertEqual(data["stocks"][0]["posts"], 3)
        self.assertEqual(data["stocks"][0]["sentiment"], "看多")

    def test_backend_unavailable_does_not_start_cooldown_or_erase_cache(self):
        class OfflineProvider:
            def collect(self, *args, **kwargs):
                return [], {"status": "off", "backend": None, "message": "CLI 未连接", "platformRequestAttempted": False}

        service = DashboardService()
        before = dict(service._xhs_samples)
        service._last_xhs_refresh = 0
        service.xhs_provider = OfflineProvider()
        service.refresh()
        self.assertEqual(service._last_xhs_refresh, 0)
        self.assertEqual(service._xhs_samples, before)

    def test_summary_does_not_render_unavailable_comments_as_zero(self):
        service = DashboardService()
        data = service._load_initial()
        summary = service._build_summary(data, [{"url": "a", "comments": None, "commentCountAvailable": False}])
        self.assertIsNone(summary["comments"])
        self.assertEqual(summary["commentCoveragePct"], 0)

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

    def test_history_archive_is_append_only_and_hides_raw_posts_from_api_view(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = HistoryArchive(Path(directory))
            posts = [{"url": "signed-url", "title": "CPO 看多", "published": "2026-08-05"}]
            dashboard = {
                "summary": {"posts": 1, "sentimentScore": 40},
                "sectors": [
                    {"rank": 1, "name": "CPO", "posts": 1, "comments": 2, "score": 40, "sentiment": "看多", "keywords": [], "topStocks": []}
                ],
                "stocks": [],
            }
            captured = datetime(2026, 8, 5, 17, 30, tzinfo=timezone(timedelta(hours=8)))
            first = archive.archive("2026-08-05", posts, dashboard, captured, "test")
            second = archive.archive("2026-08-05", posts, dashboard, captured, "test")
            self.assertEqual(first["snapshotId"], second["snapshotId"])
            self.assertEqual(archive.list_dates()[0]["snapshotCount"], 1)
            public = archive.latest_public_snapshot("2026-08-05")
            self.assertNotIn("posts", public)
            self.assertEqual(public["postCount"], 1)
            private = archive.latest_snapshot("2026-08-05")
            self.assertEqual(private["posts"][0]["url"], "signed-url")

    def test_historical_date_rebuilds_complete_dashboard_without_mutating_today(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DashboardService()
            service.history_archive = HistoryArchive(Path(directory))
            post = {
                "url": "https://example.invalid/history-post",
                "title": "CPO 中际旭创 看多",
                "content": "光模块订单改善",
                "author": "测试作者",
                "published": "2026-08-05",
                "likes": 120,
                "comments": 18,
                "tags": ["CPO", "中际旭创"],
                "images": ["https://sns-webpic-qc.xhscdn.com/history.webp"],
                "isDetailed": True,
            }
            historical = service._prepare_historical_view("2026-08-05", [post])
            captured = datetime(2026, 8, 5, 17, 30, tzinfo=timezone(timedelta(hours=8)))
            service.history_archive.archive("2026-08-05", [post], historical, captured, "test")
            before = service.current()["meta"]["dataDate"]

            selected = service.dashboard_for_date("2026-08-05")

            self.assertEqual(selected["meta"]["mode"], "history")
            self.assertEqual(selected["meta"]["dataDate"], "2026-08-05")
            self.assertEqual(selected["summary"]["posts"], 1)
            cpo = next(item for item in selected["sectors"] if item["name"] == "CPO")
            self.assertEqual(cpo["topPosts"][0]["url"], post["url"])
            self.assertEqual(cpo["topPosts"][0]["images"], post["images"])
            self.assertEqual(service.current()["meta"]["dataDate"], before)
            self.assertIsNone(service.dashboard_for_date("2026-08-04"))

    def test_market_alignment_excludes_neutral_and_flat_entities(self):
        prediction = {
            "sectors": [
                {"name": "CPO", "sentiment": "看多", "score": 50, "posts": 10},
                {"name": "机器人", "sentiment": "看空", "score": -40, "posts": 8},
                {"name": "PCB", "sentiment": "中性", "score": 0, "posts": 6},
            ],
            "stocks": [
                {"name": "中际旭创", "code": "300308", "sentiment": "看多", "score": 60, "posts": 5}
            ],
        }
        outcomes = {
            "CPO": {"changePct": 1.2},
            "机器人": {"changePct": 0.8},
            "PCB": {"changePct": -1.0},
            "300308": {"changePct": 0.1},
        }
        result = evaluate_market_alignment(prediction, outcomes, flat_threshold=0.3)
        self.assertEqual(result["overall"]["evaluable"], 2)
        self.assertEqual(result["overall"]["matches"], 1)
        self.assertEqual(result["overall"]["accuracyPct"], 50.0)
        self.assertEqual(result["overall"]["flat"], 1)
        self.assertEqual(result["overall"]["neutral"], 1)

    def test_market_alignment_includes_preliminary_and_clue_directions(self):
        prediction = {
            "sectors": [
                {"name": "CPO", "sentiment": "初步看多", "score": 42, "posts": 2},
                {"name": "PCB", "sentiment": "线索看空", "score": -30, "posts": 1},
            ],
            "stocks": [],
        }
        outcomes = {
            "CPO": {"changePct": -3.42, "source": "东方财富板块行情"},
            "PCB": {"changePct": -2.70, "source": "东方财富板块行情"},
        }
        result = evaluate_market_alignment(prediction, outcomes)
        self.assertEqual(result["overall"]["evaluable"], 2)
        self.assertEqual(result["overall"]["matches"], 1)
        self.assertEqual(result["rows"][0]["status"], "miss")
        self.assertEqual(result["rows"][0]["signalTier"], "preliminary")

    def test_midday_validation_flags_post_cutoff_sentiment_as_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = HistoryArchive(Path(directory))
            posts_before = [{"url": "before", "title": "CPO", "published": "2026-08-10"}]
            before = {
                "summary": {"posts": 1, "sentimentScore": 0},
                "sectors": [{"name": "CPO", "sentiment": "样本不足", "score": 0, "posts": 1}],
                "stocks": [],
            }
            posts_after = [*posts_before, {"url": "after", "title": "看多", "published": "2026-08-10"}]
            after = {
                "summary": {"posts": 2, "sentimentScore": 50},
                "sectors": [{"name": "CPO", "sentiment": "初步看多", "score": 50, "posts": 2}],
                "stocks": [{"name": "中际旭创", "code": "300308", "sentiment": "线索看多", "score": 60, "posts": 1}],
            }
            tz = timezone(timedelta(hours=8))
            archive.archive("2026-08-10", posts_before, before, datetime(2026, 8, 10, 11, 9, tzinfo=tz), "test")
            archive.archive("2026-08-10", posts_after, after, datetime(2026, 8, 10, 11, 32, tzinfo=tz), "test")

            class FakeQuotes:
                def fetch_symbols(self, symbols):
                    values = {
                        "sh000001": ("上证指数", 0.20),
                        "sz399001": ("深证成指", -1.13),
                        "sz399006": ("创业板指", -2.18),
                        "sh000688": ("科创50", -1.57),
                        "sz300308": ("中际旭创", -7.59),
                    }
                    return {
                        symbol: {
                            "name": values[symbol][0], "price": 1, "changePct": values[symbol][1],
                            "quoteAt": "20260810115100", "quoteDate": "2026-08-10", "source": "腾讯行情",
                        }
                        for symbol in symbols if symbol in values
                    }

            class FakeSectors:
                def fetch(self):
                    return {"CPO": {"name": "CPO", "changePct": -3.42, "source": "东方财富板块行情"}}

            service = DashboardService()
            service.history_archive = archive
            service.quote_provider = FakeQuotes()
            service.sector_quote_provider = FakeSectors()
            with mock.patch("service.china_now", return_value=datetime(2026, 8, 10, 11, 52, tzinfo=tz)):
                validation = service.validate_midday("2026-08-10")

            self.assertEqual(validation["comparisonMode"], "same-day-observation")
            self.assertFalse(validation["eligibleForAccuracy"])
            self.assertTrue(validation["lookaheadWarning"])
            self.assertEqual(validation["strictDirectionalSignals"], 0)
            self.assertEqual(validation["overall"]["accuracyPct"], 0.0)
            public = archive.latest_public_snapshot("2026-08-10")
            self.assertEqual(public["validations"][0]["overall"]["misses"], 2)
            self.assertNotIn("posts", public)


if __name__ == "__main__":
    unittest.main()
