from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load_local_env() -> None:
    """Load a project-local .env without ever overwriting real environment variables."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()


@dataclass(frozen=True)
class Settings:
    llm_provider: str = os.environ.get("SENTIBOARD_LLM_PROVIDER", "auto").strip().lower()
    llm_api_key: str = (
        os.environ.get("SENTIBOARD_LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()
    llm_api_base: str = os.environ.get(
        "SENTIBOARD_LLM_API_BASE", "https://api.deepseek.com"
    ).strip()
    llm_model: str = os.environ.get("SENTIBOARD_LLM_MODEL", "deepseek-chat").strip()
    refresh_token: str = os.environ.get("SENTIBOARD_REFRESH_TOKEN", "").strip()


settings = Settings()
