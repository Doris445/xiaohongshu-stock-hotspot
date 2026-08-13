# 数据源与接口说明

本文档供部署者和 AI 编程助手理解 SentiBoard 的只读数据链路。上游接口均由部署者本机直接访问；仓库不包含账号、Cookie、缓存或任何私有代理。

## 东方财富：板块与股票池

| 用途 | 公开入口 | 排序与字段 |
| --- | --- | --- |
| 当日热门板块 | `https://push2delay.eastmoney.com/api/qt/clist/get` | `fid=f6` 按成交额排序；`fs=m:90+t:2+f:!50`；读取 `f12` 板块代码、`f14` 名称、`f3` 涨跌幅、`f6` 成交额、`f8` 换手率 |
| 板块内活跃股 | 同一 `clist/get` 入口 | `fs=b:{板块代码}`、`fid=f6`；读取 `f12` 股票代码、`f14` 名称、`f2` 价格、`f3/f6/f8` |
| 板块快照验证 | `https://push2delay.eastmoney.com/api/qt/ulist.np/get` | 使用 `secids=90.{板块代码}` 读取板块涨跌幅 |

实现位置：`eastmoney_guba.py` 的 `SECTOR_RANK_URL` / `SECTOR_STOCK_URL`，以及 `providers.py` 的 `EastmoneySectorConstituentProvider` / `EastmoneySectorQuoteProvider`。

热门板块和板块内股票都按刷新时点的**当日成交额**排序，不用涨跌幅冒充讨论热度。随后才对这 10 × 10 个股票槽位读取股吧数据。

## 东方财富股吧：列表、正文和帖主回复

| 用途 | 公开入口 | 采集规则 |
| --- | --- | --- |
| 股票股吧首页 | `https://guba.eastmoney.com/list,{股票代码}.html` | 按“最新发帖”逐页读取；第 N 页为 `list,{代码}_{N}.html` |
| 普通股吧正文 | 列表页返回的 `https://guba.eastmoney.com/news,...html` | 仅在用户点进股票时读取预筛候选；遇到身份核验即停止自动读取 |
| 财富号文章 | 列表页返回的 `https://caifuhao.eastmoney.com/news/...` | 解析公开正文、原始发布时间、图片、作者和股票标签 |
| 只看楼主回复 | `https://gbapi.eastmoney.com/reply/api/Reply/ArticleNewAuthorOnly` | 参数包括 `postid`、`manageruid`、`p`、`ps=30`；最多 10 页，只保留回复用户等于原作者且包含分析证据的内容 |

时间窗固定为北京时间当天 `06:00:00` 到用户点击刷新时刻。程序逐页扫描，直到整页越过 06:00；触及每股页数上限或页面失败会标为“部分”，不会冒充全量数据。

刷新阶段只保存列表元数据，避免一次下载上千篇正文。用户点击单股后，系统先按标题中的股票名称/代码和分析词预筛最多 24 篇，再读取正文并执行文章门控。最终最多展示 10 篇，排序分数为：

```text
浏览量 + 点赞量 × 8 + 评论量 × 12
```

普通读者回复不会进入语义总结。仅帖主自己的分析性补充回复可作为证据，图片也随帖子或帖主回复展示。

## 腾讯行情

股票和指数验证使用腾讯公开行情入口：

```text
https://qt.gtimg.cn/q={以逗号分隔的市场代码}
```

市场代码示例：上交所 `sh600000`、深交所 `sz000001`、北交所 `bj830000`。接口解析和日期校验在 `providers.py` 的 `TencentQuoteProvider`。

## 小红书链路

小红书不是由仓库内脚本直接模拟登录：

```text
部署者自己的 Chrome 登录态
  → Agent Reach v1.5.0
  → OpenCLI xiaohongshu 只读命令
  → 标题/正文/tags/图片 URL
  → 本地文字图片预筛与 OCR
  → 本地缓存和看板
```

项目不读取、导出或提交 Chrome Cookie。Agent Reach 从官方 GitHub `v1.5.0` 标签安装，避免误装 PyPI 上同名的无关包。

## 本地看板 API

服务默认只监听 `127.0.0.1:8768`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务存活检查 |
| `GET` | `/api/dashboard` | 今天的小红书看板 |
| `GET` | `/api/dashboard?date=YYYY-MM-DD` | 从本地私有归档重建指定日期看板 |
| `POST` | `/api/refresh` | 手动刷新小红书数据 |
| `GET` | `/api/eastmoney` | 东方财富当前缓存与 10 × 10 层级 |
| `POST` | `/api/eastmoney/refresh` | 建立当日 06:00 至当前时刻的数据窗口 |
| `POST` | `/api/eastmoney/stock?code=600000` | 按需读取单股候选正文、图片和帖主回复 |
| `GET` | `/api/eastmoney/session` | 查询独立人工核验会话状态 |
| `POST` | `/api/eastmoney/session/open` | 打开项目专属 Chrome 窗口，由部署者人工完成滑块 |
| `GET` | `/api/history` | 历史日期索引 |
| `POST` | `/api/history/validate?date=YYYY-MM-DD` | 连接真实午盘行情做方向验证 |

若服务监听非回环地址，必须在 `.env` 设置 `SENTIBOARD_REFRESH_TOKEN`，所有 `POST` 请求通过 `X-Refresh-Token` 提供令牌。

## 缓存与安全边界

- 东方财富层级缓存：`data/eastmoney_cache.json`
- 单股详情缓存：`data/eastmoney_stock_details.json`
- 项目专属人工核验会话：`data/eastmoney_browser_profile/`
- 小红书当日缓存：`data/xhs_samples.json`
- 历史私有快照：`data/history/`

以上目录全部被 Git 忽略。新一天第一次刷新东方财富时会删除前一日层级和详情缓存，但保留独立人工核验会话。遇到 CAPTCHA、429、身份核验或平台风险提示时，程序停止且不自动重试；不提供验证码破解或 Cookie 复制功能。
