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
from eastmoney_guba import EastmoneyGubaProvider, GubaTechnicalClassifier
from eastmoney_browser_session import VerificationRequired
from history import HistoryArchive, evaluate_market_alignment
from providers import (
    AgentReachXHSProvider,
    EastmoneySectorConstituentProvider,
    XHSImageOCRProvider,
    is_xhs_post_today,
    parse_xhs_published_date,
)
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

    def test_agent_reach_is_found_beside_virtualenv_python(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "agent-reach.exe"
            executable.write_bytes(b"")
            with mock.patch("providers.shutil.which", return_value=None), mock.patch(
                "providers.sys.executable", str(Path(directory) / "python.exe")
            ):
                self.assertEqual(
                    AgentReachXHSProvider._agent_reach_executable(), str(executable)
                )

    def test_sector_constituents_work_without_scrapling(self):
        payload = json.dumps({
            "data": {"diff": [{"f12": "600000", "f14": "浦发银行"}]}
        }).encode("utf-8")
        with mock.patch.dict(os.environ, {"STOCK_DASHBOARD_DISABLE_NETWORK": "0"}), mock.patch(
            "providers.fetch_public_bytes", return_value=payload
        ):
            sectors = EastmoneySectorConstituentProvider().fetch()
        self.assertTrue(sectors)
        self.assertTrue(all(rows[0]["code"] == "600000" for rows in sectors.values()))

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

    def test_first_refresh_of_new_day_clears_old_hierarchy_and_detail_caches(self):
        service = DashboardService()
        service._eastmoney_data = {
            "meta": {"dataDate": "2026-08-12"},
            "summary": {"stocks": 1},
            "sectors": [{"name": "昨日板块", "stocks": [{"_windowRows": ["large-old-row"]}]}],
        }
        service._eastmoney_details = {
            "v4:2026-08-12:verified:600000": {"posts": ["yesterday"]},
            "v4:2026-08-13:verified:600001": {"posts": ["today"]},
        }

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "eastmoney_cache.json"
            detail_path = Path(directory) / "eastmoney_stock_details.json"
            with mock.patch("service.EASTMONEY_CACHE_PATH", cache_path), mock.patch(
                "service.EASTMONEY_DETAIL_CACHE_PATH", detail_path
            ):
                cleared = service._clear_previous_eastmoney_cache("2026-08-13")

            self.assertTrue(cleared)
            self.assertEqual(service._eastmoney_data["meta"]["dataDate"], "2026-08-13")
            self.assertEqual(service._eastmoney_data["meta"]["previousDataDate"], "2026-08-12")
            self.assertTrue(service._eastmoney_data["meta"]["previousDayCacheCleared"])
            self.assertEqual(service._eastmoney_data["sectors"], [])
            self.assertEqual(
                list(service._eastmoney_details), ["v4:2026-08-13:verified:600001"]
            )
            self.assertNotIn("large-old-row", cache_path.read_text(encoding="utf-8"))
            self.assertNotIn("yesterday", detail_path.read_text(encoding="utf-8"))

    def test_same_day_refresh_does_not_clear_current_cache(self):
        service = DashboardService()
        current_data = {
            "meta": {"dataDate": "2026-08-13"},
            "sectors": [{"name": "今日板块"}],
        }
        current_details = {
            "v4:2026-08-13:verified:600001": {"posts": ["today"]},
        }
        service._eastmoney_data = current_data
        service._eastmoney_details = current_details

        self.assertFalse(service._clear_previous_eastmoney_cache("2026-08-13"))
        self.assertIs(service._eastmoney_data, current_data)
        self.assertIs(service._eastmoney_details, current_details)

    def test_failed_new_day_refresh_never_restores_yesterday_cache(self):
        service = DashboardService()
        service._eastmoney_data = {
            "meta": {"dataDate": "2026-08-12"},
            "sectors": [{"name": "昨日板块", "stocks": []}],
        }
        service._eastmoney_details = {
            "v4:2026-08-12:verified:600000": {"posts": ["yesterday"]},
        }
        service._last_eastmoney_refresh = 0
        failed = service.eastmoney_provider.empty_hierarchy("采集暂不可用")
        service.eastmoney_provider.collect_hierarchy = mock.Mock(return_value=failed)
        tz = timezone(timedelta(hours=8))

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "eastmoney_cache.json"
            detail_path = Path(directory) / "eastmoney_stock_details.json"
            request_path = Path(directory) / "eastmoney_request_state.json"
            with mock.patch("service.EASTMONEY_CACHE_PATH", cache_path), mock.patch(
                "service.EASTMONEY_DETAIL_CACHE_PATH", detail_path
            ), mock.patch("service.EASTMONEY_REQUEST_STATE_PATH", request_path), mock.patch(
                "service.china_now", return_value=datetime(2026, 8, 13, 8, 0, tzinfo=tz)
            ):
                result = service.refresh_eastmoney()

            self.assertEqual(result["meta"]["status"], "warn")
            self.assertTrue(result["meta"]["previousDayCacheCleared"])
            self.assertIn("前一日缓存已清空", result["meta"]["message"])
            self.assertEqual(result["sectors"], [])
            self.assertEqual(service._eastmoney_details, {})
            self.assertNotIn("昨日板块", cache_path.read_text(encoding="utf-8"))


class EastmoneyGubaTests(unittest.TestCase):
    ROW = """<tr class="listitem"><td><div class="read">{reads}</div></td>
      <td><div class="reply">{comments}</div></td><td><div class="title">
      <a data-postid="{postid}" data-posttype="0" href="/news,600667,{postid}.html">{title}</a></div></td>
      <td><div class="author"><a href="//i.eastmoney.com/u">作者甲</a></div></td>
      <td><div class="update">{published}</div></td></tr>"""

    def test_classifier_requires_article_form_and_rejects_venting(self):
        content = (
            "一、太极实业今日分时回踩20日线后出现承接，上方压力区域仍需成交量配合确认，当前趋势保持震荡修复。"
            "二、成交量较昨日明显放大，但主力资金仍有净流出，筹码交换充分，短线资金承接与高位抛压同时存在。"
            "三、交易计划是不追高，只有回踩支撑并缩量企稳才观察；若放量跌破防守位则执行止损，控制仓位和风险。"
            "四、基本面订单和业绩需要后续公告验证，以上只是个人复盘，不构成投资建议。"
        )
        accepted = GubaTechnicalClassifier.classify("太极实业今日走势与资金复盘", content)
        short = GubaTechnicalClassifier.classify("太极实业回踩20日线", "回踩支撑，可能反弹。")
        rejected = GubaTechnicalClassifier.classify("垃圾狗庄快涨停！", "气死我了，快跑")
        self.assertTrue(accepted["analysisEligible"])
        self.assertIn("技术面", accepted["analysisTypes"])
        self.assertIn("资金面", accepted["analysisTypes"])
        self.assertFalse(short["analysisEligible"])
        self.assertIn("非文章型内容", short["rejectionReason"])
        self.assertFalse(rejected["analysisEligible"])
        self.assertEqual(rejected["rejectionReason"], "含辱骂或攻击性表达")

    def test_classifier_accepts_continuous_short_article_like_user_example(self):
        content = (
            "三环集团、博迁新材和锐捷网络今天表现类似。"
            "前期板块爆发不佳，但现在抗跌性已经出来。"
            "国产算力是长趋势，资本开支支撑整个主线，所以今天都有冲高。"
            "后面观察大盘什么时候放量，确认后再收紧仓位保利润。"
        )
        result = GubaTechnicalClassifier.classify("三环集团盘面观察", content)
        self.assertTrue(result["analysisEligible"])
        self.assertGreaterEqual(result["articleStats"]["sentences"], 3)

    def test_only_analytical_author_replies_are_kept(self):
        payload = {
            "rc": 0,
            "count": 2,
            "manager_comment_count": 2,
            "re": [
                {"reply_id": 1, "reply_text": "这次反弹仍然缩量，后面只有放量突破主线压力才算市场确认。", "reply_publish_time": "2026-08-13 10:27:00", "reply_like_count": 3, "reply_user": {"user_id": "u1"}},
                {"reply_id": 2, "reply_text": "收到，谢谢。", "reply_publish_time": "2026-08-13 10:28:00", "reply_like_count": 0, "reply_user": {"user_id": "u1"}},
            ],
        }
        provider = EastmoneyGubaProvider(
            fetcher=lambda url, timeout: json.dumps(payload, ensure_ascii=False).encode(),
            sleeper=lambda _: None,
        )
        replies, requests = provider.collect_author_replies(
            {"postId": "123", "authorId": "u1", "commentCount": 2}
        )
        self.assertEqual(requests, 1)
        self.assertEqual(len(replies), 1)
        self.assertIn("放量突破", replies[0]["content"])

    def test_guidance_separates_trade_action_outlook_and_conditions(self):
        guidance = GubaTechnicalClassifier.extract_guidance(
            "国产算力仍是主线，后续走势偏强。如果大盘放量突破压力位，可以继续持有；若跌破支撑则减仓止盈，不要追高。"
        )
        self.assertEqual(guidance["tradeBias"], "条件交易/分批处理")
        self.assertEqual(guidance["outlook"], "偏强")
        self.assertTrue(any("放量突破" in item for item in guidance["conditions"]))

    def test_latest_post_pagination_stops_only_after_crossing_six(self):
        page_one = (
            self.ROW.format(reads=12, comments=2, postid=1, title="分时走势与资金复盘", published="08-13 07:20")
            + self.ROW.format(reads=5, comments=0, postid=2, title="凌晨旧帖", published="08-13 05:50")
        )
        page_two = self.ROW.format(
            reads=3, comments=0, postid=3, title="更早帖子", published="08-13 04:20"
        )
        pages = {
            "https://guba.eastmoney.com/list,600667.html": page_one.encode(),
            "https://guba.eastmoney.com/list,600667_2.html": page_two.encode(),
        }
        provider = EastmoneyGubaProvider(fetcher=lambda url, timeout: pages[url], sleeper=lambda _: None)
        now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        result = provider.scan_stock_window({"code": "600667", "name": "太极实业"}, now)
        self.assertTrue(result["windowComplete"])
        self.assertEqual(result["pagesScanned"], 2)
        self.assertEqual([row["postId"] for row in result["rows"]], ["1"])

    def test_detail_parser_keeps_public_body_and_images(self):
        payload = {
            "post_title": "资金与趋势复盘",
            "post_content": '<p>回踩支撑后放量</p><img src="https://gbres.dfcfw.com/a.jpg">',
            "post_publish_time": "2026-08-13 09:15:00",
            "post_click_count": 120,
            "post_comment_count": 8,
            "post_like_count": 13,
            "post_user": {"user_nickname": "研究者"},
            "post_guba": {"stockbar_code": "600667"},
        }
        detail = EastmoneyGubaProvider.parse_detail(
            f"<script>var post_article={json.dumps(payload, ensure_ascii=False)}</script>"
        )
        self.assertEqual(detail["content"], "回踩支撑后放量")
        self.assertEqual(detail["images"], ["https://gbres.dfcfw.com/a.jpg"])
        self.assertEqual(detail["likeCount"], 13)

    def test_caifuhao_article_parser_keeps_original_time_body_and_images(self):
        page = """
        <h1 class="article-title">亨通光电盘面分析</h1>
        <div class="article-meta">
          <a href="https://i.eastmoney.com/123456#zhuanlan" class="auth name">研究者</a>
          <span class="txt">2026年08月13日 08:35</span>
        </div>
        <span class="zancout text-primary">9</span><span class="plcount">4</span>
        <div class="article-body"><div class="xeditor_content cfh_web">
          <p><span data-stockcode="600487">亨通光电</span>回踩支撑后放量。</p>
          <p>若站稳压力位则继续持有。</p>
          <img src="https://gbres.dfcfw.com/analysis.jpg">
        </div><div id="zwaddcontent"></div>
        """
        detail = EastmoneyGubaProvider.parse_detail(
            page, "https://caifuhao.eastmoney.com/news/1"
        )
        self.assertEqual(detail["sourceType"], "财富号文章")
        self.assertEqual(detail["publishedAt"], "2026-08-13 08:35")
        self.assertIn("若站稳压力位", detail["content"])
        self.assertEqual(detail["authorId"], "123456")
        self.assertEqual(detail["images"], ["https://gbres.dfcfw.com/analysis.jpg"])

    def test_original_publish_time_must_be_at_or_after_six(self):
        now = datetime(2026, 8, 13, 11, 30, tzinfo=timezone(timedelta(hours=8)))
        before = EastmoneyGubaProvider._published_datetime("2026年08月13日 05:59", now)
        after = EastmoneyGubaProvider._published_datetime("2026-08-13 06:00:00", now)
        cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
        self.assertLess(before, cutoff)
        self.assertEqual(after, cutoff)

    def test_multi_stock_article_context_does_not_mix_other_stock_actions(self):
        content = (
            "永鼎股份若不能回封我不会追，放量突破后才考虑买入。\n"
            "亨通光电上方套牢盘仍重，后续要看板块能否连续放量；若跌破支撑则减仓。\n"
            "甘李药业是今天新增的仓位，等待研发节点。"
        )
        context = EastmoneyGubaProvider._stock_context(
            content, {"name": "亨通光电", "code": "600487"}
        )
        self.assertIn("亨通光电", context)
        self.assertIn("若跌破支撑则减仓", context)
        self.assertNotIn("永鼎股份", context)
        self.assertNotIn("甘李药业", context)

    def test_detail_prefilter_caps_requests_and_prioritizes_analysis_titles(self):
        provider = EastmoneyGubaProvider(sleeper=lambda _: None)
        provider.detail_candidate_limit = 5
        rows = [
            {"url": f"https://guba.eastmoney.com/news,600487,{index}.html", "title": "涨了" if index < 8 else "亨通光电放量突破后的支撑压力和交易计划分析", "postType": 0, "readCount": index, "commentCount": 0}
            for index in range(10)
        ]
        selected = provider._select_detail_candidates(rows)
        self.assertEqual(len(selected), 5)
        self.assertIn("交易计划", selected[0]["title"])

    def test_verified_browser_session_can_supply_ordinary_guba_body(self):
        content = (
            "亨通光电今日回踩支撑后出现承接，成交量温和放大，短线趋势仍处于修复阶段。"
            "主力资金回流但上方套牢盘仍重，后续需要观察能否放量突破压力位。"
            "交易计划是不追高，若缩量企稳则继续持有；一旦跌破支撑就减仓止损并控制风险。"
        )
        payload = {
            "post_title": "亨通光电技术面和资金面复盘",
            "post_content": f"<p>{content}</p>",
            "post_publish_time": "2026-08-13 09:15:00",
            "post_click_count": 120,
            "post_comment_count": 0,
            "post_like_count": 13,
            "post_user": {"user_nickname": "研究者", "user_id": "u1"},
            "post_guba": {"stockbar_code": "600487"},
        }
        page = f"<script>var post_article={json.dumps(payload, ensure_ascii=False)}</script>"
        provider = EastmoneyGubaProvider(browser_fetcher=lambda _: page, sleeper=lambda _: None)
        stock = {
            "code": "600487", "name": "亨通光电", "sector": "通信",
            "scannedPosts": 1, "windowComplete": True, "pagesScanned": 1,
            "_windowRows": [{
                "url": "https://guba.eastmoney.com/news,600487,1.html",
                "title": "亨通光电技术面和资金面复盘", "postType": 0,
                "postId": "1", "readCount": 120, "commentCount": 0,
                "listPublishedAt": "08-13 09:15", "author": "研究者",
                "stockCode": "600487", "stockName": "亨通光电",
            }],
        }
        now = datetime(2026, 8, 13, 11, 0, tzinfo=timezone(timedelta(hours=8)))
        with mock.patch("eastmoney_guba.china_now", return_value=now):
            detail = provider.collect_stock_detail(stock)
        self.assertEqual(detail["quality"]["accepted"], 1)
        self.assertEqual(detail["posts"][0]["guidance"]["tradeBias"], "条件交易/分批处理")

    def test_verification_loss_stops_remaining_detail_requests(self):
        calls = []
        def blocked(url):
            calls.append(url)
            raise VerificationRequired("重新核验")
        provider = EastmoneyGubaProvider(browser_fetcher=blocked, sleeper=lambda _: None)
        stock = {
            "code": "600487", "name": "亨通光电", "scannedPosts": 3,
            "windowComplete": True, "pagesScanned": 1,
            "_windowRows": [
                {"url": f"https://guba.eastmoney.com/news,600487,{index}.html", "title": "亨通光电支撑压力交易计划分析", "postType": 0, "postId": str(index), "readCount": 20, "commentCount": 0, "listPublishedAt": "08-13 09:15", "author": "作者", "stockCode": "600487", "stockName": "亨通光电"}
                for index in range(3)
            ],
        }
        detail = provider.collect_stock_detail(stock)
        self.assertEqual(len(calls), 1)
        self.assertTrue(detail["meta"]["verificationRequired"])


if __name__ == "__main__":
    unittest.main()
