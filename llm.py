from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from config import settings


ALLOWED_SENTIMENTS = {"看多", "看空", "中性"}


class LLMAnalyzer:
    """Optional semantic analyzer with CLI and OpenAI-compatible adapters.

    Crawling remains the responsibility of Agent Reach/OpenCLI. This class only
    receives public post text and deliberately omits author names, profile data,
    URLs, cookies, and browser state from every model request.
    """

    def __init__(self) -> None:
        self.provider = self._resolve_provider()

    @staticmethod
    def _resolve_provider() -> str:
        configured = settings.llm_provider
        aliases = {
            "chatgpt": "codex",
            "codex-cli": "codex",
            "claudecode": "claude",
            "claude-code": "claude",
            "deepseek": "openai-compatible",
            "api": "openai-compatible",
            "off": "local-keywords",
            "none": "local-keywords",
        }
        configured = aliases.get(configured, configured)
        if configured != "auto":
            return configured

        claude_context = any(
            os.environ.get(key)
            for key in ("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT")
        )
        codex_context = any(
            os.environ.get(key)
            for key in ("CODEX_HOME", "CODEX_THREAD_ID", "CODEX_SANDBOX")
        )
        if claude_context and shutil.which("claude"):
            return "claude"
        if codex_context and shutil.which("codex"):
            return "codex"
        if shutil.which("claude") and not shutil.which("codex"):
            return "claude"
        if shutil.which("codex"):
            return "codex"
        if settings.llm_api_key:
            return "openai-compatible"
        return "local-keywords"

    def enrich_posts(self, posts: list[dict[str, Any]]) -> dict[str, Any]:
        if not posts or self.provider == "local-keywords":
            return {
                "provider": self.provider,
                "status": "fallback",
                "analyzedPosts": 0,
                "message": "使用本地关键词规则" if self.provider == "local-keywords" else "暂无新帖子",
            }

        public_items: list[dict[str, Any]] = []
        indexed_posts: dict[str, dict[str, Any]] = {}
        for index, post in enumerate(posts[:40]):
            post_id = str(index)
            tags = [str(tag)[:80] for tag in (post.get("tags") or [])[:12]]
            public_items.append(
                {
                    "id": post_id,
                    "title": str(post.get("title") or "")[:240],
                    "content": str(post.get("content") or "")[:1200],
                    "tags": tags,
                    "imageText": str(post.get("imageOcrText") or "")[:1200],
                }
            )
            indexed_posts[post_id] = post

        prompt = self._build_prompt(public_items)
        try:
            raw = self._invoke(prompt)
            payload = self._parse_json(raw)
            results = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(results, list):
                raise ValueError("模型未返回 items 数组")
            applied = 0
            for item in results:
                if not isinstance(item, dict):
                    continue
                post = indexed_posts.get(str(item.get("id")))
                if post is None:
                    continue
                sentiment = str(item.get("sentiment") or "中性")
                score = max(-100, min(100, int(item.get("sentimentScore") or 0)))
                post["aiSentiment"] = sentiment if sentiment in ALLOWED_SENTIMENTS else "中性"
                post["aiSentimentScore"] = score
                post["aiSectors"] = self._clean_terms(item.get("sectors"), 8)
                post["aiStocks"] = self._clean_terms(item.get("stocks"), 16)
                post["aiSummary"] = str(item.get("summary") or "")[:240]
                post["aiProvider"] = self.provider
                applied += 1
            return {
                "provider": self.provider,
                "status": "ok",
                "analyzedPosts": applied,
                "message": f"{self.provider} 已分析 {applied} 篇公开帖子文本",
            }
        except Exception as exc:
            return {
                "provider": self.provider,
                "status": "warn",
                "analyzedPosts": 0,
                "message": f"模型分析不可用，已回退本地规则：{type(exc).__name__}",
            }

    @staticmethod
    def _clean_terms(value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            term = re.sub(r"\s+", " ", str(item or "")).strip()[:40]
            if term and term not in result:
                result.append(term)
        return result[:limit]

    @staticmethod
    def _build_prompt(items: list[dict[str, Any]]) -> str:
        return (
            "你是A股社交媒体文本分析器。只依据输入的公开帖子文本，输出严格 JSON，不要解释。"
            "对每项识别：sentiment 只能是看多/看空/中性；sentimentScore 为 -100 到 100；"
            "sectors 为明确提及或高度确定的A股板块；stocks 为明确出现的股票名称或六位代码；"
            "summary 最多60字。不得猜测未出现的股票。输出结构："
            '{"items":[{"id":"0","sentiment":"中性","sentimentScore":0,'
            '"sectors":[],"stocks":[],"summary":""}]}。输入：'
            + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        )

    def _invoke(self, prompt: str) -> str:
        if self.provider == "openai-compatible":
            return self._invoke_api(prompt)
        if self.provider == "codex":
            return self._invoke_codex(prompt)
        if self.provider == "claude":
            return self._invoke_claude(prompt)
        raise RuntimeError(f"不支持的模型提供方：{self.provider}")

    @staticmethod
    def _parse_json(raw: str) -> Any:
        text = str(raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.I | re.S)
        if fenced:
            text = fenced.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = min((pos for pos in (text.find("{"), text.find("[")) if pos >= 0), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start < 0 or end <= start:
                raise
            return json.loads(text[start : end + 1])

    @staticmethod
    def _invoke_api(prompt: str) -> str:
        if not settings.llm_api_key:
            raise RuntimeError("未配置 SENTIBOARD_LLM_API_KEY")
        endpoint = settings.llm_api_base.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        body = json.dumps(
            {
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "SentiBoard/1.0",
            },
            method="POST",
        )
        with urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["choices"][0]["message"]["content"])

    @staticmethod
    def _invoke_codex(prompt: str) -> str:
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("未找到 Codex CLI")
        output_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="sentiboard-codex-", suffix=".txt", delete=False) as handle:
                output_path = Path(handle.name)
            completed = subprocess.run(
                [
                    executable,
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--output-last-message",
                    str(output_path),
                    prompt,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("Codex CLI 调用失败")
            return output_path.read_text(encoding="utf-8")
        finally:
            if output_path is not None:
                output_path.unlink(missing_ok=True)

    @staticmethod
    def _invoke_claude(prompt: str) -> str:
        executable = shutil.which("claude")
        if not executable:
            raise RuntimeError("未找到 Claude Code CLI")
        completed = subprocess.run(
            [executable, "-p", "--output-format", "text", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Claude Code CLI 调用失败")
        return completed.stdout
