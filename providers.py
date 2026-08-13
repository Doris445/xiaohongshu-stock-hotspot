from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
SCRAPLING_ROOT = ROOT / "vendor" / "Scrapling"
WINDOWS_OCR_SCRIPT = ROOT / "scripts" / "windows_ocr.ps1"
CHINA_TZ = timezone(timedelta(hours=8))


def china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def fetch_public_bytes(url: str, timeout: int = 10) -> bytes:
    """Use bundled Scrapling when available, with a stdlib deployment fallback."""
    if SCRAPLING_ROOT.exists():
        import sys

        scrapling_path = str(SCRAPLING_ROOT)
        if scrapling_path not in sys.path:
            sys.path.insert(0, scrapling_path)
    try:
        from scrapling.fetchers import Fetcher

        return Fetcher.get(url, impersonate="chrome", timeout=timeout).body
    except Exception:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 SentiBoard/0.1",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return response.read()


def parse_xhs_published_date(value: Any, now: datetime | None = None) -> date | None:
    """Parse the date forms returned by Xiaohongshu/OpenCLI.

    Unknown or ambiguous values deliberately return None so they cannot leak
    into a strict same-day dashboard.
    """
    current = (now or china_now()).astimezone(CHINA_TZ)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, CHINA_TZ).date()
        except (OSError, OverflowError, ValueError):
            return None

    text = str(value or "").strip().lower()
    if not text or text in {"刷新时采集", "未知", "--", "none", "null"}:
        return None
    if text in {"刚刚", "今天", "今日"} or re.fullmatch(r"\d+\s*(秒|分钟|小时)前", text):
        return current.date()
    if text.startswith("昨天") or text.startswith("前天"):
        return current.date() - timedelta(days=1 if text.startswith("昨天") else 2)

    numeric = re.fullmatch(r"\d{10,13}", text)
    if numeric:
        timestamp = float(text)
        if len(text) == 13:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, CHINA_TZ).date()
        except (OSError, OverflowError, ValueError):
            return None

    full_date = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if full_date:
        try:
            return date(*(int(part) for part in full_date.groups()))
        except ValueError:
            return None
    month_day = re.search(r"(?<!\d)(\d{1,2})[-/.月](\d{1,2})日?", text)
    if month_day:
        try:
            return date(current.year, int(month_day.group(1)), int(month_day.group(2)))
        except ValueError:
            return None
    return None


def is_xhs_post_today(post: dict[str, Any], now: datetime | None = None) -> bool:
    current = (now or china_now()).astimezone(CHINA_TZ)
    return parse_xhs_published_date(post.get("published"), current) == current.date()


@dataclass(frozen=True)
class StockTarget:
    name: str
    code: str
    symbol: str
    sector: str


STOCK_TARGETS = [
    StockTarget("中际旭创", "300308", "sz300308", "CPO"),
    StockTarget("新易盛", "300502", "sz300502", "CPO"),
    StockTarget("沪电股份", "002463", "sz002463", "PCB"),
    StockTarget("工业富联", "601138", "sh601138", "AI 算力"),
    StockTarget("寒武纪", "688256", "sh688256", "半导体"),
    StockTarget("胜宏科技", "300476", "sz300476", "PCB"),
    StockTarget("兆易创新", "603986", "sh603986", "半导体"),
    StockTarget("中芯国际", "688981", "sh688981", "半导体"),
    StockTarget("光迅科技", "002281", "sz002281", "光通信"),
    StockTarget("生益科技", "600183", "sh600183", "PCB"),
]


def stock_symbol(code: str) -> str:
    """Map an A-share code to Tencent's public quote symbol."""
    value = str(code or "").strip()
    if value.startswith(("6", "9")):
        return f"sh{value}"
    if value.startswith(("4", "8")):
        return f"bj{value}"
    return f"sz{value}"

SECTOR_BOARD_CODES = {
    "半导体": ("BK1036",),
    "CPO": ("BK1128",),
    "光通信": ("BK1136",),
    "PCB": ("BK0877",),
    "AI 算力": ("BK1127", "BK1138"),
    "机器人": ("BK1408",),
}

INDEX_SYMBOLS = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}


class EastmoneySectorConstituentProvider:
    """Read sector constituents from Eastmoney's public quote endpoint."""

    endpoint = (
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=300&po=1&np=1"
        "&fltt=2&invt=2&fid=f3&fs=b:{board}&fields=f12,f14"
    )

    def fetch(self) -> dict[str, list[dict[str, str]]]:
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") == "1":
            return {}
        if SCRAPLING_ROOT.exists():
            import sys

            scrapling_path = str(SCRAPLING_ROOT)
            if scrapling_path not in sys.path:
                sys.path.insert(0, scrapling_path)
        try:
            from scrapling.fetchers import Fetcher
        except Exception:
            return {}

        sectors: dict[str, list[dict[str, str]]] = {}
        for sector, boards in SECTOR_BOARD_CODES.items():
            merged: dict[str, dict[str, str]] = {}
            for board in boards:
                try:
                    response = Fetcher.get(
                        self.endpoint.format(board=board),
                        impersonate="chrome",
                        timeout=10,
                    )
                    payload = json.loads(response.body.decode("utf-8", errors="replace"))
                    rows = payload.get("data", {}).get("diff") or []
                except Exception:
                    continue
                for row in rows:
                    code = str(row.get("f12") or "").strip()
                    name = str(row.get("f14") or "").strip()
                    if not re.fullmatch(r"\d{6}", code) or not name:
                        continue
                    merged[code] = {"code": code, "name": name}
            if merged:
                sectors[sector] = list(merged.values())
        return sectors


class EastmoneySectorQuoteProvider:
    """Fetch one market snapshot for the dashboard's tracked sector boards."""

    endpoint = (
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get?"
        "secids={secids}&fields=f2,f3,f12,f14&fltt=2"
    )

    def fetch(self) -> dict[str, dict[str, Any]]:
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") == "1":
            return {}
        try:
            boards = [board for values in SECTOR_BOARD_CODES.values() for board in values]
            url = self.endpoint.format(secids=",".join(f"90.{board}" for board in boards))
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 SentiBoard/0.1",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            rows = payload.get("data", {}).get("diff") or []
        except Exception:
            return {}
        by_board = {str(row["f12"]): row for row in rows}
        result: dict[str, dict[str, Any]] = {}
        for sector, boards in SECTOR_BOARD_CODES.items():
            matched = [by_board[board] for board in boards if board in by_board]
            if not matched:
                continue
            changes = [float(row["f3"]) for row in matched]
            result[sector] = {
                "name": sector,
                "changePct": round(sum(changes) / len(changes), 2),
                "boards": [
                    {
                        "code": str(row.get("f12") or ""),
                        "name": str(row.get("f14") or ""),
                        "changePct": float(row["f3"]),
                    }
                    for row in matched
                ],
                "source": "东方财富板块行情（延迟快照）",
            }
        return result


class XHSImageOCRProvider:
    """Local Chinese OCR for already selected Xiaohongshu post images.

    The inexpensive first pass only checks low-resolution edge structure for
    any likely text. All matching images from one post are then combined and
    sent through Windows OCR once, so even a short ticker/name is retained
    without paying for a recognition pass on every ordinary photo.
    """

    def __init__(self) -> None:
        self._numpy: Any | None = None
        self._image: Any | None = None
        self._powershell: str | None = None
        self._rapidocr: Any | None = None
        self._status = "not-loaded"

    def _load(self) -> bool:
        if self._status.startswith("ready"):
            return True
        if self._status == "unavailable":
            return False
        try:
            import numpy as np
            from PIL import Image

            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            self._numpy = np
            self._image = Image
            if powershell and WINDOWS_OCR_SCRIPT.exists():
                self._powershell = powershell
                self._status = "ready-windows-ocr"
            else:
                from rapidocr_onnxruntime import RapidOCR

                self._rapidocr = RapidOCR()
                self._status = "ready-rapidocr"
            return True
        except Exception:
            self._status = "unavailable"
            return False

    @staticmethod
    def _has_any_text(image: Any, np: Any) -> bool:
        """Fast, permissive detector; this deliberately is not OCR.

        Text creates repeated horizontal and vertical luminance transitions
        across several neighbouring scan lines. The thresholds are low so a
        stock name or code consisting of only a few glyphs is still kept.
        False positives cost one slot on a per-post contact sheet; false
        negatives would lose market information, so recall is preferred.
        """
        preview = image.copy()
        preview.thumbnail((480, 640))
        rgb = np.asarray(preview.convert("RGB"), dtype=np.int16)
        if rgb.ndim != 3 or min(rgb.shape[:2]) < 40:
            return False
        gray = (rgb[..., 0] * 30 + rgb[..., 1] * 59 + rgb[..., 2] * 11) // 100
        # Ignore the usual Xiaohongshu author watermark zone. Otherwise a
        # content-free photo would look like a text image merely because the
        # platform stamps a few glyphs in its lower-right corner.
        gray = gray.copy()
        watermark_y = int(gray.shape[0] * 0.82)
        watermark_x = int(gray.shape[1] * 0.55)
        gray[watermark_y:, watermark_x:] = 255
        horizontal = np.abs(np.diff(gray, axis=1)) >= 30
        vertical = np.abs(np.diff(gray, axis=0)) >= 30
        row_threshold = max(5, int(horizontal.shape[1] * 0.018))
        col_threshold = max(5, int(vertical.shape[0] * 0.018))
        active_rows = horizontal.sum(axis=1) >= row_threshold
        active_cols = vertical.sum(axis=0) >= col_threshold

        def longest_run(values: Any) -> int:
            best = current = 0
            for value in values.tolist():
                current = current + 1 if value else 0
                best = max(best, current)
            return best

        edge_density = float((horizontal.mean() + vertical.mean()) / 2)
        return bool(
            edge_density >= 0.0025
            and longest_run(active_rows) >= 3
            and longest_run(active_cols) >= 3
            and int(horizontal.sum()) + int(vertical.sum()) >= 80
        )

    @staticmethod
    def _normalize_ocr_text(value: str) -> str:
        compact = re.sub(r"\s+", "", value or "")
        # Windows OCR can treat the final round O in CPO as Chinese punctuation
        # on mixed Chinese/English cards (for example: "cp。").
        compact = re.sub(r"(?i)cp[。．\.0oＯ]", "CPO", compact)
        return compact[:20000]

    def _recognize_sheet(self, sheet: Any) -> str:
        if self._rapidocr is not None:
            try:
                result, _ = self._rapidocr(self._numpy.asarray(sheet))
                values = [
                    str(row[1])
                    for row in (result or [])
                    if len(row) >= 3 and float(row[2]) >= 0.55
                ]
                return self._normalize_ocr_text("\n".join(values))
            except Exception:
                return ""
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="xhs-ocr-", suffix=".png", delete=False) as handle:
                temp_path = Path(handle.name)
            sheet.save(temp_path, format="PNG")
            completed = subprocess.run(
                [
                    self._powershell or "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(WINDOWS_OCR_SCRIPT),
                    "-ImagePath",
                    str(temp_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return ""
            return self._normalize_ocr_text(completed.stdout)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def extract(self, image_urls: list[str], max_images: int = 4) -> tuple[str, int, int, str]:
        if not image_urls or max_images <= 0:
            return "", 0, 0, self._status
        if not self._load():
            return "", 0, 0, self._status
        def load_image(url: str) -> Any | None:
            try:
                request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(request, timeout=15) as response:
                    payload = response.read(12 * 1024 * 1024 + 1)
                if len(payload) > 12 * 1024 * 1024:
                    return None
                return self._image.open(BytesIO(payload)).convert("RGB")
            except Exception:
                return None

        selected = image_urls[:max_images]
        with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
            images = [image for image in pool.map(load_image, selected) if image is not None]
        if not images:
            return "", 0, 0, self._status
        text_images = [image for image in images if self._has_any_text(image, self._numpy)]
        skipped = len(images) - len(text_images)
        if not text_images:
            return "", 0, skipped, self._status

        # One OCR inference per post: place up to four resized images into a
        # bounded contact sheet. We only need stock-name presence, not layout.
        tile_width, tile_height = 720, 900
        columns = 1 if len(text_images) == 1 else 2
        rows = (len(text_images) + columns - 1) // columns
        sheet = self._image.new("RGB", (tile_width * columns, tile_height * rows), "white")
        for index, image in enumerate(text_images):
            image.thumbnail((tile_width, tile_height))
            x = (index % columns) * tile_width + (tile_width - image.width) // 2
            y = (index // columns) * tile_height + (tile_height - image.height) // 2
            sheet.paste(image, (x, y))

        text = self._recognize_sheet(sheet)
        return text, len(text_images), skipped, self._status


class TencentQuoteProvider:
    """Fetches A-share snapshots through Tencent's public quote endpoint.

    Scrapling is intentionally used here as the HTTP client so the data layer
    has one reusable fetch/session implementation. Failures never erase the
    last-known values supplied by the caller.
    """

    endpoint = "https://qt.gtimg.cn/q={symbols}"

    def fetch(self, codes: list[str] | None = None) -> dict[str, dict[str, float | str]]:
        symbols = [stock_symbol(code) for code in codes] if codes else [t.symbol for t in STOCK_TARGETS]
        return self.fetch_symbols(symbols)

    def fetch_symbols(
        self, symbols: list[str]
    ) -> dict[str, dict[str, float | str | int | None]]:
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") == "1":
            return {}

        try:
            symbols = [symbol for symbol in dict.fromkeys(symbols) if len(symbol) == 8]
            if not symbols:
                return {}
            url = self.endpoint.format(symbols=",".join(symbols))
            raw = fetch_public_bytes(url).decode("gbk", errors="ignore")
        except Exception:
            return {}

        quotes: dict[str, dict[str, float | str]] = {}
        for line in raw.splitlines():
            match = re.search(r'v_(\w+)="(.*)";', line)
            if not match:
                continue
            symbol, payload = match.groups()
            fields = payload.split("~")
            if len(fields) < 6:
                continue
            try:
                current = float(fields[3])
                previous = float(fields[4])
                change = ((current - previous) / previous * 100) if previous else 0.0
                quotes[symbol] = {
                    "name": fields[1],
                    "price": current,
                    "changePct": round(change, 2),
                    "quoteAt": fields[30] if len(fields) > 30 else "",
                    "quoteDate": self._quote_date(fields[30] if len(fields) > 30 else ""),
                    "volume": int(float(fields[6])) if len(fields) > 6 and fields[6] else None,
                    "source": "腾讯行情",
                }
            except (ValueError, IndexError):
                continue
        return quotes

    @staticmethod
    def _quote_date(value: str) -> str | None:
        match = re.match(r"(20\d{2})(\d{2})(\d{2})", str(value or ""))
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


class AgentReachXHSProvider:
    """Read-only XiaoHongShu adapter routed through Agent Reach/OpenCLI.

    The adapter never logs in or reads browser cookies. When no explicitly
    available backend exists, the dashboard reports demo mode and uses a local
    fixture. This keeps UI work unblocked without mislabeling sample content as
    live platform data.
    """

    REQUEST_DELAY_SECONDS = 6.0
    DETAIL_SESSION = "sentiboard-xhs-detail"
    DETAIL_EXTRACT_JS = r"""
      (() => {
        const bodyText = document.body?.innerText || '';
        const clean = (el) => (el?.textContent || '').replace(/\s+/g, ' ').trim();
        const images = [];
        const seen = new Set();
        const normalizeImage = (raw) => {
          if (!raw || typeof raw !== 'string') return '';
          try {
            const url = new URL(raw, location.href);
            const host = url.hostname.toLowerCase();
            if (url.protocol !== 'https:' || !(host.endsWith('.xhscdn.com') || host.endsWith('.xiaohongshu.com'))) return '';
            return url.href;
          } catch (_) { return ''; }
        };
        const pushImage = (raw) => {
          const url = normalizeImage(raw);
          if (!url || seen.has(url)) return;
          seen.add(url);
          images.push(url);
        };

        const pathMatch = (location.pathname || '').match(/\/(?:explore|note|search_result|discovery\/item)\/([a-f0-9]+)/i);
        const noteId = pathMatch?.[1] || '';
        try {
          const state = window.__INITIAL_STATE__;
          const noteData = state?.note?.noteDetailMap || state?.note?.note || {};
          const candidates = [];
          if (noteId && noteData?.[noteId]) candidates.push(noteData[noteId]?.note || noteData[noteId]);
          if (!candidates.length && Object.keys(noteData || {}).length === 1) {
            const only = noteData[Object.keys(noteData)[0]];
            candidates.push(only?.note || only);
          }
          for (const note of candidates) {
            for (const item of (Array.isArray(note?.imageList) ? note.imageList : [])) {
              pushImage(item?.urlDefault || item?.urlPre || item?.url
                || item?.infoList?.find(i => i?.imageScene === 'WB_DFT')?.url
                || item?.infoList?.[0]?.url || '');
            }
          }
        } catch (_) {}
        if (!images.length) {
          const selectors = [
            '.swiper-slide img', '.carousel-image img', '.note-slider img',
            '.note-image img', '.image-wrapper img',
            '#noteContainer .media-container img[src*="xhscdn"]'
          ];
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach(img => pushImage(
              img.currentSrc || img.src || img.getAttribute('data-src') || ''
            ));
          }
        }

        const tags = [];
        document.querySelectorAll('#detail-desc a.tag, #detail-desc a[href*="search_result"]').forEach(el => {
          const value = clean(el).replace(/^#/, '');
          if (value && !tags.includes(value)) tags.push(value);
        });
        return {
          securityBlock: /安全限制|访问链接异常/.test(bodyText)
            || /website-login\/error|error_code=300017|error_code=300031/.test(location.href),
          loginWall: /登录后查看|请登录/.test(bodyText),
          title: clean(document.querySelector('#detail-title, .title')),
          content: clean(document.querySelector('#detail-desc, .desc, .note-text')),
          author: clean(document.querySelector('.username, .author-wrapper .name')),
          likes: clean(document.querySelector('.interact-container .like-wrapper .count')),
          collects: clean(document.querySelector('.interact-container .collect-wrapper .count')),
          comments: clean(document.querySelector('.interact-container .chat-wrapper .count')),
          tags,
          images: images.slice(0, 12)
        };
      })()
    """

    def __init__(self) -> None:
        self._last_platform_request = 0.0
        self.ocr_provider = XHSImageOCRProvider()

    def status(self) -> dict[str, str | None]:
        if not shutil.which("agent-reach"):
            return {"status": "off", "backend": None, "message": "未检测到 agent-reach"}
        try:
            result = subprocess.run(
                ["agent-reach", "doctor", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            payload = json.loads(result.stdout)
            xhs = payload.get("xiaohongshu", {})
            message = xhs.get("message", "小红书后端不可用")
            backend = xhs.get("active_backend")
            status = xhs.get("status", "off")
            # Agent Reach intentionally keeps authenticated platforms at warn
            # until a real platform command succeeds. A connected OpenCLI
            # bridge is enough for collect() to attempt that read-only command.
            if not backend and "OpenCLI 桥接已连接" in message and self._opencli_prefix():
                backend = "OpenCLI"
                status = "ready"
            return {"status": status, "backend": backend, "message": message}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return {"status": "off", "backend": None, "message": "无法读取 agent-reach 状态"}

    def collect(
        self,
        query_specs: list[dict[str, Any]],
        limit: int = 10,
        detail_limit: int = 12,
        known_posts: dict[str, dict[str, Any]] | None = None,
        reuse_discovery: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if os.environ.get("STOCK_DASHBOARD_DISABLE_NETWORK") == "1":
            return [], {
                "status": "off", "backend": None, "message": "测试模式未访问小红书",
                "platformRequestAttempted": False, "requestCount": 0,
            }

        state = self.status()
        backend = state.get("backend")
        if not backend:
            return [], {**state, "platformRequestAttempted": False, "requestCount": 0}

        if backend != "OpenCLI" or not self._opencli_prefix():
            return [], {
                **state, "message": f"已识别 {backend}，当前 MVP 暂未启用该适配器",
                "platformRequestAttempted": False, "requestCount": 0,
            }

        known_posts = known_posts or {}
        posts_by_url: dict[str, dict[str, Any]] = {}
        successful_queries = 0
        stopped_for_safety = False
        rejected_non_today = 0
        search_requests = 0
        detail_requests = 0
        ocr_images_processed = 0
        ocr_images_skipped = 0
        ocr_images_examined = 0
        ocr_posts_processed = 0

        if reuse_discovery:
            for url, known in known_posts.items():
                if not url or not is_xhs_post_today(known):
                    continue
                candidate = dict(known)
                candidate["url"] = url
                if not candidate.get("matchedTargets"):
                    candidate["matchedTargets"] = [
                        {
                            "targetType": candidate.get("targetType"),
                            "targetName": candidate.get("targetName"),
                            "query": candidate.get("query"),
                        }
                    ]
                candidate["needsDetail"] = not bool(
                    candidate.get("isDetailed")
                    and (str(candidate.get("content") or "").strip() or candidate.get("tags"))
                )
                posts_by_url[url] = candidate

        # Stage 1: discover broadly. Detail requests are selected only after all
        # searches finish, so one popular note cannot consume every query's budget.
        for spec in ([] if reuse_discovery else query_specs):
            query = spec["query"]
            try:
                self._pace_platform_request()
                search_requests += 1
                result = self._run_opencli(
                    [
                        "xiaohongshu",
                        "search",
                        query,
                        "--limit",
                        str(limit),
                        "--window",
                        "background",
                        "-f",
                        "json",
                    ],
                    timeout=60,
                )
                if self._requires_safety_stop(result):
                    stopped_for_safety = True
                    break
                if result.returncode == 0 and result.stdout.strip():
                    raw_candidates = self._normalize_search_result(result.stdout, spec, limit)
                    candidates = [post for post in raw_candidates if is_xhs_post_today(post)]
                    rejected_non_today += len(raw_candidates) - len(candidates)
                    for candidate in candidates:
                        url = candidate.get("url") or ""
                        if not url:
                            continue
                        target = {
                            "targetType": spec.get("targetType"),
                            "targetName": spec.get("targetName"),
                            "query": spec.get("query"),
                        }
                        existing = posts_by_url.get(url)
                        if existing:
                            contexts = existing.setdefault("matchedTargets", [])
                            if target not in contexts:
                                contexts.append(target)
                            existing["likes"] = max(int(existing.get("likes") or 0), int(candidate.get("likes") or 0))
                            continue
                        known = known_posts.get(url)
                        if known:
                            current_likes = candidate["likes"]
                            previous_likes = _to_int(known.get("likes"))
                            known_comments_available = bool(
                                known.get("commentCountAvailable")
                                if "commentCountAvailable" in known
                                else known.get("isDetailed") and known.get("comments") is not None
                            )
                            candidate = {
                                **known,
                                **candidate,
                                "content": known.get("content", ""),
                                "comments": known.get("comments") if known_comments_available else None,
                                "commentCountAvailable": known_comments_available,
                                "collects": _to_int(known.get("collects")),
                                "tags": known.get("tags") or [],
                                "isDetailed": bool(known.get("isDetailed")),
                            }
                            threshold = max(20, round(previous_likes * 0.2))
                            candidate["needsDetail"] = (
                                not candidate["isDetailed"]
                                or abs(current_likes - previous_likes) >= threshold
                            )
                        else:
                            candidate["needsDetail"] = True
                        candidate["matchedTargets"] = [target]
                        posts_by_url[url] = candidate
                    successful_queries += 1
            except (OSError, subprocess.TimeoutExpired):
                stopped_for_safety = True
                break

        posts = list(posts_by_url.values())
        selected = self._select_detail_candidates(posts, query_specs, max(0, int(detail_limit)))
        # Stage 2: globally selected text enrichment. Always use the stable
        # note adapter first. Optional image reading must never prevent the
        # remaining notes from getting正文/tags evidence.
        for post in selected:
            if stopped_for_safety or not post.get("needsDetail"):
                continue
            try:
                self._pace_platform_request()
                detail_requests += 1
                detail, safety_stop = self._collect_note_detail(post["url"])
                if safety_stop:
                    stopped_for_safety = True
                    break
                if detail:
                    post.update(detail)
                    post["isDetailed"] = True
                    post["commentCountAvailable"] = True
                    post["detailFetchedAt"] = time.time()
            except (OSError, subprocess.TimeoutExpired):
                stopped_for_safety = True
                break

        # Stage 3: attempt image URLs for at most two already-detailed leaders.
        # A page-specific image failure is non-fatal because the text evidence
        # above is already valid and the rest of the batch must be preserved.
        image_candidates = [post for post in selected if post.get("isDetailed")][:1]
        image_session_used = False
        for post in image_candidates:
            try:
                self._pace_platform_request()
                detail_requests += 1
                image_session_used = True
                image_detail, _ = self._collect_detail_with_images(post["url"])
                if image_detail.get("images"):
                    post["images"] = image_detail["images"]
            except (OSError, subprocess.TimeoutExpired):
                break
        if image_session_used:
            try:
                self._run_opencli(
                    ["browser", self.DETAIL_SESSION, "close", "--window", "background"],
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        # OCR is local and only runs for globally selected image-bearing notes.
        for post in selected:
            if not post.get("images") or post.get("imageOcrText"):
                continue
            if ocr_posts_processed >= 12:
                break
            remaining = 24 - ocr_images_examined
            if remaining <= 0:
                break
            ocr_text, image_count, skipped_count, ocr_status = self.ocr_provider.extract(
                post.get("images") or [], max_images=min(4, remaining)
            )
            post.update(
                {
                    "imageOcrText": ocr_text,
                    "ocrImageCount": image_count,
                    "ocrSkippedImageCount": skipped_count,
                    "ocrStatus": ocr_status,
                    "ocrSampledAt": time.time(),
                }
            )
            ocr_images_processed += image_count
            ocr_images_skipped += skipped_count
            ocr_images_examined += image_count + skipped_count
            if image_count:
                ocr_posts_processed += 1

        for post in posts:
            post.pop("needsDetail", None)

        if not successful_queries and not posts:
            message = "触发安全停止：未重试，请检查验证码或登录状态" if stopped_for_safety else "OpenCLI 已连接，但小红书检索失败"
            return [], {
                **state, "status": "warn", "message": message,
                "platformRequestAttempted": search_requests > 0,
                "requestCount": search_requests + detail_requests,
                "searchRequests": search_requests, "detailRequests": detail_requests,
            }
        return posts, {
            **state,
            "status": "ok",
            "backend": "OpenCLI",
            "message": (
                (f"复用今日 {len(posts)} 篇搜索结果并补全详情；" if reuse_discovery else f"今日低频采样完成 {successful_queries} 组检索；纳入 {len(posts)} 帖，")
                +
                f"本地 OCR 处理 {ocr_images_processed} 张有文字图片、跳过 {ocr_images_skipped} 张无文字图片，"
                f"剔除 {rejected_non_today} 条非今日或日期不明内容"
                + ("；检测到异常后已停止，未自动重试" if stopped_for_safety else "")
            ),
            "dataDate": china_now().date().isoformat(),
            "acceptedToday": len(posts),
            "rejectedNonToday": rejected_non_today,
            "platformRequestAttempted": (search_requests + detail_requests) > 0,
            "requestCount": search_requests + detail_requests,
            "searchRequests": search_requests,
            "detailRequests": detail_requests,
            "selectedForDetail": len(selected),
            "detailedPosts": sum(bool(post.get("isDetailed")) for post in posts),
            "discoveryMode": "reused" if reuse_discovery else "searched",
            "ocrPosts": sum(bool(post.get("imageOcrText")) for post in posts),
            "ocrImages": sum(int(post.get("ocrImageCount") or 0) for post in posts),
            "ocrSkippedImages": sum(int(post.get("ocrSkippedImageCount") or 0) for post in posts),
            "ocrStatus": self.ocr_provider._status,
        }

    @staticmethod
    def _select_detail_candidates(
        posts: list[dict[str, Any]], query_specs: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        """Choose a diverse global detail set before spending platform requests."""
        if limit <= 0:
            return []
        ranked = sorted(
            posts,
            key=lambda post: (bool(post.get("needsDetail", True)), int(post.get("likes") or 0)),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        selected_urls: set[str] = set()
        for spec in query_specs:
            target_type = spec.get("targetType")
            target_name = spec.get("targetName")
            candidate = next(
                (
                    post for post in ranked
                    if post.get("url") not in selected_urls
                    and any(
                        target.get("targetType") == target_type and target.get("targetName") == target_name
                        for target in (post.get("matchedTargets") or [])
                    )
                ),
                None,
            )
            if candidate:
                selected.append(candidate)
                selected_urls.add(candidate["url"])
            if len(selected) >= limit:
                return selected
        for post in ranked:
            if post.get("url") in selected_urls:
                continue
            selected.append(post)
            selected_urls.add(post["url"])
            if len(selected) >= limit:
                break
        return selected

    def _pace_platform_request(self) -> None:
        elapsed = time.monotonic() - self._last_platform_request
        if self._last_platform_request and elapsed < self.REQUEST_DELAY_SECONDS:
            time.sleep(self.REQUEST_DELAY_SECONDS - elapsed)
        self._last_platform_request = time.monotonic()

    def _collect_detail_with_images(self, url: str) -> tuple[dict[str, Any], bool]:
        """Open a signed note once, then extract text and image URLs in-page."""
        opened = self._run_opencli(
            ["browser", self.DETAIL_SESSION, "open", url, "--window", "background"],
            timeout=60,
        )
        if self._requires_safety_stop(opened):
            return {}, True
        if opened.returncode != 0:
            return {}, False
        evaluated = self._run_opencli(
            [
                "browser",
                self.DETAIL_SESSION,
                "eval",
                self.DETAIL_EXTRACT_JS,
                "--window",
                "background",
            ],
            timeout=30,
        )
        if self._requires_safety_stop(evaluated):
            return {}, True
        if evaluated.returncode != 0 or not evaluated.stdout.strip():
            return {}, False
        detail = self._normalize_browser_detail(evaluated.stdout)
        if detail.pop("securityBlock", False) or detail.pop("loginWall", False):
            return {}, True
        return detail, False

    def _collect_note_detail(self, url: str) -> tuple[dict[str, Any], bool]:
        """Read note text/tags/counts without requesting any comment body."""
        result = self._run_opencli(
            ["xiaohongshu", "note", url, "--window", "background", "-f", "json"],
            timeout=60,
        )
        # On Windows a .cmd shim may return code 1 after successfully printing
        # the note because '&xsec_source=' in the signed URL is interpreted by
        # cmd.exe. Preserve valid structured output before evaluating the code.
        if result.stdout.strip():
            detail = self._normalize_detail(result.stdout)
            if detail:
                return detail, False
        if self._requires_safety_stop(result):
            return {}, True
        return {}, False

    @staticmethod
    def _requires_safety_stop(result: subprocess.CompletedProcess[str]) -> bool:
        if result.returncode == 0:
            return False
        combined = f"{result.stdout}\n{result.stderr}".lower()
        safety_markers = ("auth_required", "captcha", "验证码", "频繁", "429", "rate limit", "risk")
        return any(marker in combined for marker in safety_markers) or result.returncode == 77

    @classmethod
    def _opencli_prefix(cls) -> list[str]:
        executable = shutil.which("opencli.cmd") or shutil.which("opencli")
        if executable and os.name != "nt":
            return [executable]

        agent_reach_home = Path(
            os.environ.get("AGENT_REACH_HOME") or (Path.home() / ".agent-reach")
        )
        entry = (
            agent_reach_home
            / "tools"
            / "npm-global"
            / "node_modules"
            / "@jackwener"
            / "opencli"
            / "dist"
            / "src"
            / "main.js"
        )
        node = shutil.which("node")
        if not node:
            candidates = [
                *agent_reach_home.glob("tools/node*/node.exe"),
                *agent_reach_home.glob("tools/node*/bin/node"),
            ]
            node = str(candidates[0]) if candidates else None
        if node and entry.is_file():
            return [str(node), str(entry)]
        return [executable] if executable else []

    @classmethod
    def _run_opencli(cls, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*cls._opencli_prefix(), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def _normalize_search_result(raw: str, spec: dict[str, str], limit: int) -> list[dict[str, Any]]:
        """Tolerant normalizer for changing OpenCLI response envelopes."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []

        if isinstance(payload, dict):
            candidates = payload.get("items") or payload.get("feeds") or payload.get("data") or []
            if isinstance(candidates, dict):
                candidates = candidates.get("items") or candidates.get("feeds") or []
        else:
            candidates = payload

        normalized: list[dict[str, Any]] = []
        for item in candidates[:limit] if isinstance(candidates, list) else []:
            if not isinstance(item, dict):
                continue
            card = item.get("note_card") or item.get("noteCard") or item
            interact = card.get("interact_info") or card.get("interactInfo") or {}
            user = card.get("user") or {}
            normalized.append(
                {
                    "query": spec["query"],
                    "targetType": spec["targetType"],
                    "targetName": spec["targetName"],
                    "title": card.get("display_title") or card.get("title") or "未命名笔记",
                    "content": card.get("desc") or card.get("content") or "",
                    "author": user.get("nickname") or card.get("author") or "小红书用户",
                    "likes": _to_int(interact.get("liked_count") or interact.get("likedCount") or card.get("likes")),
                    "comments": None,
                    "commentCountAvailable": False,
                    "url": card.get("url") or item.get("url") or "",
                    "published": (
                        card.get("published_at")
                        or card.get("publish_time")
                        or card.get("publishTime")
                        or card.get("time")
                        or card.get("create_time")
                        or item.get("published_at")
                        or item.get("publish_time")
                        or item.get("publishTime")
                        or "日期不明"
                    ),
                    "tags": [],
                    "isDetailed": False,
                }
            )
        return normalized

    @staticmethod
    def _normalize_detail(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # OpenCLI site adapters may emit YAML even when the global format
            # flag is lost behind a Windows .cmd signed-URL boundary.
            yaml_fields: dict[str, str] = {}
            for match in re.finditer(
                r"^-\s*field:\s*([^\r\n]+)\r?\n\s+value:\s*([^\r\n]*)",
                raw,
                flags=re.MULTILINE,
            ):
                key = match.group(1).strip()
                value = match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1].replace("''", "'")
                yaml_fields[key] = value
            if not yaml_fields:
                return {}
            payload = yaml_fields

        if isinstance(payload, list):
            fields = {
                str(item.get("field")): item.get("value")
                for item in payload
                if isinstance(item, dict) and item.get("field")
            }
        elif isinstance(payload, dict):
            fields = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        else:
            return {}

        tags_value = fields.get("tags") or ""
        tags = [
            tag.strip().lstrip("#")
            for tag in re.split(r"[,，]|\s+(?=#)", str(tags_value))
            if tag.strip().lstrip("#")
        ]
        normalized = {
            "title": fields.get("title") or "未命名笔记",
            "content": fields.get("content") or "",
            "author": fields.get("author") or "小红书用户",
            "likes": _to_int(fields.get("likes")),
            "comments": _to_int(fields.get("comments")),
            "commentCountAvailable": True,
            "collects": _to_int(fields.get("collects")),
            "tags": tags,
        }
        published = (
            fields.get("published")
            or fields.get("published_at")
            or fields.get("publish_time")
            or fields.get("publishTime")
            or fields.get("date")
        )
        if published:
            normalized["published"] = published
        return normalized

    @staticmethod
    def _normalize_browser_detail(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            return {}

        images: list[str] = []
        for value in payload.get("images") or []:
            if not isinstance(value, str):
                continue
            try:
                from urllib.parse import urlparse

                parsed = urlparse(value)
                host = (parsed.hostname or "").lower()
                if parsed.scheme == "https" and (
                    host.endswith(".xhscdn.com") or host.endswith(".xiaohongshu.com")
                ):
                    images.append(value)
            except ValueError:
                continue
        return {
            "title": payload.get("title") or "未命名笔记",
            "content": payload.get("content") or "",
            "author": payload.get("author") or "小红书用户",
            "likes": _to_int(payload.get("likes")),
            "comments": _to_int(payload.get("comments")),
            "commentCountAvailable": True,
            "collects": _to_int(payload.get("collects")),
            "tags": [str(tag).lstrip("#") for tag in (payload.get("tags") or []) if str(tag).strip()],
            "images": list(dict.fromkeys(images))[:12],
            "securityBlock": bool(payload.get("securityBlock")),
            "loginWall": bool(payload.get("loginWall")),
        }

def _to_int(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return 0
    text = str(value).strip().lower().replace(",", "")
    multiplier = 10000 if "万" in text or "w" in text else 1000 if "k" in text else 1
    match = re.search(r"[\d.]+", text)
    return int(float(match.group()) * multiplier) if match else 0
