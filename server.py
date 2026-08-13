from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from config import settings
from service import DashboardService


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SERVICE = DashboardService()


class Handler(BaseHTTPRequestHandler):
    server_version = "SentiBoard/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/dashboard":
            source_date = (parse_qs(parsed.query).get("date") or [""])[0]
            if source_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
                self._json(400, {"error": "日期格式必须为 YYYY-MM-DD"})
                return
            dashboard = SERVICE.dashboard_for_date(source_date) if source_date else SERVICE.current()
            if dashboard is None:
                self._json(404, {"error": "该日期没有已归档的小红书帖子"})
            else:
                self._json(200, dashboard)
            return
        if path == "/api/health":
            self._json(200, {"ok": True})
            return
        if path == "/api/config":
            self._json(200, {"refreshTokenRequired": bool(settings.refresh_token)})
            return
        if path == "/api/history":
            source_date = (parse_qs(parsed.query).get("date") or [""])[0]
            if source_date:
                snapshot = SERVICE.history_snapshot(source_date)
                if snapshot is None:
                    self._json(404, {"error": "未找到该日期的历史快照"})
                else:
                    self._json(200, snapshot)
            else:
                self._json(200, {"dates": SERVICE.history_index()})
            return
        if path == "/":
            path = "/index.html"
        self._static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/refresh", "/api/history/validate"}:
            self._json(404, {"error": "Not found"})
            return
        if settings.refresh_token:
            supplied = self.headers.get("X-Refresh-Token", "")
            if not hmac.compare_digest(supplied, settings.refresh_token):
                self._json(401, {"error": "刷新令牌无效"})
                return
        try:
            if parsed.path == "/api/history/validate":
                source_date = (parse_qs(parsed.query).get("date") or [""])[0]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
                    self._json(400, {"error": "日期格式必须为 YYYY-MM-DD"})
                    return
                data = SERVICE.validate_midday(source_date)
            else:
                data = SERVICE.refresh()
            self._json(200, data)
        except ValueError as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": "操作失败", "detail": str(exc)})

    def _static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self._json(404, {"error": "Not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, payload: object) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="A股社媒情绪看板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not settings.refresh_token:
        parser.error("监听非回环地址前必须配置 SENTIBOARD_REFRESH_TOKEN")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SentiBoard running at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
