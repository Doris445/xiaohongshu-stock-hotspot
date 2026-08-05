# Privacy

SentiBoard is local-first. A deployment belongs to the person running it and must use that person's own explicitly controlled Xiaohongshu session.

## Data that stays local

- Xiaohongshu login state and browser session
- collected post author names, signed URLs, text and images
- OCR output and same-day caches
- API keys and refresh tokens
- server logs

These paths are ignored by Git: `.env`, `data/xhs_samples.json`, `data/cache.json`, browser/profile directories, cookies, credentials and logs.

## Data sent to an optional model

When Codex, Claude Code or an OpenAI-compatible API is enabled, SentiBoard sends only truncated public post title, body, tags and image OCR text for classification. It does not send author names, profile data, post URLs, cookies, browser state or API keys as prompt content.

Set `SENTIBOARD_LLM_PROVIDER=local-keywords` to keep all analysis on the local machine.

## Authentication boundary

SentiBoard does not automate Xiaohongshu login and does not export browser cookies. OpenCLI may use a Chrome session that the deployer has already opened and explicitly controls. If no valid session exists, collection must stop and ask the deployer to log in manually.
