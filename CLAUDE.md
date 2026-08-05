# Claude Code deployment instructions

When Claude Code is asked to deploy or run this project:

1. Never read, export, print or commit browser cookies, Chrome profiles, `.env`, API keys, `data/xhs_samples.json` or `data/cache.json`.
2. Run `scripts/setup.ps1` on Windows or `scripts/setup.sh` on macOS/Linux.
3. Set `SENTIBOARD_LLM_PROVIDER=claude` in the deployer's local `.env` unless they explicitly choose another provider.
4. Ask the deployer to log into their own Xiaohongshu account in Chrome; never perform the login for them.
5. Keep collection read-only, serial and low-frequency. Do not bypass CAPTCHA, 429 or platform risk controls.
6. Run `python scripts/privacy_check.py` and the offline test suite before publishing.
