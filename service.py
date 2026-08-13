from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from history import HistoryArchive
from llm import LLMAnalyzer
from providers import (
    AgentReachXHSProvider,
    EastmoneySectorConstituentProvider,
    EastmoneySectorQuoteProvider,
    INDEX_SYMBOLS,
    SECTOR_BOARD_CODES,
    TencentQuoteProvider,
    china_now,
    is_xhs_post_today,
    parse_xhs_published_date,
    stock_symbol,
)


ROOT = Path(__file__).resolve().parent
FIXTURE_PATH = ROOT / "data" / "demo.json"
CACHE_PATH = ROOT / "data" / "cache.json"
XHS_SAMPLE_CACHE_PATH = ROOT / "data" / "xhs_samples.json"
REQUEST_STATE_PATH = ROOT / "data" / "request_state.json"
SECTOR_UNIVERSE_CACHE_PATH = ROOT / "data" / "sector_universe.json"
HISTORY_ROOT = ROOT / "data" / "history"

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
        self.sector_quote_provider = EastmoneySectorQuoteProvider()
        self.xhs_provider = AgentReachXHSProvider()
        self.llm_analyzer = LLMAnalyzer()
        self.history_archive = HistoryArchive(HISTORY_ROOT)
        self._data = self._load_initial()
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") != "1":
            connection = self.xhs_provider.status()
            self._data.setdefault("meta", {}).update(
                {
                    "xhsStatus": connection.get("status"),
                    "xhsBackend": connection.get("backend"),
                    "xhsMessage": connection.get("message"),
                }
            )
        cached_samples = self._load_xhs_samples()
        self._xhs_samples = {
            url: post for url, post in cached_samples.items() if is_xhs_post_today(post)
        }
        self._sector_universe = self._load_sector_universe()
        self._last_xhs_refresh = self._load_request_state()
        self._xhs_cooldown_seconds = 15 * 60
        self._data = self._prepare_daily_view(self._data)
        if cached_samples and XHS_SAMPLE_CACHE_PATH.exists():
            captured_at = datetime.fromtimestamp(
                XHS_SAMPLE_CACHE_PATH.stat().st_mtime,
                tz=china_now().tzinfo,
            )
            self._archive_samples(cached_samples, captured_at, "startup-cache")

    def _load_initial(self) -> dict[str, Any]:
        path = CACHE_PATH if CACHE_PATH.exists() else FIXTURE_PATH
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _load_request_state() -> float:
        try:
            payload = json.loads(REQUEST_STATE_PATH.read_text(encoding="utf-8"))
            value = float(payload.get("lastPlatformRequestAt") or 0)
            return value if value > 0 else 0.0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0.0

    def _record_platform_request(self, timestamp: float) -> None:
        self._last_xhs_refresh = timestamp
        REQUEST_STATE_PATH.write_text(
            json.dumps({"lastPlatformRequestAt": timestamp}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
                if isinstance(post, dict)
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
                    "platformRequestAttempted": False,
                    "requestCount": 0,
                }
            else:
                xhs_posts, xhs_state = self.xhs_provider.collect(
                    broad_specs,
                    limit=12,
                    detail_limit=6,
                    known_posts=self._xhs_samples,
                    reuse_discovery=False,
                )
                if xhs_state.get("platformRequestAttempted"):
                    self._record_platform_request(time.time())

            self._annotate_evidence(xhs_posts)
            evidence_posts = [
                post for post in xhs_posts if post.get("evidenceLevel") in {"A", "B"}
            ]
            llm_state = self.llm_analyzer.enrich_posts(evidence_posts)

            new_posts = 0
            updated_posts = 0
            if xhs_posts:
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
                # An unavailable backend or empty failed response never erases it.
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
                    "samplingMode": "今日详情恢复模式" if xhs_state.get("discoveryMode") == "reused" else "今日宽搜增量模式",
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
                    "requestCount": int(xhs_state.get("requestCount") or 0),
                    "searchRequests": int(xhs_state.get("searchRequests") or 0),
                    "detailRequests": int(xhs_state.get("detailRequests") or 0),
                    "selectedForDetail": int(xhs_state.get("selectedForDetail") or 0),
                    "cooldownSeconds": self._xhs_cooldown_seconds,
                    "nextSafeRefreshAt": (
                        datetime.fromtimestamp(
                            self._last_xhs_refresh + self._xhs_cooldown_seconds,
                            tz=now.tzinfo,
                        ).isoformat(timespec="seconds")
                        if self._last_xhs_refresh else None
                    ),
                }
            )

            quotes = self.quote_provider.fetch([str(stock.get("code") or "") for stock in data["stocks"]])
            verified_quotes = 0
            for stock in data["stocks"]:
                stock["price"] = None
                stock["changePct"] = None
                quote = quotes.get(stock_symbol(str(stock.get("code") or "")))
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
            if self._xhs_samples:
                self._archive_samples(self._xhs_samples, now, "manual-refresh")
            return copy.deepcopy(data)

    def history_index(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.history_archive.list_dates())

    def history_snapshot(self, source_date: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self.history_archive.latest_public_snapshot(source_date)
            return copy.deepcopy(snapshot) if snapshot else None

    def validate_midday(self, source_date: str) -> dict[str, Any]:
        """Compare a frozen social snapshot with the current midday market snapshot.

        A snapshot captured after 11:30 may describe a useful same-day
        contradiction, but it is never promoted to a predictive accuracy score.
        """
        with self._lock:
            now = china_now()
            today = now.date().isoformat()
            if source_date != today:
                raise ValueError("目前只能新建当天午盘验证；历史日期使用当日已保存的行情快照")
            if (now.hour, now.minute) < (11, 30):
                raise ValueError("午盘尚未结束，11:30 后才能验证")

            latest = self.history_archive.latest_snapshot(source_date)
            if not latest:
                raise ValueError(f"{source_date} 没有可验证的小红书快照")
            cutoff_at = datetime.fromisoformat(f"{source_date}T11:30:00+08:00")
            strict = self.history_archive.snapshot_at_or_before(source_date, cutoff_at)

            def directional_count(snapshot: dict[str, Any] | None) -> int:
                if not snapshot:
                    return 0
                prediction = snapshot.get("prediction") or {}
                return sum(
                    str(item.get("sentiment") or "").endswith(("看多", "看空"))
                    for item in [*(prediction.get("sectors") or []), *(prediction.get("stocks") or [])]
                )

            strict_signals = directional_count(strict)
            selected = strict if strict_signals else latest
            selected_captured = datetime.fromisoformat(str(selected.get("capturedAt")))
            is_forecast = selected_captured <= cutoff_at and directional_count(selected) > 0
            prediction = selected.get("prediction") or {}
            stock_codes = [
                str(item.get("code") or "")
                for item in prediction.get("stocks") or []
                if str(item.get("code") or "").isdigit()
            ]
            stock_symbols = [stock_symbol(code) for code in stock_codes]
            quotes = self.quote_provider.fetch_symbols([*INDEX_SYMBOLS, *stock_symbols])
            dated_quotes = {
                symbol: quote
                for symbol, quote in quotes.items()
                if quote.get("quoteDate") == source_date
            }
            if not any(symbol in dated_quotes for symbol in INDEX_SYMBOLS):
                raise ValueError("腾讯行情未返回当天指数快照，已拒绝使用旧行情")

            quote_times = [
                str(quote.get("quoteAt") or "")
                for quote in dated_quotes.values()
                if str(quote.get("quoteAt") or "")
            ]
            raw_market_at = max(quote_times) if quote_times else ""
            try:
                market_as_of = datetime.strptime(raw_market_at[:14], "%Y%m%d%H%M%S").replace(
                    tzinfo=now.tzinfo
                ).isoformat(timespec="seconds")
            except ValueError:
                market_as_of = now.isoformat(timespec="seconds")
            quote_hhmm = int(raw_market_at[8:12]) if len(raw_market_at) >= 12 else now.hour * 100 + now.minute
            if quote_hhmm > 1300:
                public = self.history_archive.latest_public_snapshot(source_date) or {}
                canonical = next(
                    (
                        item for item in reversed(public.get("validations") or [])
                        if item.get("isCanonicalMidday")
                    ),
                    None,
                )
                if canonical:
                    return canonical
                raise ValueError("当前已进入下午交易，且没有已保存的午盘行情；已拒绝用下午行情冒充午盘")

            sector_quotes = self.sector_quote_provider.fetch()
            outcomes: dict[str, dict[str, Any]] = {}
            for name, quote in sector_quotes.items():
                outcome = {**quote, "quoteAt": market_as_of}
                outcomes[name] = outcome
            for item in prediction.get("stocks") or []:
                code = str(item.get("code") or "")
                quote = dated_quotes.get(stock_symbol(code))
                if not quote:
                    continue
                outcome = {
                    **quote,
                    "name": quote.get("name") or item.get("name"),
                    "quoteAt": market_as_of,
                }
                outcomes[code] = outcome

            indices = [
                {
                    "symbol": symbol,
                    "name": quote.get("name") or INDEX_SYMBOLS[symbol],
                    "price": quote.get("price"),
                    "changePct": quote.get("changePct"),
                    "quoteAt": market_as_of,
                }
                for symbol in INDEX_SYMBOLS
                if (quote := dated_quotes.get(symbol))
            ]
            sectors = [sector_quotes[name] for name in SECTOR_BOARD_CODES if name in sector_quotes]
            comparison_mode = "forecast" if is_forecast else "same-day-observation"
            metadata = {
                "comparisonMode": comparison_mode,
                "eligibleForAccuracy": is_forecast,
                "predictionCutoff": cutoff_at.isoformat(timespec="seconds"),
                "lookaheadWarning": not is_forecast,
                "strictSnapshotId": strict.get("snapshotId") if strict else None,
                "strictCapturedAt": strict.get("capturedAt") if strict else None,
                "strictDirectionalSignals": strict_signals,
                "marketPeriod": "午盘",
                "isCanonicalMidday": True,
                "marketSource": "腾讯指数/个股 + 东方财富板块",
                "marketSnapshot": {
                    "indices": indices,
                    "sectors": sectors,
                    "trackedSectorUp": sum(float(item.get("changePct") or 0) > 0 for item in sectors),
                    "trackedSectorDown": sum(float(item.get("changePct") or 0) < 0 for item in sectors),
                },
            }
            return self.history_archive.save_validation(
                source_date=source_date,
                outcome_date=source_date,
                market_as_of=market_as_of,
                outcomes=outcomes,
                snapshot_id=selected.get("snapshotId"),
                metadata=metadata,
            )

    def dashboard_for_date(self, source_date: str) -> dict[str, Any] | None:
        """Rebuild a complete read-only dashboard from a private daily archive."""
        with self._lock:
            today = china_now().date().isoformat()
            if source_date == today:
                self._xhs_samples = {
                    url: post for url, post in self._xhs_samples.items() if is_xhs_post_today(post)
                }
                self._data = self._prepare_daily_view(self._data)
                return copy.deepcopy(self._data)

            snapshot = self.history_archive.latest_snapshot(source_date)
            if not snapshot:
                return None
            posts = [post for post in (snapshot.get("posts") or []) if isinstance(post, dict)]
            dashboard = self._prepare_historical_view(source_date, posts)
            captured_at = str(snapshot.get("capturedAt") or "")
            dashboard["meta"].update(
                {
                    "updatedAt": captured_at,
                    "updatedLabel": captured_at[5:16].replace("-", "月").replace("T", "日 ") if captured_at else source_date,
                    "historicalCapturedAt": captured_at,
                    "historicalSnapshotId": snapshot.get("snapshotId"),
                    "historicalPostsSha256": snapshot.get("postsSha256"),
                    "sampledEntityCount": sum(
                        item.get("dataSource") == "live"
                        for item in [*dashboard.get("sectors", []), *dashboard.get("stocks", [])]
                    ),
                }
            )
            return copy.deepcopy(dashboard)

    def _archive_samples(
        self,
        samples: dict[str, dict[str, Any]],
        captured_at: datetime,
        source: str,
    ) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for post in samples.values():
            published_date = parse_xhs_published_date(post.get("published"), captured_at)
            if published_date is not None:
                grouped[published_date.isoformat()].append(post)
        for source_date, posts in grouped.items():
            posts.sort(key=lambda post: str(post.get("url") or ""))
            dashboard = self._prepare_historical_view(source_date, posts)
            self.history_archive.archive(
                source_date=source_date,
                posts=posts,
                dashboard=dashboard,
                captured_at=captured_at,
                source=source,
            )

    def _prepare_daily_view(
        self, data: dict[str, Any], now: Any | None = None
    ) -> dict[str, Any]:
        current = now or china_now()
        today = current.date().isoformat()
        today_posts = [post for post in self._xhs_samples.values() if is_xhs_post_today(post, current)]
        self._annotate_evidence(today_posts)
        market_is_today = data.get("meta", {}).get("marketDate") == today
        self._reset_entities(data, preserve_market=market_is_today)

        if today_posts:
            data = self._merge_live_posts(data, today_posts)
            self._rank_sector_stocks(data, today_posts)

        self._sort_and_rank_entities(data)

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
        meta["dataQuality"] = self._build_data_quality(today_posts, data)
        return data

    def _prepare_historical_view(
        self, source_date: str, posts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        data = copy.deepcopy(self._load_initial())
        self._annotate_evidence(posts)
        self._reset_entities(data, preserve_market=False)
        if posts:
            data = self._merge_live_posts(data, posts)
            self._rank_sector_stocks(data, posts)
        self._sort_and_rank_entities(data)
        data["meta"] = {
            **(data.get("meta") or {}),
            "mode": "history",
            "modeLabel": "历史预测快照",
            "window": "单日",
            "dataDate": source_date,
            "samplePoolSize": len(posts),
            "datePolicy": "按抓取时已确认的北京时间发布日期归档",
            "marketDate": None,
            "marketStatus": "pending-validation",
            "marketSource": "等待后续交易日行情验证",
        }
        data["summary"] = self._build_summary(data, posts)
        data["meta"]["dataQuality"] = self._build_data_quality(posts, data)
        return data

    @staticmethod
    def _annotate_evidence(posts: list[dict[str, Any]]) -> None:
        """Separate discovery-only heat from evidence that may decide direction."""
        for post in posts:
            has_detail_text = bool(str(post.get("content") or "").strip() or post.get("tags"))
            has_ocr = bool(str(post.get("imageOcrText") or "").strip())
            has_title = bool(str(post.get("title") or "").strip())
            if has_detail_text:
                level, weight = "A", 1.0
            elif has_ocr:
                level, weight = "B", 0.85
            elif has_title:
                level, weight = "C", 0.35
            else:
                level, weight = "D", 0.0
            post["evidenceLevel"] = level
            post["evidenceWeight"] = weight
            if "commentCountAvailable" not in post:
                post["commentCountAvailable"] = post.get("comments") is not None and bool(post.get("isDetailed"))
            if not post.get("commentCountAvailable"):
                post["comments"] = None

    @staticmethod
    def _build_data_quality(posts: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
        total = len(posts)
        unique = len({post.get("url") or f"local:{index}" for index, post in enumerate(posts)})
        detailed = sum(bool(post.get("isDetailed")) for post in posts)
        evidence = sum(post.get("evidenceLevel") in {"A", "B"} for post in posts)
        comments_available = sum(bool(post.get("commentCountAvailable")) for post in posts)
        entities = [*data.get("sectors", []), *data.get("stocks", [])]
        covered = [item for item in entities if item.get("dataSource") == "live"]
        qualified = [item for item in covered if item.get("signalTier") == "qualified"]
        preliminary = [item for item in covered if item.get("signalTier") == "preliminary"]
        if total == 0:
            status, label, message = "empty", "暂无样本", "当天尚无可验证帖子。"
        elif evidence == 0:
            status, label, message = "insufficient", "仅标题，情绪不可判定", "标题样本可统计热度，但不能作为方向证据。"
        elif detailed / total >= 0.6 and qualified:
            status, label, message = "ready", "证据可用", "详情证据已覆盖主要样本，仍请结合行情独立判断。"
        elif preliminary:
            status, label, message = "partial", "已有初步情绪线索", "低样本实体以“线索/初步”标注，不冒充高置信度结论。"
        else:
            status, label, message = "partial", "证据部分可用", "仅对达到证据门槛的实体显示方向。"
        return {
            "status": status,
            "label": label,
            "message": message,
            "discoveredPosts": total,
            "uniquePct": round(unique / total * 100) if total else 0,
            "dateValidityPct": 100 if total else 0,
            "detailedPosts": detailed,
            "evidencePosts": evidence,
            "titleOnlyPosts": sum(post.get("evidenceLevel") == "C" for post in posts),
            "commentCoveragePct": round(comments_available / total * 100) if total else 0,
            "imagePosts": sum(bool(post.get("images")) for post in posts),
            "qualifiedEntities": len(qualified),
            "preliminaryEntities": len(preliminary),
            "coveredEntities": len(covered),
            "totalEntities": len(entities),
        }

    def _reset_entities(self, data: dict[str, Any], preserve_market: bool) -> None:
        for sector in data.get("sectors", []):
            sector.update(
                {
                    "posts": 0,
                    "comments": None,
                    "commentsAvailable": False,
                    "score": 0,
                    "sentiment": "样本不足",
                    "confidence": 0,
                    "signalTier": "none",
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
        for stock in data.get("stocks", []):
            stock.update(
                {
                    "posts": 0,
                    "comments": None,
                    "commentsAvailable": False,
                    "score": 0,
                    "sentiment": "样本不足",
                    "confidence": 0,
                    "signalTier": "none",
                    "heatChange": 0,
                    "topPosts": [],
                    "dataSource": "empty",
                    "sampledAt": None,
                }
            )
            if not preserve_market:
                stock["price"] = None
                stock["changePct"] = None

    @staticmethod
    def _sort_and_rank_entities(data: dict[str, Any]) -> None:
        data["sectors"].sort(
            key=lambda item: (int(item.get("posts") or 0), int(item.get("comments") or 0)), reverse=True
        )
        data["stocks"].sort(
            key=lambda item: (int(item.get("posts") or 0), int(item.get("comments") or 0)), reverse=True
        )
        for rank, sector in enumerate(data["sectors"], start=1):
            sector["rank"] = rank
        for rank, stock in enumerate(data["stocks"], start=1):
            stock["rank"] = rank

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

        # The main Top 10 is derived from all current sector constituents, not
        # from the old fixed watchlist. One post contributes at most one mention.
        universe: dict[str, dict[str, Any]] = {}
        for sector_name, stocks in self._sector_universe.items():
            for stock in stocks:
                code = str(stock.get("code") or "")
                if not code:
                    continue
                row = universe.setdefault(
                    code,
                    {"code": code, "name": stock.get("name") or code, "sectors": []},
                )
                if sector_name not in row["sectors"]:
                    row["sectors"].append(sector_name)

        dynamic: list[dict[str, Any]] = []
        for stock in universe.values():
            aliases = [normalize(stock["name"]), normalize(stock["code"])]
            matches = [
                post for post, text, ocr_text in prepared
                if any(alias and (alias in text or alias in ocr_text) for alias in aliases)
            ]
            if not matches:
                continue
            comments = [int(post.get("comments") or 0) for post in matches if post.get("commentCountAvailable")]
            signal = self._entity_signal(matches)
            top_posts = sorted(
                [post for post in matches if post.get("evidenceLevel") in {"A", "B"}] or matches,
                key=lambda post: int(post.get("engagement") or 0),
                reverse=True,
            )[:3]
            capped_heat = sum(
                min(8.0, 1.0 + math.log1p(int(post.get("likes") or 0) + int(post.get("comments") or 0)))
                for post in matches
            )
            dynamic.append(
                {
                    "name": stock["name"],
                    "code": stock["code"],
                    "sector": " / ".join(stock["sectors"][:2]),
                    "price": None,
                    "changePct": None,
                    "heatScore": round(len(matches) * 10 + capped_heat, 1),
                    "heatChange": 0,
                    "posts": len(matches),
                    "comments": sum(comments) if comments else None,
                    "commentsAvailable": bool(comments),
                    "commentCoveragePct": round(len(comments) / len(matches) * 100),
                    "topPosts": top_posts,
                    "dataSource": "live",
                    "sampledAt": china_now().isoformat(timespec="seconds"),
                    **signal,
                }
            )
        dynamic.sort(
            key=lambda item: (int(item.get("posts") or 0), float(item.get("heatScore") or 0), item["name"]),
            reverse=True,
        )

        existing = {str(stock.get("code") or ""): copy.deepcopy(stock) for stock in data.get("stocks", [])}
        selected_codes = {stock["code"] for stock in dynamic[:10]}
        fillers: list[dict[str, Any]] = []
        for code, stock in existing.items():
            if not code or code in selected_codes:
                continue
            stock.update(
                {
                    "posts": 0, "comments": None, "commentsAvailable": False,
                    "score": 0, "sentiment": "样本不足", "confidence": 0,
                    "signalTier": "none",
                    "heatScore": 0, "heatChange": 0, "topPosts": [],
                    "dataSource": "empty", "sampledAt": None,
                }
            )
            fillers.append(stock)
            if len(dynamic[:10]) + len(fillers) >= 10:
                break
        data["stocks"] = (dynamic[:10] + fillers)[:10]

    @staticmethod
    def _entity_signal(matches: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = [post for post in matches if post.get("evidenceLevel") in {"A", "B"}]
        authors = {
            str(post.get("author") or "").strip()
            for post in evidence
            if str(post.get("author") or "").strip() not in {"", "小红书用户"}
        }
        weights: list[float] = []
        weighted_scores: list[float] = []
        for post in evidence:
            interaction = int(post.get("likes") or 0) + int(post.get("comments") or 0)
            viral_weight = min(8.0, 1.0 + math.log1p(max(0, interaction)))
            weight = viral_weight * float(post.get("evidenceWeight") or 0)
            weights.append(weight)
            weighted_scores.append(float(post.get("sentimentScore") or 0) * weight)
        raw_score = round(sum(weighted_scores) / sum(weights)) if weights and sum(weights) else 0
        qualified = len(evidence) >= 3 and len(authors) >= 2
        agreement = (
            sum(1 for post in evidence if (post.get("sentimentScore") or 0) * raw_score >= 0) / len(evidence)
            if evidence and raw_score else 0.5
        )
        confidence = min(
            100,
            round(min(1, len(evidence) / 6) * 45 + min(1, len(authors) / 4) * 25 + agreement * 30),
        )
        if not evidence:
            return {
                "score": 0, "sentiment": "样本不足", "confidence": 0,
                "evidencePosts": 0, "uniqueAuthors": 0, "signalTier": "none",
            }
        if not qualified:
            direction = "看多" if raw_score > 8 else "看空" if raw_score < -8 else "中性"
            prefix = "初步" if len(evidence) >= 2 and len(authors) >= 2 else "线索"
            return {
                "score": raw_score, "sentiment": f"{prefix}{direction}",
                "confidence": min(confidence, 49 if prefix == "初步" else 29),
                "evidencePosts": len(evidence), "uniqueAuthors": len(authors),
                "signalTier": "preliminary",
            }
        sentiment = "看多" if raw_score > 8 else "看空" if raw_score < -8 else "中性"
        return {
            "score": raw_score, "sentiment": sentiment, "confidence": confidence,
            "evidencePosts": len(evidence), "uniqueAuthors": len(authors),
            "signalTier": "qualified",
        }

    @classmethod
    def _merge_live_posts(cls, data: dict[str, Any], posts: list[dict[str, Any]]) -> dict[str, Any]:
        cls._annotate_evidence(posts)
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
            if post.get("evidenceLevel") not in {"A", "B"}:
                label, score = "样本不足", 0
            post["sentiment"] = label
            post["sentimentScore"] = score
            post["engagement"] = int(post.get("likes") or 0) + int(post.get("comments") or 0)

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
                evidence_matches = [post for post in matches if post.get("evidenceLevel") in {"A", "B"}]
                top_posts = sorted(evidence_matches or matches, key=lambda p: p["engagement"], reverse=True)[:3]
                comments = [int(p.get("comments") or 0) for p in matches if p.get("commentCountAvailable")]
                signal = cls._entity_signal(matches)
                entity["posts"] = len(matches)
                entity["comments"] = sum(comments) if comments else None
                entity["commentsAvailable"] = bool(comments)
                entity["commentCoveragePct"] = round(len(comments) / len(matches) * 100)
                entity["topPosts"] = top_posts
                entity["dataSource"] = "live"
                entity["sampledAt"] = china_now().isoformat(timespec="seconds")
                entity.update(signal)
                if entity_type == "sector":
                    tag_counts: dict[str, int] = {}
                    for post in evidence_matches:
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
            available_comments = [
                int(post.get("comments") or 0)
                for post in unique_posts.values()
                if post.get("commentCountAvailable")
            ]
            total_comments = sum(available_comments) if available_comments else None
            comment_coverage = round(len(available_comments) / total_posts * 100) if total_posts else 0
            sentiment_stocks = [
                item for item in data["stocks"]
                if item.get("dataSource") == "live" and item.get("signalTier") in {"qualified", "preliminary"}
            ]
        else:
            total_posts = sum(item["posts"] for item in data["sectors"])
            sector_comments = [int(item.get("comments") or 0) for item in data["sectors"] if item.get("commentsAvailable")]
            total_comments = sum(sector_comments) if sector_comments else None
            comment_coverage = 100 if sector_comments else 0
            sentiment_stocks = [item for item in data["stocks"] if item.get("signalTier") in {"qualified", "preliminary"}]
        positive = [item for item in sentiment_stocks if str(item.get("sentiment") or "").endswith("看多")]
        qualified_stocks = [item for item in sentiment_stocks if item.get("signalTier") == "qualified"]
        avg = (
            round(sum(item["score"] for item in sentiment_stocks) / len(sentiment_stocks))
            if sentiment_stocks
            else 0
        )
        return {
            "posts": total_posts,
            "comments": total_comments,
            "commentCoveragePct": comment_coverage,
            "bullRatio": round(len(positive) / len(sentiment_stocks) * 100) if sentiment_stocks else 0,
            "sentimentScore": avg,
            "sentimentStatus": "ready" if qualified_stocks else "preliminary" if sentiment_stocks else "insufficient",
            "qualifiedStocks": len(qualified_stocks),
            "preliminaryStocks": len(sentiment_stocks) - len(qualified_stocks),
        }
