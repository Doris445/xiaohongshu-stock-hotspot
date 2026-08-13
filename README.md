# 小红书股票热点看盘 · SentiBoard

SentiBoard 是一个本地优先、手动刷新的 A 股社交情绪看板。它聚合当天的小红书公开帖子与当日行情，展示热门板块、板块情绪、热门股票 Top 10，以及互动量最高的帖子和配图。

> 本项目只做信息整理，不构成投资建议。小红书自动化访问存在平台风控风险，请使用自己明确控制的账号并保持低频。

## 功能

- 严格只纳入北京时间当天、发布日期可验证的帖子。
- 标题用于热度发现；只有正文/tags（A 级）或图片 OCR 文字（B 级）参与情绪方向判断。仅标题为 C 级，不输出看多/看空。
- 图片先做低分辨率文字检测，只 OCR 主体区域含文字的图片；一篇帖子的图片合并后只识别一次。
- 每个板块展示当天明确提及的热门股票，最多 10 只，不用零提及股票凑数。
- 点击板块或股票，可查看互动量最高的 3 篇帖子及其图片。
- 手动刷新、15 分钟账号冷却、请求串行、异常即停且不自动重试；未连接后端不会误占冷却，也不会清空既有缓存。
- 每次先完成 6 个板块的全局搜索，再优先为每个板块选择 1 篇尚无正文的帖子补详情；后续刷新逐批扩大证据，不重复读取已经补全的同一批。
- 1 篇 A/B 证据显示“线索”，2 篇独立作者显示“初步”，至少 3 篇且来自 2 位作者才升级为正式方向；单篇爆款权重设有上限。
- 评论正文不采集；评论数未补全时显示 `--`，不会伪装成 0。
- 热门股票 Top 10 从东方财富板块成分中按当日帖子提及动态生成，不再受固定股票池限制。
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

历史验证数据保存在 `data/history/`，同样不会进入 Git。`GET /api/history` 只返回日期、样本数和哈希；`GET /api/history?date=YYYY-MM-DD` 返回预测摘要和已保存的验证结果，不会返回原始帖子、作者或签名 URL。午盘结束后可调用 `POST /api/history/validate?date=YYYY-MM-DD`，指数/个股来自腾讯、板块来自东方财富；绝对涨跌幅小于 `0.3%` 视为横盘，不计入方向一致率。

验证严格使用 11:30 前最后一份有方向的冻结快照。若 11:30 前仍无可判定方向、午盘后才出现情绪，界面只显示“同日方向一致率”并标注时间穿越风险，不把它计作预测成绩。13:00 后不会用下午实时行情覆盖午盘快照。

看板顶部的日历按钮支持直接选择年、月、日。选择已有归档的日期后，`GET /api/dashboard?date=YYYY-MM-DD` 会在服务端使用该日私有归档重建完整只读看板，包括板块、股票、情绪、Top 3 帖子与帖子图片；点击“返回今天”恢复当天看板。没有归档的日期会返回 404，不会用今天、演示数据或相邻日期补齐。

## 测试

```powershell
$env:STOCK_DASHBOARD_DISABLE_NETWORK='1'
python -m unittest -v test_dashboard.py
```

## License

MIT
