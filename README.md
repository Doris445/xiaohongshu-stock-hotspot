# 小红书股票热点看盘 · SentiBoard

SentiBoard 是一个本地优先、手动刷新的 A 股社交情绪看板。它聚合当天的小红书公开帖子与当日行情，展示热门板块、板块情绪、热门股票 Top 10，以及互动量最高的帖子和配图。

> 本项目只做信息整理，不构成投资建议。小红书自动化访问存在平台风控风险，请使用自己明确控制的账号并保持低频。

## 功能

- 严格只纳入北京时间当天、发布日期可验证的帖子。
- 标题、正文、tags 与图片 OCR 文字共同参与板块、股票和情绪分析。
- 图片先做低分辨率文字检测，只 OCR 主体区域含文字的图片；一篇帖子的图片合并后只识别一次。
- 每个板块展示当天明确提及的热门股票，最多 10 只，不用零提及股票凑数。
- 点击板块或股票，可查看互动量最高的 3 篇帖子及其图片。
- 手动刷新、15 分钟账号冷却、请求串行、异常即停且不自动重试。
- 每次成功抓取都会生成本地、不可覆盖的历史预测快照，用于和后续交易日行情做方向验证。
- 可选 Codex、Claude Code 或 OpenAI 兼容 API（包括 DeepSeek）做语义分析；失败时自动回退本地规则。

## 数据与模型分工

```text
部署者自己的 Chrome / 小红书登录态
                ↓
Agent Reach → OpenCLI（只读采集）
                ↓
标题 + 正文 + tags + 图片 OCR
                ↓
Codex / Claude Code / DeepSeek（可选语义分析）
                ↓
本地看板与本地缓存
```

模型不负责绕过登录或直接读取 Cookie。Agent Reach/OpenCLI 负责只读采集；模型只接收去掉作者、URL 和账号信息后的公开帖子文本。

## 一键安装

要求：Python 3.10+、Chrome、Node.js 20+。

### Windows

```powershell
git clone https://github.com/Doris445/xiaohongshu-stock-hotspot.git
cd xiaohongshu-stock-hotspot
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

### macOS / Linux

```bash
git clone "https://github.com/Doris445/xiaohongshu-stock-hotspot.git"
cd xiaohongshu-stock-hotspot
bash scripts/setup.sh
```

安装脚本会在项目内创建 `.venv`，安装锁定版本的 Agent Reach 与 Scrapling，并安装 OpenCLI 渠道。部署者不需要分别克隆这两个项目。

## 配置自己的小红书账号

1. 在自己的 Chrome 中登录自己明确控制的小红书账号。
2. 按 Agent Reach/OpenCLI 的安装提示启用浏览器扩展。
3. 在项目虚拟环境中检查状态：

```powershell
.\.venv\Scripts\agent-reach.exe doctor --json
```

macOS/Linux：

```bash
.venv/bin/agent-reach doctor --json
```

项目不会替你登录、导出 Cookie、读取浏览器密码或把登录态写进仓库。没有可用登录态时，看板只显示合成演示数据。

## 配置分析模型

复制 `.env.example` 为 `.env`。`.env` 已被 Git 忽略。

### 自动模式

```env
SENTIBOARD_LLM_PROVIDER=auto
```

- 在 Codex 环境中优先调用 Codex CLI（ChatGPT）。
- 在 Claude Code 环境中优先调用 Claude CLI。
- 两者都不可用时，若配置了 API Key 则使用 OpenAI 兼容 API，否则回退本地关键词规则。

也可以明确指定：`codex`、`claude`、`openai-compatible` 或 `local-keywords`。

### DeepSeek

```env
SENTIBOARD_LLM_PROVIDER=openai-compatible
SENTIBOARD_LLM_API_BASE=https://api.deepseek.com
SENTIBOARD_LLM_MODEL=deepseek-chat
SENTIBOARD_LLM_API_KEY=你的密钥
```

其他兼容 `/chat/completions` 的服务只需替换 API Base、模型名和密钥。

## 启动

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

macOS/Linux：

```bash
bash scripts/start.sh
```

浏览器打开 [http://127.0.0.1:8768](http://127.0.0.1:8768)。点击右上角刷新按钮后才会采集，页面不会实时轮询小红书。

## 局域网或服务器暴露

默认只监听 `127.0.0.1`。如果要监听非回环地址，必须先配置刷新令牌：

```env
SENTIBOARD_REFRESH_TOKEN=请生成一个足够长的随机值
```

前端会要求输入令牌后才能触发刷新。公开部署还应使用 HTTPS、反向代理和访问控制。由于 OpenCLI 依赖部署者自己的桌面 Chrome 登录态，小红书采集推荐在本地桌面运行，而不是无人值守的公网服务器。

## 依赖策略

- [Agent Reach](https://github.com/Panniantong/agent-reach) 以固定版本的 Python 依赖安装，MIT License。
- [Scrapling](https://github.com/D4Vinci/Scrapling) 以固定版本的 Python 依赖安装，BSD-3-Clause License。
- Windows 默认使用系统 OCR；其他系统可安装 `.[ocr]` 启用 RapidOCR。

采用依赖安装而非复制上游源码，仓库更小、许可证边界清晰，也更容易升级和获取安全修复。详见 `THIRD_PARTY_NOTICES.md`。

## 隐私

真实帖子缓存、作者、帖子 URL、OCR 结果、日志、API 密钥和所有登录信息都不会进入版本控制。发布或提交前请运行：

```powershell
python scripts/privacy_check.py
```

更多说明见 [PRIVACY.md](PRIVACY.md) 与 [SECURITY.md](SECURITY.md)。

历史验证数据保存在 `data/history/`，同样不会进入 Git。`GET /api/history` 只返回日期、样本数和哈希；`GET /api/history?date=YYYY-MM-DD` 返回预测摘要，不会返回原始帖子、作者或签名 URL。方向验证默认将绝对涨跌幅小于 `0.3%` 视为横盘，不计入命中率。

## 测试

```powershell
$env:STOCK_DASHBOARD_DISABLE_NETWORK='1'
python -m unittest -v test_dashboard.py
```

## License

MIT
