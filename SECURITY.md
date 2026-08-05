# Security Policy

## Supported version

The latest `main` branch is supported during the MVP stage.

## Safe deployment

- Keep the default `127.0.0.1` binding unless you understand the exposure.
- A non-loopback binding requires `SENTIBOARD_REFRESH_TOKEN`.
- Never commit `.env`, cookies, browser profiles, runtime caches or logs.
- Use a dedicated low-privilege Xiaohongshu account if possible, keep collection read-only and low-frequency, and stop when CAPTCHA, 429 or risk-control signals appear.
- Rotate an API key immediately if it is ever printed, committed or shared.

## Reporting

Please open a private GitHub security advisory for credential exposure, authentication bypass, path traversal or unintended platform write behavior. Do not include live cookies, API keys or signed Xiaohongshu URLs in a public issue.
