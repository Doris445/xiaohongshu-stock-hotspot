from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _chrome_path() -> str | None:
    configured = os.environ.get("SENTIBOARD_CHROME_PATH")
    candidates = [
        configured,
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    return next((str(value) for value in candidates if value and Path(value).is_file()), None)


def _agent_reach_path() -> str | None:
    candidates = [
        shutil.which("agent-reach"),
        shutil.which("agent-reach.exe"),
        str(Path(sys.executable).parent / "agent-reach.exe"),
        str(Path(sys.executable).parent / "agent-reach"),
    ]
    return next((value for value in candidates if value and Path(value).is_file()), None)


def inspect_environment() -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    version_ok = (3, 10) <= sys.version_info[:2] < (3, 14) and sys.maxsize > 2**32
    checks["python"] = {
        "ok": version_ok,
        "value": sys.version.split()[0],
        "message": "需要 64 位 Python 3.10–3.13",
    }
    for module, label in (("numpy", "NumPy"), ("PIL", "Pillow")):
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "installed")
            checks[module] = {"ok": True, "value": version, "message": label}
        except ImportError:
            checks[module] = {"ok": False, "value": None, "message": f"缺少 {label}"}

    checks["node"] = {
        "ok": bool(shutil.which("node") or shutil.which("node.exe")),
        "value": shutil.which("node") or shutil.which("node.exe"),
        "message": "东方财富人工核验助手需要 Node.js 20+",
        "required": False,
    }
    checks["chrome"] = {
        "ok": bool(_chrome_path()),
        "value": _chrome_path(),
        "message": "真实采集与人工核验需要 Chrome",
        "required": False,
    }
    checks["agentReach"] = {
        "ok": bool(_agent_reach_path()),
        "value": _agent_reach_path(),
        "message": "小红书采集需要 Agent Reach；东方财富模式不依赖它",
        "required": False,
    }
    data_root = ROOT / "data"
    try:
        data_root.mkdir(exist_ok=True)
        probe = data_root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    checks["dataDirectory"] = {
        "ok": writable,
        "value": str(data_root),
        "message": "data 目录必须可写，缓存不会上传 Git",
    }
    required_ok = all(
        item["ok"] for item in checks.values() if item.get("required", True)
    )
    return {"ok": required_ok, "python": sys.executable, "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书、东财股吧热点看盘部署自检")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quick", action="store_true", help="仅检查启动必需项")
    args = parser.parse_args()
    report = inspect_environment()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("小红书、东财股吧热点看盘部署自检")
        for name, item in report["checks"].items():
            optional = not item.get("required", True)
            marker = "[OK]" if item["ok"] else ("[WARN]" if optional else "[FAIL]")
            suffix = f" — {item['value']}" if item.get("value") else ""
            if not args.quick or not item["ok"] or not optional:
                print(f"  {marker} {name}{suffix}")
                if not item["ok"]:
                    print(f"    {item['message']}")
        if report["ok"]:
            print("自检通过。")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
