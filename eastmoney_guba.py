from __future__ import annotations

import html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode, urljoin

from eastmoney_browser_session import VerificationRequired
from providers import TencentQuoteProvider, china_now, fetch_public_bytes, stock_symbol


GUBA_HOME = "https://guba.eastmoney.com/o/default"
GUBA_LIST = "https://guba.eastmoney.com/list,{code}.html"
SECTOR_RANK_URL = (
    "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz={limit}&po=1&np=1"
    "&fltt=2&invt=2&fid=f6&fs=m:90+t:2+f:!50&fields=f12,f14,f3,f6,f8"
)
SECTOR_STOCK_URL = (
    "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz={limit}&po=1&np=1"
    "&fltt=2&invt=2&fid=f6&fs=b:{board}&fields=f12,f14,f2,f3,f6,f8"
)
AUTHOR_REPLY_URL = "https://gbapi.eastmoney.com/reply/api/Reply/ArticleNewAuthorOnly"


def _plain_text(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _content_text(value: str) -> str:
    """Preserve paragraph boundaries because they are analysis evidence."""
    value = re.sub(
        r"<(?:br\s*/?|/(?:p|div|li|h[1-6]))\s*>", "\n", value or "", flags=re.I
    )
    value = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [re.sub(r"[\t \u00a0]+", " ", html.unescape(line)).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _count(value: Any) -> int:
    match = re.search(r"[\d.]+", str(value or "").replace(",", ""))
    if not match:
        return 0
    multiplier = 10_000 if "万" in str(value) else 1
    return int(float(match.group()) * multiplier)


class GubaTechnicalClassifier:
    """Explainable gate that excludes venting, slogans and promotional posts."""

    EVIDENCE = {
        "技术面": (
            r"支撑|压力|阻力|突破|回踩|缺口|前高|前低|趋势|形态|平台|箱体|主线|抗跌|冲高|"
            r"均线|日线|周线|分时|五日线|十日线|20日线|30日线|60日线|"
            r"macd|kdj|rsi|boll|金叉|死叉|背离|量价|放量|缩量|换手"
        ),
        "资金面": r"主力资金|净流入|净流出|大单|北向|游资|机构席位|龙虎榜|筹码|承接|抛压|资金结构|成交量",
        "基本面": r"基本面|财报|营收|净利润|毛利率|估值|市盈率|市净率|pe\b|pb\b|订单|业绩|产业链|公告|产能|资本开支",
        "交易计划": r"交易计划|操作策略|操作逻辑|仓位|止损|止盈|目标位|防守位|低吸|加仓|减仓|兑现|不追|观察位|收紧|保利润",
        "事件驱动": r"政策|并购|重组|中标|定增|回购|减持|解禁|催化|发布会|行业景气",
        "风险提示": r"风险|谨慎|警惕|跌破|失守|高位分歧|冲高回落|不确定|仅供参考|不构成投资建议",
    }
    ABUSE = r"傻逼|煞笔|垃圾|狗庄|狗托|畜生|骗子|废物|去死|操你|艹|妈的|瓜皮|贱|有毒|恶心|cnm|nmsl"
    PROMOTION = r"加群|微信群|微信号|公众号|私信我|带你吃肉|免费荐股|免费领取|扫码|老师带|评论[168]{2,}|点赞.{0,4}(集合|关注)"
    SLOGAN = r"^(涨停|跌停|起飞|快跑|冲啊?|加油|买买买|卖卖卖|完蛋|牛逼|666|回本|加仓|满仓|梭哈|抄底)[!！。,.，\s]*$"
    STRUCTURE = r"(?:^|[\n。；])(?:[一二三四五六七八九十]+[、.]|\d+[、.]|首先|其次|最后|结论|逻辑|计划)"
    NUMBER = r"(?:\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*(?:元|日线|点|倍)|ma\d+)"
    BULL = r"看多|偏多|突破|企稳|上行|反弹|修复|低吸|增持|净流入|金叉|放量上涨|趋势向上|目标位|继续持有|机会"
    BEAR = r"看空|偏空|跌破|失守|下行|减仓|止损|净流出|死叉|冲高回落|风险偏大|抛压|高位分歧|不追|兑现"
    ARTICLE_MIN_CHARS = 60

    @classmethod
    def potential(cls, title: str) -> bool:
        text = _plain_text(title).lower()
        if not text or re.search(cls.ABUSE, text, re.I) or re.search(cls.PROMOTION, text, re.I):
            return False
        return any(re.search(pattern, text, re.I) for pattern in cls.EVIDENCE.values())

    @classmethod
    def reply_is_analysis(cls, value: str) -> bool:
        text = _plain_text(value).lower()
        if len(re.sub(r"\s+", "", text)) < 15:
            return False
        if re.search(cls.ABUSE, text, re.I) or re.search(cls.PROMOTION, text, re.I):
            return False
        return any(re.search(pattern, text, re.I) for pattern in cls.EVIDENCE.values())

    @classmethod
    def extract_guidance(cls, content: str) -> dict[str, Any]:
        sentences = [
            re.sub(r"\s+", " ", item).strip(" ，,。；;：:")
            for item in re.split(r"[。！？!?；;\n]+", _plain_text(content))
            if len(re.sub(r"\s+", "", item)) >= 8
        ]

        def select(pattern: str, limit: int = 4) -> list[str]:
            values: list[str] = []
            for sentence in sentences:
                if re.search(pattern, sentence, re.I) and sentence not in values:
                    values.append(sentence[:100])
                if len(values) >= limit:
                    break
            return values

        action_sentences = select(
            r"买入|低吸|加仓|建仓|持有|拿住|观望|等待|不追|减仓|卖出|兑现|止盈|止损|收紧|空仓|仓位"
        )
        outlook_sentences = select(
            r"后面|后续|接下来|预计|大概率|可能|趋势|主线|上涨|下跌|反弹|回落|震荡|突破|跌破|企稳|抗跌"
        )
        condition_sentences = select(r"如果|若|一旦|只有|只要|等待|放量|缩量|突破|跌破|企稳|支撑|压力")
        risk_sentences = select(r"风险|谨慎|警惕|止损|跌破|失守|不追|高位|抛压|减仓|收紧")

        joined = "。".join(action_sentences)
        buy = len(re.findall(r"买入|低吸|加仓|建仓", joined))
        hold = len(re.findall(r"持有|拿住|观望|等待|不追", joined))
        sell = len(re.findall(r"减仓|卖出|兑现|止盈|止损|收紧", joined))
        if (buy or hold) and sell:
            trade_bias = "条件交易/分批处理"
        elif sell > max(buy, hold):
            trade_bias = "偏减仓/止盈"
        elif buy > max(sell, hold):
            trade_bias = "偏买入/低吸"
        elif hold:
            trade_bias = "持有/等待确认"
        else:
            trade_bias = "未给出明确买卖动作"

        outlook_text = "。".join(outlook_sentences)
        strong = len(re.findall(r"偏强|抗跌|上行|上涨|反弹|突破|企稳|主线", outlook_text))
        weak = len(re.findall(r"偏弱|下行|下跌|回落|跌破|失守|抛压", outlook_text))
        outlook = "偏强" if strong > weak else "偏弱" if weak > strong else "震荡/条件性判断" if outlook_sentences else "未明确"
        return {
            "tradeBias": trade_bias,
            "outlook": outlook,
            "actionSentences": action_sentences,
            "outlookSentences": outlook_sentences,
            "conditions": condition_sentences,
            "risks": risk_sentences,
        }

    @classmethod
    def classify(cls, title: str, content: str) -> dict[str, Any]:
        text = _plain_text(f"{title}\n{content}").lower()
        compact = re.sub(r"\s+", "", text)
        content_compact = re.sub(r"\s+", "", _plain_text(content))
        sentence_count = len([
            segment for segment in re.split(r"[。！？!?；;\n]+", _plain_text(content))
            if len(re.sub(r"\s+", "", segment)) >= 8
        ])
        categories = [name for name, pattern in cls.EVIDENCE.items() if re.search(pattern, text, re.I)]
        abuse = bool(re.search(cls.ABUSE, text, re.I))
        promotion = bool(re.search(cls.PROMOTION, text, re.I))
        slogan = bool(re.fullmatch(cls.SLOGAN, _plain_text(title).lower(), re.I))
        structured = bool(re.search(cls.STRUCTURE, text, re.I))
        numeric = bool(re.search(cls.NUMBER, text, re.I))
        content_length = len(content_compact)

        score = len(categories) * 17
        score += min(18, max(0, (content_length - 24) // 35 * 4))
        score += 10 if structured else 0
        score += 8 if numeric else 0
        score -= 55 if abuse else 0
        score -= 55 if promotion else 0
        score -= 35 if slogan else 0
        score -= 45 if content_length < cls.ARTICLE_MIN_CHARS else 0
        score -= 20 if sentence_count < 3 else 0
        score = max(0, min(100, score))

        strong = any(name in categories for name in ("技术面", "资金面", "基本面"))
        eligible = bool(
            not abuse
            and not promotion
            and not slogan
            and score >= 55
            and content_length >= cls.ARTICLE_MIN_CHARS
            and sentence_count >= 3
            and (structured or sentence_count >= 3)
            and (len(categories) >= 2 or (strong and structured and numeric))
        )
        if eligible:
            reasons = [f"包含{name}证据" for name in categories[:4]]
            if structured:
                reasons.append("正文有分点论证")
            if numeric:
                reasons.append("包含价位/比例/周期等量化信息")
            rejection = None
        else:
            reasons = []
            rejection = (
                "含辱骂或攻击性表达" if abuse else
                "含引流、拉群或荐股推广" if promotion else
                "只有口号，没有分析依据" if slogan else
                f"非文章型内容（正文少于 {cls.ARTICLE_MIN_CHARS} 字）" if content_length < cls.ARTICLE_MIN_CHARS else
                "正文段落/完整句不足 3 个" if sentence_count < 3 else
                "缺少文章结构或连续论证" if not structured and sentence_count < 3 else
                "分析证据类型不足" if len(categories) < 2 else
                "质量评分未达到 55 分"
            )

        bull_hits = len(re.findall(cls.BULL, text, re.I))
        bear_hits = len(re.findall(cls.BEAR, text, re.I))
        sentiment = "中性"
        if bull_hits >= bear_hits + 2:
            sentiment = "看多"
        elif bear_hits >= bull_hits + 2:
            sentiment = "看空"
        return {
            "analysisEligible": eligible,
            "analysisScore": score,
            "analysisTypes": categories,
            "analysisReasons": reasons,
            "rejectionReason": rejection,
            "qualityTier": "A" if eligible and score >= 75 else "B" if eligible else "filtered",
            "sentiment": sentiment,
            "sentimentEvidence": {"bull": bull_hits, "bear": bear_hits},
            "articleStats": {"characters": content_length, "sentences": sentence_count, "structured": structured},
        }


class EastmoneyGubaProvider:
    """Low-frequency, read-only parser for Eastmoney's public rendered pages."""

    LIST_ROW = re.compile(
        r'<tr\s+class="listitem"[^>]*>.*?class="read"[^>]*>(?P<read>.*?)</div>.*?'
        r'class="reply"[^>]*>(?P<reply>.*?)</div>.*?class="title"[^>]*>\s*'
        r'<a[^>]*data-postid="(?P<postid>\d+)"[^>]*data-posttype="(?P<type>\d+)"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'class="author"[^>]*>\s*<a[^>]*>(?P<author>.*?)</a>.*?'
        r'class="update"[^>]*>(?P<update>.*?)</div>.*?</tr>',
        re.I | re.S,
    )

    def __init__(
        self,
        fetcher: Callable[[str, int], bytes] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        browser_fetcher: Callable[[str], str] | None = None,
    ) -> None:
        self.fetcher = fetcher or (lambda url, timeout: fetch_public_bytes(url, timeout=timeout))
        self.sleeper = sleeper
        self.browser_fetcher = browser_fetcher
        self.delay_seconds = max(0.0, float(os.environ.get("SENTIBOARD_GUBA_REQUEST_DELAY", "0.8")))
        self.detail_candidate_limit = max(
            5, int(os.environ.get("SENTIBOARD_GUBA_DETAIL_CANDIDATES", "24"))
        )
        self.max_pages_per_stock = max(
            1, int(os.environ.get("SENTIBOARD_GUBA_MAX_PAGES_PER_STOCK", "50"))
        )
        self.quote_provider = TencentQuoteProvider()

    def collect(self, stock_limit: int = 10, detail_per_stock: int = 3) -> dict[str, Any]:
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") == "1":
            return self.empty("测试模式未访问东方财富股吧")
        now = china_now()
        request_count = 0
        try:
            home = self._fetch_text(GUBA_HOME)
            request_count += 1
            hot_stocks = self.parse_hot_stocks(home)[:stock_limit]
        except Exception as exc:
            return self.empty(f"热门股票入口读取失败：{type(exc).__name__}")

        all_rows: list[dict[str, Any]] = []
        stock_errors = 0
        for index, stock in enumerate(hot_stocks):
            if index:
                self.sleeper(self.delay_seconds)
            try:
                page = self._fetch_text(GUBA_LIST.format(code=stock["code"]))
                request_count += 1
                rows = self.parse_list(page, stock, now)
                candidates = [row for row in rows if GubaTechnicalClassifier.potential(row["title"])]
                candidates.sort(
                    key=lambda row: (row["postType"] == 20, row["readCount"] + row["commentCount"] * 6),
                    reverse=True,
                )
                all_rows.extend(candidates[:detail_per_stock])
                stock["scannedPosts"] = len(rows)
                stock["candidatePosts"] = len(candidates)
            except Exception:
                stock_errors += 1
                stock["scannedPosts"] = 0
                stock["candidatePosts"] = 0

        seen_urls: set[str] = set()
        detailed: list[dict[str, Any]] = []
        rejected_date = 0
        detail_errors = 0
        for row in all_rows:
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
            if "caifuhao.eastmoney.com/news/" not in row["url"]:
                # Ordinary guba detail pages currently require an interactive
                # identity slider. Do not bypass or repeatedly trigger it.
                detail_errors += 1
                continue
            self.sleeper(self.delay_seconds)
            try:
                page = self._fetch_text(row["url"])
                request_count += 1
                detail = self.parse_detail(page, row["url"])
                published = self._published_datetime(detail.get("publishedAt"), now)
                cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
                if published is None or published < cutoff or published > now:
                    rejected_date += 1
                    continue
                stock_mentioned = (
                    row["stockCode"] == str(detail.get("gubaCode") or "")
                    or row["stockCode"] in f"{detail.get('title', '')} {detail.get('content', '')}"
                    or row["stockName"] in f"{detail.get('title', '')} {detail.get('content', '')}"
                )
                if not stock_mentioned:
                    continue
                post = {**row, **detail}
                if detail.get("readCount") is None:
                    post["readCount"] = row["readCount"]
                if detail.get("commentCount") is None:
                    post["commentCount"] = row["commentCount"]
                post.update(GubaTechnicalClassifier.classify(post["title"], post["content"]))
                post["content"] = _plain_text(post["content"])[:360]
                detailed.append(post)
            except Exception:
                detail_errors += 1

        quotes = self.quote_provider.fetch([stock["code"] for stock in hot_stocks])
        by_stock: dict[str, list[dict[str, Any]]] = {}
        for stock in hot_stocks:
            by_stock[stock["code"]] = [p for p in detailed if p["stockCode"] == stock["code"]]
        stock_cards = []
        filtered_counter: Counter[str] = Counter()
        for rank, stock in enumerate(hot_stocks, 1):
            posts = by_stock[stock["code"]]
            eligible = [post for post in posts if post["analysisEligible"]]
            for post in posts:
                if not post["analysisEligible"]:
                    filtered_counter[post.get("rejectionReason") or "其他"] += 1
            sentiment_counts = Counter(post["sentiment"] for post in eligible)
            directional = sentiment_counts["看多"] - sentiment_counts["看空"]
            sentiment = "看多" if directional > 0 else "看空" if directional < 0 else "中性"
            quote = quotes.get(stock_symbol(stock["code"]), {})
            top_posts = sorted(
                eligible,
                key=lambda post: (post["analysisScore"], post["readCount"] + post["commentCount"] * 6),
                reverse=True,
            )[:3]
            stock_cards.append({
                **stock,
                "rank": rank,
                "price": quote.get("price"),
                "changePct": quote.get("changePct"),
                "scannedPosts": int(stock.get("scannedPosts") or 0),
                "candidatePosts": int(stock.get("candidatePosts") or 0),
                "technicalPosts": len(eligible),
                "filteredPosts": max(0, int(stock.get("scannedPosts") or 0) - len(eligible)),
                "uniqueAuthors": len({post["author"] for post in eligible}),
                "sentiment": sentiment,
                "sentimentCounts": dict(sentiment_counts),
                "analysisTypes": Counter(t for p in eligible for t in p["analysisTypes"]).most_common(3),
                "topPosts": top_posts,
            })

        eligible_total = sum(stock["technicalPosts"] for stock in stock_cards)
        scanned_total = sum(stock["scannedPosts"] for stock in stock_cards)
        bull = sum(stock["sentimentCounts"].get("看多", 0) for stock in stock_cards)
        bear = sum(stock["sentimentCounts"].get("看空", 0) for stock in stock_cards)
        return {
            "meta": {
                "source": "东方财富股吧公开页面",
                "status": "ok" if hot_stocks else "empty",
                "message": f"低频串行采集完成；{stock_errors} 个股吧、{detail_errors} 篇详情读取失败",
                "updatedAt": now.isoformat(timespec="seconds"),
                "updatedLabel": now.strftime("%m月%d日 %H:%M"),
                "dataDate": now.date().isoformat(),
                "requestCount": request_count,
                "rejectedNonToday": rejected_date,
                "methodology": "仅分析当天发布的正文；分析证据门控后才进入多空统计",
            },
            "summary": {
                "hotStocks": len(stock_cards),
                "scannedPosts": scanned_total,
                "technicalPosts": eligible_total,
                "filteredPosts": max(0, scanned_total - eligible_total),
                "uniqueAuthors": len({p["author"] for p in detailed if p["analysisEligible"]}),
                "acceptancePct": round(eligible_total / scanned_total * 100, 1) if scanned_total else 0,
                "bullPosts": bull,
                "bearPosts": bear,
            },
            "quality": {
                "threshold": 55,
                "rules": ["至少两类分析证据", "当天原始发布时间", "排除辱骂、口号和引流", "仅补充帖主的分析性回复"],
                "filteredReasons": dict(filtered_counter),
            },
            "stocks": stock_cards,
        }

    def collect_hierarchy(self, sector_limit: int = 10, stocks_per_sector: int = 10) -> dict[str, Any]:
        """Scan 10x10 stock-bar list pages without downloading every post body."""
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") == "1":
            return self.empty_hierarchy("测试模式未访问东方财富股吧")
        now = china_now()
        request_count = 0
        try:
            sector_payload = self._fetch_json(SECTOR_RANK_URL.format(limit=sector_limit))
            request_count += 1
            sector_rows = (sector_payload.get("data") or {}).get("diff") or []
        except Exception as exc:
            return self.empty_hierarchy(f"板块排行读取失败：{type(exc).__name__}")

        sectors: list[dict[str, Any]] = []
        list_errors = 0
        scanned_windows: dict[str, dict[str, Any]] = {}
        for sector_rank, raw_sector in enumerate(sector_rows[:sector_limit], 1):
            board = str(raw_sector.get("f12") or "")
            sector = {
                "rank": sector_rank,
                "code": board,
                "name": str(raw_sector.get("f14") or board),
                "changePct": raw_sector.get("f3"),
                "amount": raw_sector.get("f6"),
                "turnoverRate": raw_sector.get("f8"),
                "stocks": [],
            }
            self.sleeper(self.delay_seconds)
            try:
                stock_payload = self._fetch_json(
                    SECTOR_STOCK_URL.format(limit=stocks_per_sector, board=board)
                )
                request_count += 1
                stock_rows = (stock_payload.get("data") or {}).get("diff") or []
            except Exception:
                stock_rows = []
                list_errors += 1

            for stock_rank, raw_stock in enumerate(stock_rows[:stocks_per_sector], 1):
                stock = {
                    "rank": stock_rank,
                    "code": str(raw_stock.get("f12") or ""),
                    "name": str(raw_stock.get("f14") or ""),
                    "sector": sector["name"],
                    "sectorCode": board,
                    "price": raw_stock.get("f2"),
                    "changePct": raw_stock.get("f3"),
                    "amount": raw_stock.get("f6"),
                    "turnoverRate": raw_stock.get("f8"),
                    "scannedPosts": 0,
                    "candidatePosts": 0,
                    "todayReads": 0,
                    "todayComments": 0,
                }
                try:
                    window = scanned_windows.get(stock["code"])
                    if window is None:
                        window = self.scan_stock_window(stock, now)
                        scanned_windows[stock["code"]] = window
                        request_count += window["pagesScanned"]
                    rows = window["rows"]
                    candidates = [row for row in rows if GubaTechnicalClassifier.potential(row["title"])]
                    stock.update({
                        "scannedPosts": len(rows),
                        "candidatePosts": len(candidates),
                        "todayReads": sum(row["readCount"] for row in rows),
                        "todayComments": sum(row["commentCount"] for row in rows),
                        "pagesScanned": window["pagesScanned"],
                        "windowComplete": window["windowComplete"],
                        "windowStart": f"{now.date().isoformat()}T06:00:00+08:00",
                        "windowEnd": now.isoformat(timespec="seconds"),
                        "earliestPostTime": window["earliestPostTime"],
                        "latestPostTime": window["latestPostTime"],
                        "_candidateRows": candidates,
                        "_windowRows": rows,
                    })
                except Exception:
                    list_errors += 1
                sector["stocks"].append(stock)

            sector["scannedPosts"] = sum(stock["scannedPosts"] for stock in sector["stocks"])
            sector["candidatePosts"] = sum(stock["candidatePosts"] for stock in sector["stocks"])
            sector["todayReads"] = sum(stock["todayReads"] for stock in sector["stocks"])
            sector["todayComments"] = sum(stock["todayComments"] for stock in sector["stocks"])
            sector["pagesScanned"] = sum(int(stock.get("pagesScanned") or 0) for stock in sector["stocks"])
            sector["completeStocks"] = sum(bool(stock.get("windowComplete")) for stock in sector["stocks"])
            sectors.append(sector)

        stocks = [stock for sector in sectors for stock in sector["stocks"]]
        return {
            "meta": {
                "source": "东方财富公开板块行情 + 股吧公开页面",
                "status": "ok" if sectors else "empty",
                "message": f"已扫描 {len(stocks)} 只活跃股；{list_errors} 个公开页面读取失败",
                "updatedAt": now.isoformat(timespec="seconds"),
                "updatedLabel": now.strftime("%m月%d日 %H:%M"),
                "dataDate": now.date().isoformat(),
                "requestCount": request_count,
                "methodology": "板块和成分股按当日成交额排序；股吧从刷新时点逐页扫描至当天 06:00，详情按点击补全",
                "scope": "10 个热门板块 × 每板块 10 只活跃股",
            },
            "summary": {
                "hotSectors": len(sectors),
                "stocks": len(stocks),
                "scannedPosts": sum(stock["scannedPosts"] for stock in stocks),
                "candidatePosts": sum(stock["candidatePosts"] for stock in stocks),
                "todayReads": sum(stock["todayReads"] for stock in stocks),
                "todayComments": sum(stock["todayComments"] for stock in stocks),
                "pagesScanned": sum(int(stock.get("pagesScanned") or 0) for stock in stocks),
                "completeStocks": sum(bool(stock.get("windowComplete")) for stock in stocks),
            },
            "quality": {
                "threshold": 55,
                "rules": [
                    "板块/个股热度按当日成交额，不把涨跌幅当热度",
                    "逐页扫描至早于 06:00；点入股票后用正文二次筛选",
                    "排除辱骂、喊单、短口号与引流内容",
                    "普通评论不纳入分析，仅补充帖主的分析性回复",
                ],
            },
            "sectors": sectors,
        }

    def collect_stock_detail(self, stock: dict[str, Any], max_posts: int = 10) -> dict[str, Any]:
        """Download only one selected stock's likely analysis posts and images."""
        now = china_now()
        cached_candidates = stock.get("_windowRows")
        request_count = 0
        if isinstance(cached_candidates, list):
            candidates = [dict(row) for row in cached_candidates if isinstance(row, dict)]
            scanned_count = int(stock.get("scannedPosts") or 0)
        else:
            window = self.scan_stock_window(stock, now)
            request_count += window["pagesScanned"]
            rows = window["rows"]
            scanned_count = len(rows)
            candidates = rows
        candidates = self._select_detail_candidates(candidates)
        posts: list[dict[str, Any]] = []
        rejected: Counter[str] = Counter()
        verification_lost = False
        for row_index, row in enumerate(candidates):
            is_wealth_article = "caifuhao.eastmoney.com/news/" in row["url"]
            if not is_wealth_article and self.browser_fetcher is None:
                rejected["普通股吧正文待人工核验"] += 1
                continue
            self.sleeper(self.delay_seconds)
            try:
                detail_page = (
                    self._fetch_text(row["url"])
                    if is_wealth_article else self.browser_fetcher(row["url"])
                )
                request_count += 1
                detail = self.parse_detail(detail_page, row["url"])
                published = self._published_datetime(detail.get("publishedAt"), now)
                cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
                if published is None or published < cutoff or published > now:
                    rejected["不是当天原始发布"] += 1
                    continue
                combined = f"{detail.get('title', '')} {detail.get('content', '')}"
                if str(stock["code"]) not in combined and str(stock["name"]) not in combined:
                    rejected["未明确分析该股票"] += 1
                    continue
                post = {**row, **detail}
                if detail.get("readCount") is None:
                    post["readCount"] = row["readCount"]
                if detail.get("commentCount") is None:
                    post["commentCount"] = row["commentCount"]
                post.update(GubaTechnicalClassifier.classify(post["title"], post["content"]))
                if not post["analysisEligible"]:
                    rejected[post["rejectionReason"] or "质量不足"] += 1
                    continue
                stock_context = self._stock_context(post["content"], stock)
                context_signal = GubaTechnicalClassifier.classify(stock["name"], stock_context)
                post["stockContext"] = stock_context
                post["sentiment"] = context_signal["sentiment"]
                post["sentimentEvidence"] = context_signal["sentimentEvidence"]
                post["interactionScore"] = (
                    int(post["readCount"]) + int(post["likeCount"]) * 8
                    + int(post["commentCount"]) * 12
                )
                posts.append(post)
            except VerificationRequired:
                rejected["人工核验已失效"] += len(candidates) - row_index
                verification_lost = True
                break
            except Exception:
                rejected["正文读取失败"] += 1

        posts.sort(
            key=lambda post: (post["interactionScore"], post["analysisScore"]), reverse=True
        )
        posts = posts[:max_posts]
        for index, post in enumerate(posts, 1):
            post["rank"] = index
            replies, reply_requests = self.collect_author_replies(post)
            request_count += reply_requests
            post["authorReplies"] = replies
            if replies:
                supplemented = GubaTechnicalClassifier.classify(
                    stock["name"],
                    f"{post['stockContext']}\n" + "\n".join(reply["content"] for reply in replies),
                )
                post["sentiment"] = supplemented["sentiment"]
                post["sentimentEvidence"] = supplemented["sentimentEvidence"]
            combined_guidance = f"{post['stockContext']}\n" + "\n".join(
                reply["content"] for reply in replies
            )
            post["guidance"] = GubaTechnicalClassifier.extract_guidance(combined_guidance)
        return {
            "meta": {
                "source": "东方财富股吧公开帖子",
                "updatedAt": now.isoformat(timespec="seconds"),
                "updatedLabel": now.strftime("%m月%d日 %H:%M"),
                "dataDate": now.date().isoformat(),
                "requestCount": request_count,
                "verificationRequired": verification_lost,
                "message": f"从 06:00 后 {scanned_count} 篇帖子中筛出 {len(posts)} 篇高互动技术分析",
            },
            "stock": {key: stock.get(key) for key in ("code", "name", "sector", "price", "changePct", "amount", "turnoverRate")},
            "posts": posts,
            "communityView": self.summarize_posts(posts),
            "quality": {"scanned": scanned_count, "candidates": len(candidates), "accepted": len(posts), "rejectedReasons": dict(rejected), "windowComplete": bool(stock.get("windowComplete")), "pagesScanned": int(stock.get("pagesScanned") or 0)},
        }

    def _select_detail_candidates(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Use cheap list metadata to avoid opening every one-line post."""
        ranked = sorted(
            (dict(row) for row in rows),
            key=lambda row: (
                "caifuhao.eastmoney.com/news/" in str(row.get("url") or ""),
                GubaTechnicalClassifier.potential(str(row.get("title") or "")),
                len(_plain_text(str(row.get("title") or ""))) >= 18,
                int(row.get("readCount") or 0) + int(row.get("commentCount") or 0) * 12,
            ),
            reverse=True,
        )
        wealth = [
            row for row in ranked
            if "caifuhao.eastmoney.com/news/" in str(row.get("url") or "")
        ]
        ordinary = [
            row for row in ranked
            if row not in wealth and (
                GubaTechnicalClassifier.potential(str(row.get("title") or ""))
                or len(_plain_text(str(row.get("title") or ""))) >= 18
            )
        ]
        fallback = [row for row in ranked if row not in wealth and row not in ordinary]
        selected = wealth + ordinary + fallback
        return selected[: self.detail_candidate_limit]

    @staticmethod
    def _stock_context(content: str, stock: dict[str, Any]) -> str:
        """Keep only the selected stock's paragraph(s) from multi-stock articles."""
        name = str(stock.get("name") or "").strip()
        code = str(stock.get("code") or "").strip()
        lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
        if len(lines) <= 1:
            return str(content or "")
        selected: list[str] = []
        for line in lines:
            if not ((name and name in line) or (code and code in line)):
                continue
            selected.append(line)
        return "\n".join(dict.fromkeys(selected)) or str(content or "")

    def collect_author_replies(self, post: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        author_id = str(post.get("authorId") or "")
        post_id = str(post.get("postId") or "")
        if not author_id or not post_id or int(post.get("commentCount") or 0) <= 0:
            return [], 0
        replies: list[dict[str, Any]] = []
        requests = 0
        seen: set[str] = set()
        for page in range(1, 11):
            if page > 1:
                self.sleeper(self.delay_seconds)
            query = urlencode({
                "postid": post_id, "sort": 1, "sorttype": 1, "p": page,
                "ps": 30, "needHide": "true", "manageruid": author_id,
            })
            try:
                payload = self._fetch_json(f"{AUTHOR_REPLY_URL}?{query}")
                requests += 1
            except Exception:
                break
            rows = payload.get("re") or []
            if isinstance(rows, dict):
                rows = rows.get("list") or rows.get("reply_list") or []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                user = row.get("reply_user") or {}
                if str(user.get("user_id") or row.get("reply_user_id") or "") != author_id:
                    continue
                content = _plain_text(row.get("reply_text") or "")
                if not GubaTechnicalClassifier.reply_is_analysis(content):
                    continue
                reply_id = str(row.get("reply_id") or "")
                if reply_id and reply_id in seen:
                    continue
                seen.add(reply_id)
                images = self._reply_images(row.get("reply_picture"))
                replies.append({
                    "replyId": reply_id,
                    "content": content,
                    "publishedAt": str(row.get("reply_publish_time") or ""),
                    "likeCount": _count(row.get("reply_like_count")),
                    "images": images,
                })
            total = int(payload.get("manager_comment_count") or payload.get("count") or 0)
            if page * 30 >= total or len(rows) < 30:
                break
        return replies, requests

    @staticmethod
    def _reply_images(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        images: list[str] = []
        for item in values:
            raw = item if isinstance(item, str) else (
                item.get("url") or item.get("imgurl") or item.get("origin_url") or ""
            ) if isinstance(item, dict) else ""
            url = urljoin("https://guba.eastmoney.com", html.unescape(str(raw)))
            if url.startswith("https://") and ("eastmoney" in url or "dfcfw.com" in url):
                if url not in images:
                    images.append(url)
        return images[:9]

    def scan_stock_window(self, stock: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Page newest-post order until every non-pinned row is before 06:00."""
        cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
        accepted: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages_scanned = 0
        complete = False
        for page_number in range(1, self.max_pages_per_stock + 1):
            if page_number > 1:
                self.sleeper(self.delay_seconds)
            url = (
                GUBA_LIST.format(code=stock["code"])
                if page_number == 1
                else f"https://guba.eastmoney.com/list,{stock['code']}_{page_number}.html"
            )
            page = self._fetch_text(url)
            pages_scanned += 1
            rows = self.parse_list(page, stock, now, today_only=False)
            if not rows:
                complete = True
                break
            parsed_times = [self._list_datetime(row["listPublishedAt"], now) for row in rows]
            regular_times = [value for value in parsed_times if value is not None]
            for row, published_at in zip(rows, parsed_times):
                if published_at is None or published_at < cutoff or published_at > now:
                    continue
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                accepted.append(row)
            # The plain URL is 东方财富's “最新发帖” order. Once an entire
            # page is earlier than the cutoff, later pages cannot re-enter it.
            if regular_times and max(regular_times) < cutoff:
                complete = True
                break
        accepted.sort(key=lambda row: row["listPublishedAt"], reverse=True)
        return {
            "rows": accepted,
            "pagesScanned": pages_scanned,
            "windowComplete": complete,
            "earliestPostTime": accepted[-1]["listPublishedAt"] if accepted else None,
            "latestPostTime": accepted[0]["listPublishedAt"] if accepted else None,
        }

    @staticmethod
    def _list_datetime(value: str, now: datetime) -> datetime | None:
        match = re.fullmatch(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", str(value or ""))
        if not match:
            return None
        try:
            return now.replace(
                month=int(match.group(1)), day=int(match.group(2)),
                hour=int(match.group(3)), minute=int(match.group(4)),
                second=0, microsecond=0,
            )
        except ValueError:
            return None

    @staticmethod
    def summarize_posts(posts: list[dict[str, Any]]) -> dict[str, Any]:
        if not posts:
            return {
                "stance": "证据不足", "confidence": 0,
                "summary": "06:00 至刷新时点没有足够的高质量技术分析帖，系统不生成方向结论。",
                "commonEvidence": [], "observationPlan": "保持观察，等待更多独立作者给出可验证的分析依据。",
                "tradeTendency": "证据不足", "outlook": "证据不足", "authorPlans": [],
                "riskTriggers": ["样本不足，不能代表市场共识"],
            }
        weights = {
            "看多": sum(max(1, p["interactionScore"]) for p in posts if p["sentiment"] == "看多"),
            "看空": sum(max(1, p["interactionScore"]) for p in posts if p["sentiment"] == "看空"),
            "中性": sum(max(1, p["interactionScore"]) for p in posts if p["sentiment"] == "中性"),
        }
        total = sum(weights.values()) or 1
        directional = weights["看多"] - weights["看空"]
        stance = "偏多" if directional / total > 0.16 else "偏空" if directional / total < -0.16 else "分歧/中性"
        confidence = round(min(90, abs(directional) / total * 100 + min(30, len(posts) * 3)))
        type_counts = Counter(t for post in posts for t in post["analysisTypes"])
        evidence = [name for name, _ in type_counts.most_common(4)]
        trade_weights: Counter[str] = Counter()
        outlook_weights: Counter[str] = Counter()
        for post in posts:
            weight = max(1, int(post.get("interactionScore") or 0))
            guidance = post.get("guidance") or {}
            trade_weights[str(guidance.get("tradeBias") or "未给出明确买卖动作")] += weight
            outlook_weights[str(guidance.get("outlook") or "未明确")] += weight
        trade_tendency = trade_weights.most_common(1)[0][0]
        outlook = outlook_weights.most_common(1)[0][0]
        author_plans: list[str] = []
        for post in posts:
            for sentence in (post.get("guidance") or {}).get("actionSentences") or []:
                if sentence not in author_plans:
                    author_plans.append(sentence)
                if len(author_plans) >= 5:
                    break
            if len(author_plans) >= 5:
                break
        if stance == "偏多":
            plan = "社区高互动分析整体偏多；更适合等待回踩支撑或量价确认，避免把一致看多直接等同于追高信号。"
        elif stance == "偏空":
            plan = "社区高互动分析整体偏空；优先观察跌破、抛压和资金流出条件是否持续，避免仅凭恐慌表达作出决策。"
        else:
            plan = "多空证据分歧明显；等待方向突破、成交量或资金结构给出一致确认，再形成自己的交易计划。"
        risks = []
        if any("风险提示" in post["analysisTypes"] for post in posts):
            risks.append("多篇帖子主动提及风险/止损条件")
        if len({post["author"] for post in posts}) < 3:
            risks.append("独立作者不足 3 位，存在单一观点放大")
        if weights["中性"] / total > 0.4:
            risks.append("中性权重较高，方向共识较弱")
        risks.append("社区观点可能滞后或带有持仓偏见，必须结合真实行情核验")
        return {
            "stance": stance,
            "confidence": confidence,
            "summary": f"综合 {len(posts)} 篇技术分析帖，互动加权后社区观点为{stance}。主要依据集中在{'、'.join(evidence) or '未形成稳定类别'}。",
            "commonEvidence": evidence,
            "tradeTendency": trade_tendency,
            "outlook": outlook,
            "authorPlans": author_plans,
            "observationPlan": plan,
            "riskTriggers": risks,
            "weights": weights,
            "disclaimer": "这是公开社区观点的结构化汇总，不构成投资建议。",
        }

    def _fetch_text(self, url: str) -> str:
        return self.fetcher(url, 15).decode("utf-8", errors="replace")

    def _fetch_json(self, url: str) -> dict[str, Any]:
        payload = json.loads(self._fetch_text(url))
        if not isinstance(payload, dict) or payload.get("rc") not in {0, None}:
            raise ValueError("unexpected Eastmoney response")
        return payload

    @staticmethod
    def parse_hot_stocks(page: str) -> list[dict[str, str]]:
        section = re.search(r"热门个股吧.*?<ul\s+class=\"list\">(?P<body>.*?)</ul>", page, re.I | re.S)
        body = section.group("body") if section else ""
        stocks: list[dict[str, str]] = []
        seen: set[str] = set()
        for code, name in re.findall(r'href=["\'](?:https?://guba\.eastmoney\.com/)?list,(\d{6})\.html["\'][^>]*>(.*?)</a>', body, re.I | re.S):
            if code in seen:
                continue
            seen.add(code)
            stocks.append({"code": code, "name": _plain_text(name).removesuffix("吧")})
        return stocks

    @classmethod
    def parse_list(
        cls, page: str, stock: dict[str, str], now: datetime, today_only: bool = True
    ) -> list[dict[str, Any]]:
        today_label = now.strftime("%m-%d")
        rows: list[dict[str, Any]] = []
        for match in cls.LIST_ROW.finditer(page):
            update = _plain_text(match.group("update"))
            if today_only and not update.startswith(today_label):
                continue
            post_type = int(match.group("type"))
            if post_type in {1, 2, 3, 4}:  # 资讯、公告、研报等机构内容不作为博主观点
                continue
            rows.append({
                "stockCode": stock["code"],
                "stockName": stock["name"],
                "postId": match.group("postid"),
                "postType": post_type,
                "title": _plain_text(match.group("title")),
                "author": _plain_text(match.group("author")),
                "readCount": _count(_plain_text(match.group("read"))),
                "commentCount": _count(_plain_text(match.group("reply"))),
                "listPublishedAt": update,
                "url": urljoin("https://guba.eastmoney.com", html.unescape(match.group("href"))),
            })
        return rows

    @staticmethod
    def _published_datetime(value: Any, now: datetime) -> datetime | None:
        text = str(value or "").strip()
        match = re.search(
            r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?[ T](\d{1,2}):(\d{2})(?::(\d{2}))?",
            text,
        )
        if not match:
            return None
        try:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4)), int(match.group(5)), int(match.group(6) or 0),
                tzinfo=now.tzinfo,
            )
        except ValueError:
            return None

    @staticmethod
    def parse_detail(page: str, source_url: str = "") -> dict[str, Any]:
        match = re.search(r"var\s+post_article\s*=\s*(\{.*?\})\s*</script>", page, re.I | re.S)
        image_values: list[str] = []
        if match:
            payload = json.loads(match.group(1))
            user = payload.get("post_user") or {}
            guba = payload.get("post_guba") or {}
            content_html = str(payload.get("post_content") or payload.get("post_abstract") or "")
            image_values.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)', content_html, re.I))
            for key in ("post_pic_url", "post_pic_url2"):
                for item in payload.get(key) or []:
                    if isinstance(item, str):
                        image_values.append(item)
                    elif isinstance(item, dict):
                        image_values.append(str(item.get("url") or item.get("imgurl") or ""))
            for item in payload.get("stockimgs") or []:
                if isinstance(item, dict):
                    image_values.append(str(item.get("imgurl") or ""))
            result = {
                "title": _plain_text(payload.get("post_title") or ""),
                "content": _content_text(content_html),
                "author": str(user.get("user_nickname") or "东方财富用户"),
                "authorId": str(user.get("user_id") or ""),
                "publishedAt": str(payload.get("post_publish_time") or ""),
                "readCount": _count(payload.get("post_click_count")),
                "commentCount": _count(payload.get("post_comment_count")),
                "likeCount": _count(payload.get("post_like_count")),
                "gubaCode": str(guba.get("stockbar_code") or ""),
                "sourceType": "股吧用户帖",
            }
        else:
            title_match = re.search(
                r'<h1[^>]+class=["\'][^"\']*article-title[^"\']*["\'][^>]*>(.*?)</h1>',
                page, re.I | re.S,
            )
            body_match = re.search(
                r'<div[^>]+class=["\'][^"\']*article-body[^"\']*["\'][^>]*>(.*?)'
                r'<div[^>]+id=["\']zwaddcontent["\']',
                page, re.I | re.S,
            )
            published_match = re.search(
                r"(20\d{2}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2})", page
            )
            if not title_match or not body_match or not published_match:
                raise ValueError("supported article body not found")
            content_html = body_match.group(1)
            author_match = re.search(
                r'<a[^>]+href=["\']https://i\.eastmoney\.com/(\d+)[^"\']*["\'][^>]*'
                r'class=["\'][^"\']*(?:auth|name)[^"\']*["\'][^>]*>(.*?)</a>',
                page, re.I | re.S,
            )
            image_values.extend(
                re.findall(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)', content_html, re.I)
            )
            like_match = re.search(
                r'class=["\'][^"\']*zancout[^"\']*["\'][^>]*>(.*?)</span>', page, re.I | re.S
            )
            comment_match = re.search(
                r'class=["\'][^"\']*plcount[^"\']*["\'][^>]*>(.*?)</span>', page, re.I | re.S
            )
            stock_match = re.search(r'data-stockcode=["\'](\d{6})["\']', content_html, re.I)
            result = {
                "title": _plain_text(title_match.group(1)),
                "content": _content_text(content_html),
                "author": _plain_text(author_match.group(2)) if author_match else "东方财富专栏作者",
                "authorId": author_match.group(1) if author_match else "",
                "publishedAt": re.sub(
                    r"年|月", "-", published_match.group(1)
                ).replace("日", ""),
                "readCount": None,
                "commentCount": _count(comment_match.group(1)) if comment_match else None,
                "likeCount": _count(like_match.group(1)) if like_match else 0,
                "gubaCode": stock_match.group(1) if stock_match else "",
                "sourceType": "财富号文章",
            }
        images: list[str] = []
        for value in image_values:
            if not value:
                continue
            url = urljoin(source_url or "https://guba.eastmoney.com", html.unescape(value))
            if url.startswith("https://") and ("eastmoney" in url or "dfcfw.com" in url):
                if url not in images:
                    images.append(url)
        result["images"] = images[:12]
        return result

    @staticmethod
    def empty(message: str) -> dict[str, Any]:
        now = china_now()
        return {
            "meta": {"source": "东方财富股吧公开页面", "status": "empty", "message": message, "updatedAt": now.isoformat(timespec="seconds"), "updatedLabel": now.strftime("%m月%d日 %H:%M"), "dataDate": now.date().isoformat(), "requestCount": 0, "methodology": "仅分析当天发布的技术分析正文"},
            "summary": {"hotStocks": 0, "scannedPosts": 0, "technicalPosts": 0, "filteredPosts": 0, "uniqueAuthors": 0, "acceptancePct": 0, "bullPosts": 0, "bearPosts": 0},
            "quality": {"threshold": 55, "rules": ["至少两类分析证据", "排除辱骂、口号和引流", "仅补充帖主的分析性回复"], "filteredReasons": {}},
            "stocks": [],
        }

    @staticmethod
    def empty_hierarchy(message: str) -> dict[str, Any]:
        now = china_now()
        return {
            "meta": {"source": "东方财富公开板块行情 + 股吧公开页面", "status": "empty", "message": message, "updatedAt": now.isoformat(timespec="seconds"), "updatedLabel": now.strftime("%m月%d日 %H:%M"), "dataDate": now.date().isoformat(), "requestCount": 0, "methodology": "板块与股票按当日成交额排序；详情按需读取", "scope": "10 个热门板块 × 每板块 10 只活跃股"},
            "summary": {"hotSectors": 0, "stocks": 0, "scannedPosts": 0, "candidatePosts": 0, "todayReads": 0, "todayComments": 0},
            "quality": {"threshold": 55, "rules": ["排除辱骂、喊单、短口号与引流", "普通评论不纳入分析，仅补充帖主的分析性回复"]},
            "sectors": [],
        }
