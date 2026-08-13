# 用 Codex / Claude Code 一键部署

仓库根目录的 `AGENTS.md` 和 `CLAUDE.md` 是给 AI 编程助手的执行约束。推荐让 AI 在**部署者自己的桌面电脑**上操作，因为小红书和东方财富人工核验都依赖本机 Chrome。

## 复制给 Codex

```text
请部署这个仓库。完整读取并遵守 AGENTS.md：不要读取、打印或提交任何 Cookie、Chrome profile、.env、API key 或 data 缓存。Windows 运行 scripts/setup.ps1，macOS/Linux 运行 scripts/setup.sh；安装中断时直接重跑，不要删除 .venv。用 .venv 的 Python 运行 scripts/doctor.py --json 和离线测试，再通过 scripts/start.ps1 或 scripts/start.sh 启动。轮询 /api/health 成功后，把本地浏览链接给我。需要登录小红书或完成东方财富滑块时，打开窗口让我本人操作，不要尝试绕过。
```

Codex 部署后应在本机 `.env` 使用：

```env
SENTIBOARD_LLM_PROVIDER=codex
```

## 复制给 Claude Code

```text
请部署这个仓库。完整读取并遵守 CLAUDE.md：不要读取、打印或提交任何 Cookie、Chrome profile、.env、API key 或 data 缓存。Windows 运行 scripts/setup.ps1，macOS/Linux 运行 scripts/setup.sh；安装中断时直接重跑，不要删除 .venv。用 .venv 的 Python 运行 scripts/doctor.py --json 和离线测试，再通过 scripts/start.ps1 或 scripts/start.sh 启动。轮询 /api/health 成功后，把本地浏览链接给我。需要登录小红书或完成东方财富滑块时，打开窗口让我本人操作，不要尝试绕过。
```

Claude Code 部署后应在本机 `.env` 使用：

```env
SENTIBOARD_LLM_PROVIDER=claude
```

## AI 应执行的确定性流程

1. 确认 Python 为 64 位 3.10–3.13；Windows 不要信任 `WindowsApps/python.exe` 占位符。
2. 执行平台对应的 `scripts/setup.*`。脚本分阶段输出且可重复运行。
3. 只修改本机 `.env`，不得把它加入 Git。
4. 运行 `.venv` 中的 Python：`scripts/doctor.py --json`。
5. 设置 `STOCK_DASHBOARD_DISABLE_NETWORK=1` 后运行 `python -m unittest -v test_dashboard.py`。
6. 通过 `scripts/start.*` 启动，而不是使用全局 Python 直接运行 `server.py`。
7. 请求 `http://127.0.0.1:8768/api/health`，收到 `{"ok": true}` 才宣告部署成功。
8. 提示部署者在自己的 Chrome 登录小红书；登录缺失属于待配置状态，不是安装失败。
9. 东方财富普通正文出现核验时，点击页面“解锁正文”，等待部署者本人完成一次滑块。

## 可恢复安装

- 默认安装包含完整看板核心和 Agent Reach/OpenCLI 小红书连接器。
- Scrapling fetchers 是可选 HTTP 加速层，不影响默认看板功能。Windows 传 `-WithScrapling`；macOS/Linux 设置 `SENTIBOARD_WITH_SCRAPLING=1`。
- 若只需快速验收 UI/东方财富，可临时跳过小红书：Windows 传 `-SkipXiaohongshu`；macOS/Linux 设置 `SENTIBOARD_SKIP_XIAOHONGSHU=1`。随后重跑默认脚本补齐即可。
- pip 下载或 Agent Reach 安装中断时保留 `.venv`，直接重跑；依赖缓存会被复用。
