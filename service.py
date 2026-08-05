from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from llm import LLMAnalyzer
from providers import (
    AgentReachXHSProvider,
    EastmoneySectorConstituentProvider,
    STOCK_TARGETS,
    TencentQuoteProvider,
    china_now,
    is_xhs_post_today,
)


ROOT = Path(__file__).resolve().parent
FIXTURE_PATH = ROOT / "data" / "demo.json"
CACHE_PATH = ROOT / "data" / "cache.json"
XHS_SAMPLE_CACHE_PATH = ROOT / "data" / "xhs_samples.json"
SECTOR_UNIVERSE_CACHE_PATH = ROOT / "data" / "sector_universe.json"

SECTOR_ALIASES = {
    "半导体": ("半导体", "芯片", "晶圆", "光刻", "存储", "gpu", "ai芯片"),
    "CPO": ("cpo", "光模块", "800g", "1.6t", "硅光"),
    "光通信": ("光通信", "光器件", "光芯片", "光纤", "光网络"),
    "PCB": ("pcb", "印制电路板", "覆铜板", "高多层板", "电路板"),
    "AI 算力": ("ai算力", "算力", "服务器", "数据中心", "液冷", "gpu"),
    "机器人": ("机器人", "人形机器人", "减速器", "丝杠", "伺服"),
}


class DashboardService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.quote_provider = TencentQuoteProvider()
        self.sector_provider = EastmoneySectorConstituentProvider()
        self.xhs_provider = AgentReachXHSProvider()
        self.llm_analyzer = LLMAnalyzer()
        self._data = self._load_initial()
        self._xhs_samples = self._load_xhs_samples()
        self._sector_universe = self._load_sector_universe()
        self._last_xhs_refresh = (
            XHS_SAMPLE_CACHE_PATH.stat().st_mtime if XHS_SAMPLE_CACHE_PATH.exists() else 0.0
        )
        self._xhs_cooldown_seconds = 15 * 60
        self._data = self._prepare_daily_view(self._data)

    def _load_initial(self) -> dict[str, Any]:
        path = CACHE_PATH if CACHE_PATH.exists() else FIXTURE_PATH
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _load_xhs_samples() -> dict[str, dict[str, Any]]:
        if not XHS_SAMPLE_CACHE_PATH.exists():
            return {}
        try:
            payload = json.loads(XHS_SAMPLE_CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            return {
                url: post
                for url, post in payload.items()
                if isinstance(post, dict) and is_xhs_post_today(post)
            }
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _load_sector_universe() -> dict[str, list[dict[str, str]]]:
        if not SECTOR_UNIVERSE_CACHE_PATH.exists():
            return {}
        try:
            payload = json.loads(SECTOR_UNIVERSE_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        cleaned: dict[str, list[dict[str, str]]] = {}
        for sector, rows in payload.items():
            if not isinstance(rows, list):
                continue
            seen: set[str] = set()
            values: list[dict[str, str]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code") or "").strip()
                name = str(row.get("name") or "").strip()
                if len(code) != 6 or not code.isdigit() or not name or code in seen:
                    continue
                seen.add(code)
                values.append({"code": code, "name": name})
            if values:
                cleaned[str(sector)] = values
        return cleaned

    def current(self) -> dict[str, Any]:
        with self._lock:
            # A process may stay alive across midnight. Rebuild on every read so
            # yesterday's posts and quotes disappear without requiring refresh.
            self._xhs_samples = {
                url: post for url, post in self._xhs_samples.items() if is_xhs_post_today(post)
            }
            self._data = self._prepare_daily_view(self._data)
            return copy.deepcopy(self._data)

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            now = china_now()
            today = now.date().isoformat()
            self._xhs_samples = {
                url: post for url, post in self._xhs_samples.items() if is_xhs_post_today(post, now)
            }
            data = self._prepare_daily_view(copy.deepcopy(self._data), now=now)
            date_label = f"{now.month}月{now.day}日"
            broad_specs = [
                {
                    "query": f"今日 {date_label} 半导体 个股 股票",
                    "targetType": "sector", "targetName": "半导体", "detailLimit": 3,
                },
                {
                    "query": f"今日 {date_label} CPO 光模块 个股 股票",
                    "targetType": "sector", "targetName": "CPO", "detailLimit": 3,
                },
                {
                    "query": f"今日 {date_label} 光通信 光器件 个股 股票",
                    "targetType": "sector", "targetName": "光通信", "detailLimit": 3,
                },
                {
                    "query": f"今日 {date_label} PCB 印制电路板 个股 股票",
                    "targetType": "sector", "targetName": "PCB", "detailLimit": 3,
                },
                {
                    "query": f"今日 {date_label} AI 算力 液冷 服务器 个股 股票",
                    "targetType": "sector", "targetName": "AI 算力", "detailLimit": 3,
                },
                {
                    "query": f"今日 {date_label} 机器人 人形机器人 个股 股票",
                    "targetType": "sector", "targetName": "机器人", "detailLimit": 3,
                },
                {
                    "query": f"今日 {date_label} 中际旭创 新易盛 沪电股份 工业富联 寒武纪",
                    "targetType": "stock-batch",
                    "targetName": "股票池 1-5",
                    "detailLimit": 2,
                },
                {
                    "query": f"今日 {date_label} 胜宏科技 兆易创新 中芯国际 光迅科技 生益科技",
                    "targetType": "stock-batch",
                    "targetName": "股票池 6-10",
                    "detailLimit": 2,
                },
            ]
            fresh_universe = self.sector_provider.fetch()
            if fresh_universe:
                self._sector_universe.update(fresh_universe)
                SECTOR_UNIVERSE_CACHE_PATH.write_text(
                    json.dumps(self._sector_universe, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            universe_source = "东方财富公开板块成分（本次刷新）" if fresh_universe else (
                "东方财富公开板块成分（缓存）" if self._sector_universe else "板块成分暂不可用"
            )
            cooldown_remaining = max(
                0,
                round(self._xhs_cooldown_seconds - (time.time() - self._last_xhs_refresh)),
            ) if self._last_xhs_refresh else 0

            if cooldown_remaining:
                xhs_posts = []
                xhs_state = {
                    "status": data.get("meta", {}).get("xhsStatus", "ok"),
                    "backend": data.get("meta", {}).get("xhsBackend", "OpenCLI"),
                    "message": f"账号安全冷却中，约 {max(1, cooldown_remaining // 60)} 分钟后可再次采样",
                    "cooldown": True,
                }
            else:
                xhs_posts, xhs_state = self.xhs_provider.collect(
                    broad_specs,
                    limit=12,
                    detail_limit=3,
                    known_posts=self._xhs_samples,
                )
                self._last_xhs_refresh = time.time()

            llm_state = self.llm_analyzer.enrich_posts(xhs_posts)

            new_posts = 0
            updated_posts = 0
            if not cooldown_remaining:
                for post in (post for post in xhs_posts if is_xhs_post_today(post, now)):
                    url = post.get("url") or ""
                    if not url:
                        continue
                    previous = self._xhs_samples.get(url)
                    if previous is None:
                        new_posts += 1
                    elif previous.get("likes") != post.get("likes") or previous.get("comments") != post.get("comments"):
                        updated_posts += 1
                    self._xhs_samples[url] = post
                # The disk cache is a same-day pool, never a historical archive.
                self._xhs_samples = dict(list(self._xhs_samples.items())[-300:])
                XHS_SAMPLE_CACHE_PATH.write_text(
                    json.dumps(self._xhs_samples, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            data = self._prepare_daily_view(data, now=now)
            data["meta"].update(
                {
                    "sampledTargets": [item["targetName"] for item in broad_specs],
                    "newPosts": new_posts,
                    "updatedPosts": updated_posts,
                    "samplingMode": "今日宽搜增量模式",
                    "sectorUniverseSource": universe_source,
                    "sectorStockFormula": "按今日提及帖数排序；识别标题、正文、tags 与图片 OCR 文字",
                    "ocrStatus": xhs_state.get("ocrStatus", data.get("meta", {}).get("ocrStatus", "not-loaded")),
                    "ocrPosts": xhs_state.get("ocrPosts", data.get("meta", {}).get("ocrPosts", 0)),
                    "ocrImages": xhs_state.get("ocrImages", data.get("meta", {}).get("ocrImages", 0)),
                    "ocrSkippedImages": xhs_state.get("ocrSkippedImages", data.get("meta", {}).get("ocrSkippedImages", 0)),
                    "llmProvider": llm_state.get("provider"),
                    "llmStatus": llm_state.get("status"),
                    "llmMessage": llm_state.get("message"),
                    "llmAnalyzedPosts": llm_state.get("analyzedPosts", 0),
                }
            )

            quotes = self.quote_provider.fetch()
            targets_by_code = {target.code: target for target in STOCK_TARGETS}
            verified_quotes = 0
            for stock in data["stocks"]:
                stock["price"] = None
                stock["changePct"] = None
                target = targets_by_code.get(stock.get("code"))
                if not target:
                    continue
                quote = quotes.get(target.symbol)
                if quote and quote.get("quoteDate") == today:
                    stock["price"] = quote["price"]
                    stock["changePct"] = quote["changePct"]
                    verified_quotes += 1

            meta_update = {
                    "xhsBackend": xhs_state.get("backend"),
                    "xhsStatus": xhs_state.get("status"),
                    "xhsMessage": xhs_state.get("message"),
                    "analysisFields": "标题 + 正文 + tags + 图片 OCR 文字",
                    "marketDate": today if verified_quotes else None,
                    "marketStatus": "today-verified" if verified_quotes else "unavailable",
                    "marketSource": (
                        f"腾讯行情 / Scrapling（今日已验证 {verified_quotes} 只）"
                        if verified_quotes
                        else "今日行情暂不可用（不沿用历史快照）"
                    ),
                    "updatedAt": now.isoformat(timespec="seconds"),
                    "updatedLabel": now.strftime("%m月%d日 %H:%" + "M"),
                }
            data["meta"].update(meta_update)
            data["summary"] = self._build_summary(data, list(self._xhs_samples.values()))
            self._data = data
            CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return copy.deepcopy(data)

    def _prepare_daily_view(
        self, data: dict[str, Any], now: Any | None = None
    ) -> dict[str, Any]:
        current = now or china_now()
        today = current.date().isoformat()
        today_posts = [post for post in self._xhs_samples.values() if is_xhs_post_today(post, current)]

        for sector in data.get("sectors", []):
            sector.update(
                {
                    "posts": 0,
                    "comments": 0,
                    "score": 0,
                    "sentiment": "暂无",
                    "change": 0,
                    "keywords": [],
                    "topPosts": [],
                    "topStocks": [],
                    "stockRankingCoverage": {
                        "postsScanned": 0,
                        "ocrPosts": 0,
                        "ocrImages": 0,
                        "ocrSkippedImages": 0,
                        "constituents": len(self._sector_universe.get(sector.get("name"), [])),
                    },
                    "dataSource": "empty",
                    "sampledAt": None,
                }
            )
        market_is_today = data.get("meta", {}).get("marketDate") == today
        for stock in data.get("stocks", []):
            stock.update(
                {
                    "posts": 0,
                    "comments": 0,
                    "score": 0,
                    "sentiment": "暂无",
                    "heatChange": 0,
                    "topPosts": [],
                    "dataSource": "empty",
                    "sampledAt": None,
                }
            )
            if not market_is_today:
                stock["price"] = None
                stock["changePct"] = None

        if today_posts:
            data = self._merge_live_posts(data, today_posts)
            self._rank_sector_stocks(data, today_posts)

        data["sectors"].sort(key=lambda item: (item.get("posts", 0), item.get("comments", 0)), reverse=True)
        data["stocks"].sort(key=lambda item: (item.get("posts", 0), item.get("comments", 0)), reverse=True)
        for rank, sector in enumerate(data["sectors"], start=1):
            sector["rank"] = rank
        for rank, stock in enumerate(data["stocks"], start=1):
            stock["rank"] = rank

        sampled_count = sum(
            item.get("dataSource") == "live" for item in [*data["sectors"], *data["stocks"]]
        )
        meta = data.setdefault("meta", {})
        meta.update(
            {
                "mode": "live" if today_posts else "empty",
                "modeLabel": "今日公开数据" if today_posts else "今日暂无样本",
                "window": "今日",
                "dataDate": today,
                "datePolicy": "仅纳入发布日期可验证为北京时间今天的帖子",
                "samplePoolSize": len(today_posts),
                "sampledEntityCount": sampled_count,
                "analysisFields": "标题 + 正文 + tags + 图片 OCR 文字",
                "ocrStatus": next(
                    (
                        str(post.get("ocrStatus"))
                        for post in today_posts
                        if post.get("ocrStatus")
                    ),
                    "not-loaded",
                ),
                "ocrPosts": sum(bool(post.get("imageOcrText")) for post in today_posts),
                "ocrImages": sum(int(post.get("ocrImageCount") or 0) for post in today_posts),
                "ocrSkippedImages": sum(
                    int(post.get("ocrSkippedImageCount") or 0) for post in today_posts
                ),
            }
        )
        if not market_is_today:
            meta.update(
                {
                    "marketDate": None,
                    "marketStatus": "unavailable",
                    "marketSource": "今日行情暂不可用（不沿用历史快照）",
                }
            )
        data["summary"] = self._build_summary(data, today_posts)
        return data

    def _rank_sector_stocks(
        self, data: dict[str, Any], posts: list[dict[str, Any]]
    ) -> None:
        def normalize(value: Any) -> str:
            return "".join(str(value or "").lower().split())

        prepared: list[tuple[dict[str, Any], str, str]] = []
        for post in posts:
            tags = " ".join(str(tag) for tag in (post.get("tags") or []))
            ai_terms = " ".join(
                [*(post.get("aiSectors") or []), *(post.get("aiStocks") or [])]
            )
            text = normalize(
                f"{post.get('title', '')} {post.get('content', '')} {tags} {ai_terms}"
            )
            ocr_text = normalize(post.get("imageOcrText"))
            prepared.append((post, text, ocr_text))

        for sector in data.get("sectors", []):
            sector_name = sector.get("name") or ""
            aliases = [normalize(alias) for alias in SECTOR_ALIASES.get(sector_name, (sector_name,))]
            sector_posts = [
                (post, text, ocr_text)
                for post, text, ocr_text in prepared
                if (
                    post.get("targetType") == "sector"
                    and post.get("targetName") == sector_name
                )
                or any(alias and (alias in text or alias in ocr_text) for alias in aliases)
            ]
            ranked: list[dict[str, Any]] = []
            for stock in self._sector_universe.get(sector_name, []):
                stock_aliases = [normalize(stock.get("name")), normalize(stock.get("code"))]
                stock_aliases = [alias for alias in stock_aliases if alias]
                mentioning_posts = [
                    post
                    for post, text, ocr_text in sector_posts
                    if any(alias in text or alias in ocr_text for alias in stock_aliases)
                ]
                post_mentions = len(mentioning_posts)
                if post_mentions <= 0:
                    continue
                image_mentions = sum(
                    any(alias in ocr_text for alias in stock_aliases)
                    for _, _, ocr_text in sector_posts
                )
                engagement = sum(
                    int(post.get("likes") or 0) + int(post.get("comments") or 0)
                    for post in mentioning_posts
                )
                ranked.append(
                    {
                        "name": stock["name"],
                        "code": stock["code"],
                        "postMentions": post_mentions,
                        "imageMentions": image_mentions,
                        "engagement": engagement,
                        "heatScore": post_mentions,
                    }
                )
            ranked.sort(
                key=lambda item: (
                    item["postMentions"], item["engagement"], item["imageMentions"], item["name"]
                ),
                reverse=True,
            )
            sector["topStocks"] = [
                {**item, "rank": index}
                for index, item in enumerate(ranked[:10], start=1)
            ]
            sector["stockRankingCoverage"] = {
                "postsScanned": len(sector_posts),
                "ocrPosts": sum(bool(post.get("imageOcrText")) for post, _, _ in sector_posts),
                "ocrImages": sum(int(post.get("ocrImageCount") or 0) for post, _, _ in sector_posts),
                "ocrSkippedImages": sum(int(post.get("ocrSkippedImageCount") or 0) for post, _, _ in sector_posts),
                "constituents": len(self._sector_universe.get(sector_name, [])),
            }

    @staticmethod
    def _merge_live_posts(data: dict[str, Any], posts: list[dict[str, Any]]) -> dict[str, Any]:
        bullish = (
            "看多", "看好", "突破", "增长", "低估", "机会", "景气", "反转", "反弹",
            "加仓", "抄底", "上涨", "大涨", "起飞", "强势", "拿住", "业绩",
        )
        bearish = (
            "看空", "风险", "高估", "下跌", "减仓", "泡沫", "回撤", "利空", "谨慎",
            "追高", "见顶", "高位", "小心",
        )
        generic_tags = {"股票", "股市", "投资", "理财", "基金", "投资需谨慎", "金融理财"}

        def classify(text: str) -> tuple[str, int]:
            bull = sum(text.count(word) for word in bullish)
            bear = sum(text.count(word) for word in bearish)
            score = max(-100, min(100, (bull - bear) * 22))
            return ("看多" if score > 8 else "看空" if score < -8 else "中性", score)

        for post in posts:
            tags_text = " ".join(tag for tag in (post.get("tags") or []) if tag not in generic_tags)
            ai_label = post.get("aiSentiment")
            ai_score = post.get("aiSentimentScore")
            if ai_label in {"看多", "看空", "中性"} and isinstance(ai_score, int):
                label, score = ai_label, max(-100, min(100, ai_score))
            else:
                label, score = classify(
                    f"{post.get('title', '')} {post.get('content', '')} "
                    f"{tags_text} {post.get('imageOcrText', '')}"
                )
            post["sentiment"] = label
            post["sentimentScore"] = score
            post["engagement"] = post["likes"] + post["comments"]

        def searchable_text(post: dict[str, Any]) -> str:
            tags_text = " ".join(post.get("tags") or [])
            ai_terms = " ".join(
                [*(post.get("aiSectors") or []), *(post.get("aiStocks") or [])]
            )
            return (
                f"{post.get('title', '')} {post.get('content', '')} "
                f"{tags_text} {post.get('imageOcrText', '')} {ai_terms}"
            ).lower().replace(" ", "")

        post_texts = [(post, searchable_text(post)) for post in posts]

        def apply_live(entity: dict[str, Any], entity_type: str) -> None:
            if entity_type == "sector":
                aliases = SECTOR_ALIASES.get(entity["name"], (entity["name"],))
            else:
                aliases = (entity["name"], entity.get("code", ""))
            normalized_aliases = [alias.lower().replace(" ", "") for alias in aliases if alias]
            matches = [
                post
                for post, text in post_texts
                if any(alias in text for alias in normalized_aliases)
            ]
            if matches:
                matches.sort(key=lambda p: p["engagement"], reverse=True)
                detailed = [post for post in matches if post.get("isDetailed")]
                top_posts = sorted(detailed or matches, key=lambda p: p["engagement"], reverse=True)[:3]
                entity["posts"] = len(matches)
                entity["comments"] = sum(p["comments"] for p in matches)
                entity["topPosts"] = top_posts
                entity["dataSource"] = "live"
                entity["sampledAt"] = china_now().isoformat(timespec="seconds")
                weighted = sum(p["sentimentScore"] * max(p["engagement"], 1) for p in matches)
                weight = sum(max(p["engagement"], 1) for p in matches)
                entity["score"] = round(weighted / weight) if weight else 0
                entity["sentiment"] = (
                    "看多" if entity["score"] > 8 else "看空" if entity["score"] < -8 else "中性"
                )
                if entity_type == "sector":
                    tag_counts: dict[str, int] = {}
                    for post in detailed:
                        for tag in post.get("tags") or []:
                            tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if tag_counts:
                        entity["keywords"] = [
                            tag for tag, _ in sorted(tag_counts.items(), key=lambda item: item[1], reverse=True)[:3]
                        ]

        for sector in data["sectors"]:
            apply_live(sector, "sector")
        for stock in data["stocks"]:
            apply_live(stock, "stock")
        return data

    @staticmethod
    def _build_summary(
        data: dict[str, Any], sample_posts: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        if sample_posts is not None:
            unique_posts = {
                post.get("url") or f"local:{index}": post
                for index, post in enumerate(sample_posts)
            }
            total_posts = len(unique_posts)
            total_comments = sum(int(post.get("comments") or 0) for post in unique_posts.values())
            sentiment_stocks = [item for item in data["stocks"] if item.get("dataSource") == "live"]
        else:
            total_posts = sum(item["posts"] for item in data["sectors"])
            total_comments = sum(item["comments"] for item in data["sectors"])
            sentiment_stocks = data["stocks"]
        positive = [item for item in sentiment_stocks if item["sentiment"] == "看多"]
        avg = (
            round(sum(item["score"] for item in sentiment_stocks) / len(sentiment_stocks))
            if sentiment_stocks
            else 0
        )
        return {
            "posts": total_posts,
            "comments": total_comments,
            "bullRatio": round(len(positive) / len(sentiment_stocks) * 100) if sentiment_stocks else 0,
            "sentimentScore": avg,
        }
