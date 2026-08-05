from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# GitHub's Windows runner may expose a cp1252 console even when the repository
# and Python sources are UTF-8. Keep Chinese diagnostics portable.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

FORBIDDEN_TRACKED = {
    ".env",
    "data/cache.json",
    "data/xhs_samples.json",
    "server.log",
    "server-error.log",
}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "API key assignment": re.compile(
        r"(?i)(api[_-]?key|secret|password|authorization)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}"
    ),
    "Windows user path": re.compile(r"(?i)C:\\Users\\(?!YOUR_|example)")
}


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [ROOT / line for line in completed.stdout.splitlines() if line.strip()]


def main() -> None:
    problems: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in FORBIDDEN_TRACKED:
            problems.append(f"forbidden tracked file: {relative}")
            continue
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                problems.append(f"{label}: {relative}")
    if problems:
        raise SystemExit("隐私检查失败：\n- " + "\n- ".join(problems))
    print("隐私检查通过：未发现被跟踪的缓存、凭据或个人绝对路径。")


if __name__ == "__main__":
    main()
