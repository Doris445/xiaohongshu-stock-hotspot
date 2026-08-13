from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PROFILE_ROOT = ROOT / "data" / "eastmoney_browser_profile"
CDP_SCRIPT = ROOT / "scripts" / "eastmoney_browser_session.mjs"


class VerificationRequired(RuntimeError):
    pass


class EastmoneyBrowserSession:
    """Project-owned Chrome profile; never reads or exports browser cookies."""

    def __init__(self) -> None:
        self.port = max(1024, int(os.environ.get("SENTIBOARD_EASTMONEY_CDP_PORT", "9333")))
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.profile_root = PROFILE_ROOT
        self._process: subprocess.Popen[Any] | None = None

    @staticmethod
    def _node() -> str | None:
        return shutil.which("node") or shutil.which("node.exe")

    @staticmethod
    def _chrome() -> str | None:
        configured = os.environ.get("SENTIBOARD_CHROME_PATH")
        candidates = [
            configured,
            shutil.which("chrome"),
            shutil.which("chrome.exe"),
            shutil.which("google-chrome"),
            shutil.which("chromium"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        return next((str(value) for value in candidates if value and Path(value).is_file()), None)

    def _run_helper(self, command: str, url: str = "", timeout: int = 40) -> dict[str, Any]:
        node = self._node()
        if not node or not CDP_SCRIPT.is_file():
            return {"status": "unavailable", "message": "缺少 Node.js 或隔离浏览器助手"}
        result = subprocess.run(
            [node, str(CDP_SCRIPT), command, self.endpoint, url],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {"status": "offline", "message": "隔离浏览器状态无法解析"}
        if not isinstance(payload, dict):
            return {"status": "offline", "message": "隔离浏览器返回格式异常"}
        return payload

    def status(self) -> dict[str, Any]:
        result = self._run_helper("status", timeout=12)
        result.pop("html", None)
        result["isolated"] = True
        result["profileLabel"] = "项目专属东方财富会话"
        return result

    def open_for_verification(self, url: str) -> dict[str, Any]:
        chrome = self._chrome()
        if not chrome:
            return {"status": "unavailable", "message": "未找到 Chrome，请配置 SENTIBOARD_CHROME_PATH"}
        if not url.startswith("https://guba.eastmoney.com/news,"):
            raise ValueError("没有可用于人工核验的普通股吧帖子")
        self.profile_root.mkdir(parents=True, exist_ok=True)
        args = [
            chrome,
            f"--user-data-dir={self.profile_root}",
            "--profile-directory=Default",
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url,
        ]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self._process = subprocess.Popen(
            args, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        for _ in range(20):
            time.sleep(0.25)
            state = self.status()
            if state.get("status") not in {"offline", "unavailable"}:
                break
        return {
            **state,
            "message": "隔离窗口已打开，请手动完成东方财富滑块，然后回到看板点击“检查核验”",
        }

    def fetch_html(self, url: str) -> str:
        result = self._run_helper("fetch", url=url, timeout=45)
        if result.get("status") == "verification_required":
            raise VerificationRequired("东方财富要求重新进行人工核验")
        if result.get("status") != "ready" or not result.get("html"):
            raise RuntimeError(str(result.get("message") or "隔离浏览器正文读取失败"))
        return str(result["html"])
