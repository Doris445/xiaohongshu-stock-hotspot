const state = {
  data: null,
  source: "xhs",
  eastmoneyData: null,
  eastmoneySession: null,
  activeEastmoneySector: null,
  filter: "全部",
  lastFocus: null,
  refreshTokenRequired: false,
  selectedDate: null,
  availableDates: [],
  dateLoading: false,
};
const $ = (selector) => document.querySelector(selector);
const fmt = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });

function sentimentClass(value) {
  const text = String(value || "");
  return text.includes("看多") ? "bull" : text.includes("看空") ? "bear" : "neutral";
}

function number(value) {
  return fmt.format(Number(value || 0));
}

function availableNumber(value, available = value !== null && value !== undefined) {
  return available ? number(value) : "--";
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function excerpt(value = "", limit = 240) {
  const normalized = String(value).replace(/\s+/g, " ").trim();
  return normalized.length > limit ? `${normalized.slice(0, limit)}…` : normalized;
}

function localIsoDate(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function humanDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? `${match[1]}年${Number(match[2])}月${Number(match[3])}日` : value;
}

function viewLabel() {
  return state.data?.meta?.mode === "history" ? humanDate(state.data.meta.dataDate) : "今日";
}

async function loadDashboard(selectedDate = null) {
  const today = localIsoDate();
  const normalizedDate = selectedDate && selectedDate !== today ? selectedDate : null;
  const endpoint = normalizedDate ? `/api/dashboard?date=${encodeURIComponent(normalizedDate)}` : "/api/dashboard";
  const [response, configResponse, historyResponse] = await Promise.all([
    fetch(endpoint, { cache: "no-store" }),
    fetch("/api/config", { cache: "no-store" }),
    fetch("/api/history", { cache: "no-store" }),
  ]);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || "无法读取看板数据");
  }
  state.data = await response.json();
  state.selectedDate = normalizedDate;
  if (configResponse.ok) {
    const config = await configResponse.json();
    state.refreshTokenRequired = Boolean(config.refreshTokenRequired);
  }
  if (historyResponse.ok) {
    const history = await historyResponse.json();
    state.availableDates = Array.isArray(history.dates) ? history.dates : [];
  }
  render();
  renderDatePicker();
}

async function loadEastmoney() {
  const [response, sessionResponse] = await Promise.all([
    fetch("/api/eastmoney", { cache: "no-store" }),
    fetch("/api/eastmoney/session", { cache: "no-store" }),
  ]);
  if (!response.ok) throw new Error("无法读取东方财富社区看板");
  state.eastmoneyData = await response.json();
  if (sessionResponse.ok) state.eastmoneySession = await sessionResponse.json();
  if (!state.activeEastmoneySector) {
    state.activeEastmoneySector = state.eastmoneyData.sectors?.[0]?.code || null;
  }
  render();
}

async function switchSource(source) {
  if (!['xhs', 'eastmoney'].includes(source) || state.source === source) return;
  state.source = source;
  state.filter = "全部";
  document.querySelectorAll(".source-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.source === source);
  });
  closeDatePicker();
  if (source === "eastmoney" && !state.eastmoneyData) {
    showToast("正在读取东方财富社区缓存…");
    await loadEastmoney();
  } else {
    render();
  }
}

function render() {
  document.body.dataset.source = state.source;
  if (state.source === "eastmoney") {
    renderEastmoney();
    return;
  }
  const { meta, summary, sectors, stocks } = state.data;
  const label = viewLabel();
  const isHistory = meta.mode === "history";
  $("#heroTitle").innerHTML = "市场在讨论什么，<br /><em>情绪正在往哪走。</em>";
  $("#heroDescription").textContent = "聚合公开社媒讨论与行情快照，快速识别 A 股热门板块、个股和情绪分歧。";
  $("#sectorKicker").textContent = "SECTOR HEAT";
  $("#sectorSectionTitle").textContent = "热门板块";
  $("#sentimentFilter").hidden = false;
  $("#dateButton").hidden = false;
  $("#historyButton").hidden = false;
  $("#eastmoneyUnlockButton").hidden = true;
  $("#socialSource").textContent = "社媒来源：小红书公开内容";
  $("#qualityDateLabel").textContent = "日期有效";
  $("#qualityEvidenceLabel").textContent = "详情证据";
  $("#qualityEntitiesLabel").textContent = "可用信号";
  $("#qualityCommentsLabel").textContent = "评论覆盖";
  setStockHeaders("排名 / 股票", "所属板块", "最新价", "讨论热度", "帖子 / 评论", "情绪");
  renderMethodology("xhs");
  $("#updatedLabel").textContent = isHistory ? `快照 ${meta.updatedLabel || meta.dataDate}` : `截止 ${meta.updatedLabel}`;
  $("#windowLabel").textContent = label;
  $("#modeBadge").textContent = meta.modeLabel;
  $("#modeBadge").classList.toggle("live", meta.mode === "live" || meta.mode === "mixed");
  $("#sourceText").textContent = isHistory
    ? `${humanDate(meta.dataDate)} 本地历史归档 · 只读快照`
    : meta.xhsBackend === "OpenCLI" && ["ready", "ok"].includes(meta.xhsStatus)
    ? "OpenCLI 已连接 · 登录态将在刷新时验证"
    : meta.mode === "live" || meta.mode === "mixed"
    ? (meta.xhsMessage || "Agent Reach 已连接小红书公开内容")
    : (meta.xhsMessage || "今天尚未采集到发布日期可验证的帖子");
  $("#sampleScope").textContent = `北京时间 ${meta.dataDate || "今日"} · 去重 ${meta.samplePoolSize || 0} 帖 · ${meta.sampledEntityCount || 0}/${sectors.length + stocks.length} 实体已覆盖`;
  $("#postsMetricLabel").textContent = `${label}去重帖子`;
  $("#commentsMetricLabel").textContent = `${label}帖子评论`;
  $("#bullMetricNote").textContent = `${label}已覆盖股票`;
  $("#stockSectionTitle").textContent = `${label}股票讨论监控池`;
  $("#sectorSectionDescription").textContent = `点击板块，查看${label}热门股票 Top 10 与高互动帖子`;
  $("#stockSectionDescription").textContent = `点击任意一行，查看${label}互动量最高的三篇帖子`;
  $("#marketSource").textContent = `行情来源：${meta.marketSource}`;
  $("#dateButtonLabel").textContent = isHistory ? humanDate(meta.dataDate) : "选择日期";
  $("#dateButton").setAttribute("aria-label", isHistory ? `选择看板日期，当前为${humanDate(meta.dataDate)}` : "选择看板日期");
  $("#dateButton").classList.toggle("history-active", isHistory);
  $("#refreshButton").disabled = isHistory;
  const nextSafe = meta.nextSafeRefreshAt ? new Date(meta.nextSafeRefreshAt) : null;
  const cooling = nextSafe && nextSafe.getTime() > Date.now();
  $("#refreshButton").title = isHistory
    ? "历史快照为只读；返回今天后可刷新"
    : cooling
    ? `安全窗口将在 ${nextSafe.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 后开放；现在点击只读取缓存`
    : "刷新今天的数据";

  const sentimentReady = summary.sentimentStatus === "ready";
  const sentimentPreliminary = summary.sentimentStatus === "preliminary";
  const sentimentUsable = sentimentReady || sentimentPreliminary;
  const pulse = sentimentUsable ? Math.max(0, Math.min(100, 50 + summary.sentimentScore / 2)) : 50;
  $("#pulseScore").textContent = sentimentReady ? Math.round(pulse) : sentimentPreliminary ? `≈${Math.round(pulse)}` : "--";
  $("#pulseBar").style.left = `${pulse}%`;
  const pulseDirection = summary.sentimentScore > 15 ? "偏多" : summary.sentimentScore < -15 ? "偏空" : "中性";
  $("#pulseTrend").textContent = sentimentReady ? pulseDirection : sentimentPreliminary ? `初步${pulseDirection}` : "证据不足";
  $("#pulseTrend").className = `pulse-trend ${sentimentClass(sentimentUsable ? (summary.sentimentScore > 15 ? "看多" : summary.sentimentScore < -15 ? "看空" : "中性") : "样本不足")}`;
  $("#postsMetric").textContent = number(summary.posts);
  $("#commentsMetric").textContent = availableNumber(summary.comments);
  $("#commentsMetricNote").textContent = summary.comments === null || summary.comments === undefined ? "详情未补全，不显示为 0" : `已覆盖 ${number(summary.commentCoveragePct)}% 帖子`;
  $("#bullMetric").textContent = sentimentUsable ? `${summary.bullRatio}%` : "--";
  $("#bullMetricNote").textContent = sentimentReady ? `${label}高置信度股票` : sentimentPreliminary ? `${label}初步信号股票` : `${label}暂无可用信号`;
  renderDataQuality(meta.dataQuality || {});
  $("#sectorGrid").innerHTML = sectors.map(renderSector).join("");
  renderStocks(stocks);
  bindEntityButtons();
}

function setStockHeaders(name, sector, price, heat, posts, sentiment) {
  $("#stockHeadName").textContent = name;
  $("#stockHeadSector").textContent = sector;
  $("#stockHeadPrice").textContent = price;
  $("#stockHeadHeat").textContent = heat;
  $("#stockHeadPosts").textContent = posts;
  $("#stockHeadSentiment").textContent = sentiment;
}

function renderMethodology(source) {
  if (source === "eastmoney") {
    $("#methodTitle").textContent = "东方财富社区如何筛选技术分析";
    $("#methodSteps").innerHTML = `
      <div><b>01</b><p><strong>确定股票池</strong><span>按当日成交额选 10 个热门板块，每板块取 10 只活跃股。</span></p></div>
      <div><b>02</b><p><strong>完整时间窗</strong><span>每只股票按最新发帖分页，覆盖北京时间 06:00 至点击刷新时点；整页越过 06:00 才停止。</span></p></div>
      <div><b>03</b><p><strong>文章门控</strong><span>正文约 60 字以上、至少 3 个有效句/段、明确点名股票并含至少两类分析证据；一两句情绪帖直接剔除。</span></p></div>
      <div><b>04</b><p><strong>人工核验会话</strong><span>普通股吧需要身份核实时，由你在项目专属隔离窗口手动完成滑块；程序只复用该会话读取预筛后的正文和图片。</span></p></div>`;
    $("#methodCaveat").textContent = "不会自动破解滑块，也不会读取日常 Chrome 的 Cookie。普通读者评论只统计数量；仅对已入选文章保留帖主的有效补充分析。社区综合倾向不构成投资建议。";
    return;
  }
  $("#methodTitle").textContent = "这个看板怎么算“情绪”";
  $("#methodSteps").innerHTML = `
    <div><b>01</b><p><strong>采样</strong><span>按板块与股票关键词搜索公开笔记，仅纳入发布日期可验证为所选北京时间日期的内容。</span></p></div>
    <div><b>02</b><p><strong>证据</strong><span>A 级为正文或 tags，B 级为图片文字，C 级仅标题。C 级只计热度，不判断看多看空。</span></p></div>
    <div><b>03</b><p><strong>分层</strong><span>1 篇证据显示“线索”，2 篇独立作者显示“初步”；至少 3 篇且来自 2 位作者才升级为正式方向。</span></p></div>
    <div><b>04</b><p><strong>股票热度</strong><span>从板块成分股中动态识别股票；按当日提及帖数排序，同一篇帖子重复出现只计一次。</span></p></div>`;
  $("#methodCaveat").textContent = "帖子日期无法确认或不属于所选日期时，系统会留空而不沿用其他日期数据。历史行情不会伪装成当日实时行情。";
}

function formatAmount(value) {
  const amount = Number(value || 0);
  if (amount >= 1e8) return `${(amount / 1e8).toFixed(amount >= 1e10 ? 0 : 1)}亿`;
  if (amount >= 1e4) return `${(amount / 1e4).toFixed(0)}万`;
  return number(amount);
}

function renderEastmoney() {
  const data = state.eastmoneyData;
  if (!data) return;
  const { meta = {}, summary = {}, sectors = [] } = data;
  const completePct = summary.stocks ? Math.round(Number(summary.completeStocks || 0) / Number(summary.stocks) * 100) : 0;
  $("#heroTitle").innerHTML = "从板块到个股，<br /><em>只留下有依据的分析。</em>";
  $("#heroDescription").textContent = "覆盖 10 个热门板块与 100 只活跃股，逐页扫描 06:00 至刷新时点的全部发帖；详情只补全你点开的股票。";
  $("#updatedLabel").textContent = `截止 ${meta.updatedLabel || "尚未刷新"}`;
  $("#windowLabel").textContent = "06:00 至刷新";
  $("#modeBadge").textContent = meta.status === "ok" ? "社区扫描" : "等待刷新";
  $("#modeBadge").classList.toggle("live", meta.status === "ok");
  $("#sourceText").textContent = meta.message || "东方财富社区等待刷新";
  $("#sampleScope").textContent = `${meta.scope || "10 个板块 × 10 只股票"} · ${number(summary.pagesScanned)} 页 · ${number(summary.scannedPosts)} 帖`;
  $("#pulseScore").textContent = summary.stocks ? completePct : "--";
  $("#pulseBar").style.left = `${Math.max(0, Math.min(100, completePct))}%`;
  $("#pulseTrend").textContent = summary.stocks ? `${number(summary.completeStocks)}/${number(summary.stocks)} 完整` : "等待扫描";
  $("#pulseTrend").className = `pulse-trend ${completePct === 100 ? "bull" : "neutral"}`;
  $("#postsMetricLabel").textContent = "06:00 后帖子";
  $("#postsMetric").textContent = number(summary.scannedPosts);
  $("#commentsMetricLabel").textContent = "评论数量";
  $("#commentsMetric").textContent = number(summary.todayComments);
  $("#commentsMetricNote").textContent = "普通评论只计数；入选文章补充帖主回复";
  $("#bullMetricLabel").textContent = "疑似分析帖";
  $("#bullMetric").textContent = number(summary.candidatePosts);
  $("#bullMetricNote").textContent = "点入股票后用正文二次筛选";
  $("#qualityStrip").dataset.status = completePct === 100 ? "ready" : summary.stocks ? "insufficient" : "empty";
  $("#qualityLabel").textContent = completePct === 100 ? "时间窗完整" : summary.stocks ? "部分股票未越过 06:00" : "尚未扫描";
  $("#qualityMessage").textContent = "普通列表按最新发帖分页；详情页再校验原始发布时间";
  $("#qualityDateLabel").textContent = "时间起点";
  $("#qualityDate").textContent = "06:00";
  $("#qualityEvidenceLabel").textContent = "分析候选";
  $("#qualityEvidence").textContent = `${number(summary.candidatePosts)} / ${number(summary.scannedPosts)}`;
  $("#qualityEntitiesLabel").textContent = "完整股票";
  $("#qualityEntities").textContent = `${number(summary.completeStocks)} / ${number(summary.stocks)}`;
  $("#qualityCommentsLabel").textContent = "已扫描页";
  $("#qualityComments").textContent = number(summary.pagesScanned);
  $("#sectorKicker").textContent = "MARKET-ACTIVE SECTORS";
  $("#sectorSectionTitle").textContent = "今日热门板块 Top 10";
  $("#sectorSectionDescription").textContent = "按当日成交额排序；点击板块查看其中 10 只活跃股";
  $("#sectorGrid").innerHTML = sectors.map(renderEastmoneySector).join("") || `<div class="eastmoney-empty">点击右上角“刷新数据”开始建立 06:00 后的社区样本。</div>`;
  $("#sentimentFilter").hidden = true;
  $("#dateButton").hidden = true;
  $("#historyButton").hidden = true;
  $("#marketSource").textContent = "热度来源：东方财富延迟行情成交额";
  $("#socialSource").textContent = "社区来源：东方财富股吧公开页面";
  setStockHeaders("排名 / 股票", "所属板块", "最新价", "成交额 / 换手", "帖子 / 分析候选", "时间窗");
  renderMethodology("eastmoney");
  bindEastmoneySectorButtons();
  renderEastmoneyStocks();
  const nextSafe = meta.nextSafeRefreshAt ? new Date(meta.nextSafeRefreshAt) : null;
  $("#refreshButton").disabled = false;
  $("#refreshButton").title = nextSafe && nextSafe > new Date() ? `大批量扫描冷却至 ${nextSafe.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : "扫描 06:00 至当前时点";
  renderEastmoneySession();
}

function renderEastmoneySession() {
  const button = $("#eastmoneyUnlockButton");
  button.hidden = state.source !== "eastmoney";
  const status = state.eastmoneySession?.status || "offline";
  button.dataset.status = status;
  const labels = {
    ready: "正文已解锁",
    verification_required: "检查核验",
    waiting: "检查核验",
    unavailable: "浏览器不可用",
    offline: "解锁正文",
  };
  button.querySelector(".button-label").textContent = labels[status] || "解锁正文";
  button.title = state.eastmoneySession?.message || "使用项目专属隔离浏览器人工核验一次";
}

function requestHeaders() {
  const headers = {};
  if (!state.refreshTokenRequired) return headers;
  let token = sessionStorage.getItem("sentiboardRefreshToken") || "";
  if (!token) token = window.prompt("请输入看板刷新令牌") || "";
  if (!token) throw new Error("未提供刷新令牌");
  headers["X-Refresh-Token"] = token;
  sessionStorage.setItem("sentiboardRefreshToken", token);
  return headers;
}

async function unlockEastmoneySession() {
  const button = $("#eastmoneyUnlockButton");
  button.disabled = true;
  button.classList.add("loading");
  try {
    const current = state.eastmoneySession?.status || "offline";
    const shouldOpen = ["offline", "unavailable"].includes(current);
    const response = await fetch(
      shouldOpen ? "/api/eastmoney/session/open" : "/api/eastmoney/session",
      shouldOpen ? { method: "POST", headers: requestHeaders() } : { cache: "no-store" },
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || "隔离浏览器操作失败");
    state.eastmoneySession = payload;
    renderEastmoneySession();
    if (payload.status === "ready") {
      showToast("人工核验已通过；普通股吧正文将使用隔离会话低频读取", 5200);
    } else if (shouldOpen) {
      showToast("隔离窗口已打开。请手动完成滑块，再点击“检查核验”", 7000);
    } else {
      showToast(payload.message || "尚未通过核验，请在隔离窗口完成滑块", 5200);
    }
  } catch (error) {
    showToast(`解锁失败：${error.message}`, 5200);
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
}

function renderEastmoneySector(sector) {
  const active = sector.code === state.activeEastmoneySector;
  const change = Number(sector.changePct || 0);
  return `<article class="sector-card eastmoney-sector ${active ? "selected" : ""}" tabindex="0" role="button" data-sector-code="${escapeHtml(sector.code)}">
    <div class="sector-top"><div><span class="rank">${String(sector.rank).padStart(2, "0")}</span><h3>${escapeHtml(sector.name)}</h3></div><span class="sentiment ${change > 0 ? "bull" : change < 0 ? "bear" : "neutral"}">${change > 0 ? "+" : ""}${change.toFixed(2)}%</span></div>
    <div class="sector-stats"><div class="sector-stat"><span>成交额</span><b>${formatAmount(sector.amount)}</b></div><div class="sector-stat"><span>06:00 后帖子</span><b>${number(sector.scannedPosts)}</b></div></div>
    <div class="sector-foot"><span class="keywords">${number(sector.candidatePosts)} 篇疑似分析 · ${number(sector.completeStocks)}/10 股票完整</span><span class="view-arrow ui-icon" aria-hidden="true">&#xE8A7;</span></div>
  </article>`;
}

function bindEastmoneySectorButtons() {
  document.querySelectorAll(".eastmoney-sector").forEach((card) => {
    const select = () => {
      state.activeEastmoneySector = card.dataset.sectorCode;
      document.querySelectorAll(".eastmoney-sector").forEach((item) => item.classList.toggle("selected", item === card));
      renderEastmoneyStocks();
      $(".stock-section").scrollIntoView({ behavior: "smooth", block: "start" });
    };
    card.addEventListener("click", select);
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
  });
}

function renderEastmoneyStocks() {
  const sectors = state.eastmoneyData?.sectors || [];
  const sector = sectors.find((item) => item.code === state.activeEastmoneySector) || sectors[0];
  const stocks = sector?.stocks || [];
  $("#stockSectionTitle").textContent = sector ? `${sector.name} · 活跃股票 Top 10` : "活跃股票 Top 10";
  $("#stockSectionDescription").textContent = "点击股票，按需补全 06:00 后技术分析帖正文和图片，并生成社区观点综合倾向";
  $("#stockTable").innerHTML = stocks.length ? stocks.map((stock) => {
    const change = Number(stock.changePct || 0);
    return `<tr class="eastmoney-stock-row" tabindex="0" data-stock-code="${escapeHtml(stock.code)}">
      <td><div class="stock-name-cell"><span class="table-rank">${String(stock.rank).padStart(2, "0")}</span><span class="stock-name"><b>${escapeHtml(stock.name)}</b><small>${escapeHtml(stock.code)} · ${escapeHtml(stock.sector)}</small></span></div></td>
      <td>${escapeHtml(stock.sector)}</td>
      <td><span class="price"><b>¥${Number(stock.price || 0).toFixed(2)}</b><span class="${change > 0 ? "positive" : change < 0 ? "negative" : ""}">${change > 0 ? "+" : ""}${change.toFixed(2)}%</span></span></td>
      <td><span class="activity-metric"><b>${formatAmount(stock.amount)}</b><small>${Number(stock.turnoverRate || 0).toFixed(2)}% 换手</small></span></td>
      <td class="counts">${number(stock.scannedPosts)} / ${number(stock.candidatePosts)}</td>
      <td><span class="window-status ${stock.windowComplete ? "complete" : "partial"}">${stock.windowComplete ? "完整" : "部分"}</span></td>
      <td><span class="row-arrow ui-icon" aria-hidden="true">&#xE8A7;</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="empty-row">该板块尚未建立股票样本</td></tr>`;
  document.querySelectorAll(".eastmoney-stock-row").forEach((row) => {
    const open = () => openEastmoneyStock(row.dataset.stockCode, row);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });
}

async function openEastmoneyStock(code, trigger) {
  const stock = (state.eastmoneyData?.sectors || []).flatMap((sector) => sector.stocks || []).find((item) => item.code === code);
  if (!stock) return;
  state.lastFocus = trigger;
  $("#drawerKicker").textContent = `${escapeHtml(code)} · TECHNICAL TOP 10`;
  $("#drawerTitle").textContent = stock.name;
  $("#drawerSubtitle").textContent = "正在补全技术分析帖正文、图片与精确发布时间…";
  $("#drawerContent").innerHTML = `<div class="drawer-loading"><span class="ui-icon">&#xE72C;</span><p>首次读取该股票会逐篇校验候选帖，完成后将使用本地缓存。</p></div>`;
  $("#drawerNote").textContent = "检查该股时间窗内全部用户帖；普通读者评论不读取，只保留入选文章中原作者的分析型回复。";
  $("#overlay").hidden = false;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  $("#closeDrawer").focus();
  try {
    const headers = {};
    if (state.refreshTokenRequired) {
      const token = sessionStorage.getItem("sentiboardRefreshToken") || window.prompt("请输入看板刷新令牌") || "";
      if (!token) throw new Error("未提供刷新令牌");
      headers["X-Refresh-Token"] = token;
      sessionStorage.setItem("sentiboardRefreshToken", token);
    }
    const response = await fetch(`/api/eastmoney/stock?code=${encodeURIComponent(code)}`, { method: "POST", headers });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || "详情读取失败");
    if (payload.meta?.verificationRequired) {
      state.eastmoneySession = { status: "verification_required", message: "东方财富要求重新人工核验" };
      renderEastmoneySession();
      showToast("隔离会话已失效，请点击“检查核验”并在窗口中重新完成滑块", 6200);
    }
    renderEastmoneyStockDetail(payload);
  } catch (error) {
    $("#drawerContent").innerHTML = `<div class="drawer-empty"><h3>详情读取失败</h3><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function renderEastmoneyStockDetail(data) {
  const view = data.communityView || {};
  const posts = data.posts || [];
  $("#drawerSubtitle").textContent = `${data.meta?.dataDate || "今日"} 06:00 后 · ${number(data.quality?.scanned)} 帖扫描 · ${number(posts.length)} 篇入选`;
  const risks = (view.riskTriggers || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const authorPlans = (view.authorPlans || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const summary = `<section class="community-view ${sentimentClass(view.stance)}">
    <div class="community-view-head"><span>社区综合倾向</span><strong>${escapeHtml(view.stance || "证据不足")}</strong><em>置信度 ${number(view.confidence)}%</em></div>
    <h3>${escapeHtml(view.summary || "")}</h3>
    <div class="view-decisions"><div><span>互动加权买卖倾向</span><b>${escapeHtml(view.tradeTendency || "证据不足")}</b></div><div><span>后续走势判断</span><b>${escapeHtml(view.outlook || "证据不足")}</b></div></div>
    <p>${escapeHtml(view.observationPlan || "")}</p>
    <div class="evidence-tags">${(view.commonEvidence || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
    ${authorPlans ? `<div class="author-plan-summary"><b>帖主给出的代表性动作/条件</b><ul>${authorPlans}</ul></div>` : ""}
    <ul>${risks}</ul><small>${escapeHtml(view.disclaimer || "")}</small>
  </section>`;
  const cards = posts.length ? posts.map(renderGubaPost).join("") : `<div class="drawer-empty"><h3>没有足够的技术分析帖</h3><p>系统已过滤辱骂、喊单、引流、非当天帖子及证据不足内容。</p></div>`;
  $("#drawerContent").innerHTML = `${summary}<section class="drawer-section"><div class="drawer-section-head"><div><span>TOP 10 POSTS</span><h3>互动综合最高的技术分析</h3></div><small>浏览 + 点赞×8 + 评论×12</small></div>${cards}</section>`;
  bindPostImages();
}

function renderGubaPost(post) {
  const images = (post.images || []).filter(Boolean).slice(0, 12);
  const gallery = images.length ? `<div class="post-gallery gallery-${Math.min(images.length, 4)}">${images.map((url, index) => `<button class="post-image" type="button" data-image-url="${escapeHtml(url)}" data-image-label="${escapeHtml(post.title)} · 图片 ${index + 1}"><img src="${escapeHtml(url)}" alt="${escapeHtml(post.title)}，图片 ${index + 1}" loading="lazy" referrerpolicy="no-referrer" /></button>`).join("")}</div>` : "";
  const authorReplies = (post.authorReplies || []).map((reply, replyIndex) => {
    const replyImages = (reply.images || []).map((url, imageIndex) => `<button class="post-image" type="button" data-image-url="${escapeHtml(url)}" data-image-label="帖主回复 ${replyIndex + 1} · 图片 ${imageIndex + 1}"><img src="${escapeHtml(url)}" alt="帖主回复图片 ${imageIndex + 1}" loading="lazy" referrerpolicy="no-referrer" /></button>`).join("");
    return `<div class="author-reply"><div><b>帖主补充分析</b><span>${escapeHtml(reply.publishedAt)} · ${number(reply.likeCount)} 赞</span></div><p>${escapeHtml(reply.content)}</p>${replyImages ? `<div class="post-gallery">${replyImages}</div>` : ""}</div>`;
  }).join("");
  const guidance = post.guidance || {};
  const conditions = (guidance.conditions || []).slice(0, 3).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const guidanceBox = `<section class="post-guidance"><div><span>买卖倾向</span><b>${escapeHtml(guidance.tradeBias || "未明确")}</b></div><div><span>后续走势</span><b>${escapeHtml(guidance.outlook || "未明确")}</b></div>${conditions ? `<ul>${conditions}</ul>` : ""}</section>`;
  return `<article class="post-card guba-post-card">
    <div class="post-number"><span>TOP ${number(post.rank)} · <i class="evidence-badge level-${String(post.qualityTier || "b").toLowerCase()}">${number(post.analysisScore)} 分</i></span><span class="sentiment ${sentimentClass(post.sentiment)}">${escapeHtml(post.sentiment)}</span></div>
    <h3>${escapeHtml(post.title)}</h3><p class="guba-post-content">${escapeHtml(post.content || "")}</p>${gallery}${guidanceBox}
    <div class="evidence-tags">${(post.analysisReasons || []).map((reason) => `<span>${escapeHtml(reason)}</span>`).join("")}</div>
    <div class="post-meta"><span class="post-author"><b>${escapeHtml(post.author)}</b><span>${escapeHtml(post.publishedAt)}</span></span><span class="engagement"><span>浏览 ${number(post.readCount)}</span><span>赞 ${number(post.likeCount)}</span><span>评 ${number(post.commentCount)}</span></span></div>
    ${authorReplies ? `<section class="author-replies"><h4>原作者在评论区的有效补充</h4>${authorReplies}</section>` : ""}
    <a class="post-link" href="${escapeHtml(post.url)}" target="_blank" rel="noopener noreferrer"><span>在东方财富查看原帖</span><span class="ui-icon">&#xE8A7;</span></a>
  </article>`;
}

function renderDataQuality(quality) {
  const strip = $("#qualityStrip");
  strip.dataset.status = quality.status || "empty";
  $("#qualityLabel").textContent = quality.label || "质量未知";
  $("#qualityMessage").textContent = quality.message || "暂无采集质量信息";
  $("#qualityDate").textContent = `${number(quality.dateValidityPct)}%`;
  $("#qualityEvidence").textContent = `${number(quality.evidencePosts)} / ${number(quality.discoveredPosts)}`;
  $("#qualityEntities").textContent = `${number(Number(quality.qualifiedEntities || 0) + Number(quality.preliminaryEntities || 0))} / ${number(quality.totalEntities)}`;
  $("#qualityComments").textContent = `${number(quality.commentCoveragePct)}%`;
}

function renderSector(item) {
  const label = viewLabel();
  const barWidth = Math.max(8, Math.min(100, 50 + item.score / 2));
  const keywords = Array.isArray(item.keywords) ? item.keywords : [];
  return `
    <article class="sector-card entity-open ${sentimentClass(item.sentiment)}" tabindex="0" role="button" aria-label="查看${escapeHtml(item.name)}热门帖子" data-kind="sector" data-name="${escapeHtml(item.name)}">
      <div class="sector-top">
        <div><span class="rank">0${item.rank}</span><h3>${escapeHtml(item.name)}</h3></div>
        <div class="source-stack"><span class="entity-source ${item.dataSource === "live" ? "sampled" : "placeholder"}">${item.dataSource === "live" ? `${label}采样` : `${label}暂无`}</span><span class="sentiment ${sentimentClass(item.sentiment)}">${item.sentiment}</span></div>
      </div>
      <div class="heat-row"><div class="heat-bar"><i style="width:${barWidth}%"></i></div><span class="heat-score">${item.score > 0 ? "+" : ""}${item.score}</span></div>
      <div class="sector-stats">
        <div class="sector-stat"><span>帖子数量</span><b>${number(item.posts)}</b></div>
        <div class="sector-stat"><span>评论数量</span><b>${availableNumber(item.comments, item.commentsAvailable)}</b></div>
      </div>
      <div class="sector-foot"><span class="keywords">${keywords.map((k) => `#${escapeHtml(k)}`).join("  ") || `${label}暂无匹配标签`}</span><span class="view-arrow ui-icon" aria-hidden="true">&#xE8A7;</span></div>
    </article>`;
}

function renderStocks(stocks) {
  const label = viewLabel();
  const visible = state.filter === "全部" ? stocks : stocks.filter((stock) => String(stock.sentiment || "").endsWith(state.filter));
  const maxPosts = Math.max(...stocks.map((stock) => Number(stock.posts || 0)), 1);
  $("#stockTable").innerHTML = visible.length ? visible.map((stock) => {
    const hasQuote = stock.price !== null && stock.price !== undefined
      && stock.changePct !== null && stock.changePct !== undefined
      && Number.isFinite(Number(stock.price)) && Number.isFinite(Number(stock.changePct));
    const change = hasQuote ? Number(stock.changePct) : null;
    const changeClass = change === null ? "" : change >= 0 ? "positive" : "negative";
    const priceHtml = hasQuote
      ? `<b>¥${Number(stock.price).toFixed(2)}</b><span class="${changeClass}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</span>`
      : `<b>--</b><span>${label}暂无</span>`;
    const heatWidth = stock.posts ? Math.max(8, Math.min(100, (stock.posts / maxPosts) * 100)) : 0;
    return `<tr class="entity-open ${sentimentClass(stock.sentiment)} ${stock.dataSource === "live" ? "sampled-row" : "placeholder-row"}" tabindex="0" data-kind="stock" data-name="${escapeHtml(stock.name)}">
      <td><div class="stock-name-cell"><span class="table-rank">${String(stock.rank).padStart(2, "0")}</span><span class="stock-name"><b>${escapeHtml(stock.name)}</b><small>${stock.code} · ${escapeHtml(stock.sector)} · ${stock.dataSource === "live" ? `${label}采样` : `${label}暂无`}</small></span></div></td>
      <td>${escapeHtml(stock.sector)}</td>
      <td><span class="price">${priceHtml}</span></td>
      <td><div class="mini-heat ${stock.heatChange >= 0 ? "up" : "down"}"><span><i style="width:${heatWidth}%"></i></span><b class="${stock.heatChange >= 0 ? "positive" : "negative"}">${stock.heatChange >= 0 ? "+" : ""}${stock.heatChange}%</b></div></td>
      <td class="counts">${number(stock.posts)} / ${availableNumber(stock.comments, stock.commentsAvailable)}</td>
      <td><span class="sentiment ${sentimentClass(stock.sentiment)}">${stock.sentiment}</span></td>
      <td><span class="row-arrow ui-icon" aria-hidden="true">&#xE8A7;</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="empty-row">当前筛选下暂无股票</td></tr>`;
}

function bindEntityButtons() {
  document.querySelectorAll(".entity-open").forEach((element) => {
    element.addEventListener("click", () => openDrawer(element.dataset.kind, element.dataset.name, element));
    element.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDrawer(element.dataset.kind, element.dataset.name, element);
      }
    });
  });
}

function openDrawer(kind, name, trigger) {
  const collection = kind === "sector" ? state.data.sectors : state.data.stocks;
  const item = collection.find((entry) => entry.name === name);
  if (!item) return;
  state.lastFocus = trigger;
  $("#drawerNote").textContent = "热度按帖子覆盖统计；低样本方向明确标注为“线索/初步”，达到 3 篇 A/B 证据和 2 位作者后才升级为正式结论。";
  $("#drawerKicker").textContent = kind === "sector" ? "SECTOR · STOCK HEAT" : `${item.code} · TOP POSTS`;
  $("#drawerTitle").textContent = item.name;
  const topPosts = Array.isArray(item.topPosts) ? item.topPosts : [];
  const topStocks = Array.isArray(item.topStocks) ? item.topStocks : [];
  const label = viewLabel();
  $("#drawerSubtitle").textContent = kind === "sector"
    ? `${label}板块热门股票 ${topStocks.length}/10 · 证据帖 ${number(item.evidencePosts)} 篇 · ${item.sentiment} · 置信度 ${number(item.confidence)}%`
    : `${label}高互动帖子 ${Math.min(topPosts.length, 3)} 篇 · 证据帖 ${number(item.evidencePosts)} 篇 · ${item.sentiment} · 置信度 ${number(item.confidence)}%`;
  const postsHtml = topPosts.length
    ? topPosts.map((post, index) => renderPost(post, index)).join("")
    : `<div class="drawer-empty"><span class="ui-icon" aria-hidden="true">&#xE9D9;</span><h3>${label}暂无可验证帖子</h3><p>为保证数据口径准确，其他日期或发布日期不明确的帖子不会出现在这里。</p></div>`;
  $("#drawerContent").innerHTML = kind === "sector"
    ? `${renderSectorStockRanking(item)}<section class="drawer-section"><div class="drawer-section-head"><div><span>TOP POSTS</span><h3>高互动帖子</h3></div><small>点赞数 + 评论数</small></div>${postsHtml}</section>`
    : postsHtml;
  bindPostImages();
  $("#overlay").hidden = false;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  $("#closeDrawer").focus();
}

async function openHistory(trigger) {
  state.lastFocus = trigger;
  trigger.disabled = true;
  try {
    const indexResponse = await fetch("/api/history", { cache: "no-store" });
    if (!indexResponse.ok) throw new Error("无法读取历史归档");
    const index = await indexResponse.json();
    const dates = Array.isArray(index.dates) ? index.dates : [];
    if (!dates.length) {
      showToast("当前还没有历史预测快照", 3200);
      return;
    }
    const latest = dates[0];
    let snapshotResponse = await fetch(`/api/history?date=${encodeURIComponent(latest.sourceDate)}`, { cache: "no-store" });
    if (!snapshotResponse.ok) throw new Error("无法读取历史预测详情");
    let snapshot = await snapshotResponse.json();
    let existingValidations = Array.isArray(snapshot.validations) ? snapshot.validations : [];
    const now = new Date();
    const middayEnded = now.getHours() > 11 || (now.getHours() === 11 && now.getMinutes() >= 30);
    if (latest.sourceDate === localIsoDate() && middayEnded && !existingValidations.length) {
      const headers = {};
      if (state.refreshTokenRequired) {
        let token = sessionStorage.getItem("sentiboardRefreshToken") || "";
        if (!token) token = window.prompt("请输入看板刷新令牌") || "";
        if (token) headers["X-Refresh-Token"] = token;
      }
      const validationResponse = await fetch(`/api/history/validate?date=${encodeURIComponent(latest.sourceDate)}`, { method: "POST", headers });
      if (validationResponse.ok) {
        snapshotResponse = await fetch(`/api/history?date=${encodeURIComponent(latest.sourceDate)}`, { cache: "no-store" });
        if (snapshotResponse.ok) snapshot = await snapshotResponse.json();
      }
    }
    const prediction = snapshot.prediction || {};
    const summary = prediction.summary || {};
    const sectors = Array.isArray(prediction.sectors) ? prediction.sectors : [];
    const stocks = Array.isArray(prediction.stocks) ? prediction.stocks : [];
    const validations = Array.isArray(snapshot.validations) ? snapshot.validations : [];
    const latestValidation = validations.find((item) => item.isCanonicalMidday)
      || (validations.length ? validations[validations.length - 1] : null);

    $("#drawerKicker").textContent = "FORWARD VALIDATION";
    $("#drawerTitle").textContent = `${latest.sourceDate} 情绪快照`;
    $("#drawerSubtitle").textContent = `${number(snapshot.postCount)} 篇帖子 · 截止 ${escapeHtml(snapshot.capturedAt || "--")} · SHA-256 ${escapeHtml(String(snapshot.postsSha256 || "").slice(0, 10))}`;
    $("#drawerContent").innerHTML = renderHistorySnapshot(summary, sectors, stocks, latestValidation);
    $("#drawerNote").textContent = "验证口径：看多且上涨、看空且下跌为方向一致；中性与绝对涨跌幅小于 0.3% 的横盘样本不计入命中率。";
    $("#overlay").hidden = false;
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    $("#closeDrawer").focus();
  } catch (error) {
    showToast(`历史归档读取失败：${error.message}`, 4200);
  } finally {
    trigger.disabled = false;
  }
}

function renderHistorySnapshot(summary, sectors, stocks, validation) {
  const score = Number(summary.sentimentScore || 0);
  const overall = validation?.overall || {};
  const accuracy = overall.accuracyPct;
  const isObservation = validation?.comparisonMode === "same-day-observation";
  const status = validation
    ? `<span class="history-status ${Number(accuracy) < 50 ? "mismatch" : "verified"}">${isObservation ? "同日方向一致率" : "预测命中率"} ${accuracy ?? "--"}%</span>`
    : `<span class="history-status pending">等待午盘行情验证</span>`;
  const validationRows = Array.isArray(validation?.rows) ? validation.rows : [];
  const findOutcome = (type, item) => validationRows.find((row) => row.entityType === type && (
    (item.code && String(row.code || "") === String(item.code)) || row.name === item.name
  ));
  const actualCell = (row) => {
    if (!row || row.changePct === null || row.changePct === undefined) return `<em class="actual missing">--</em>`;
    const change = Number(row.changePct);
    const label = row.status === "match" ? "一致" : row.status === "miss" ? "背离" : row.status === "flat" ? "横盘" : "不计";
    return `<em class="actual ${change > 0 ? "positive" : change < 0 ? "negative" : "flat"}">${change > 0 ? "+" : ""}${change.toFixed(2)}%<small>${label}</small></em>`;
  };
  const sectorRows = sectors.map((item) => `
    <div class="forecast-row ${findOutcome("sector", item)?.status || ""}">
      <span><b>${escapeHtml(item.name)}</b><small>${number(item.posts)} 篇提及</small></span>
      <strong class="${sentimentClass(item.sentiment)}">${escapeHtml(item.sentiment)}</strong>
      <em>${Number(item.score || 0) > 0 ? "+" : ""}${number(item.score)}</em>
      ${actualCell(findOutcome("sector", item))}
    </div>`).join("");
  const stockRows = stocks.filter((item) => Number(item.posts || 0) > 0).map((item) => `
    <div class="forecast-row ${findOutcome("stock", item)?.status || ""}">
      <span><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.code)} · ${number(item.posts)} 篇</small></span>
      <strong class="${sentimentClass(item.sentiment)}">${escapeHtml(item.sentiment)}</strong>
      <em>${Number(item.score || 0) > 0 ? "+" : ""}${number(item.score)}</em>
      ${actualCell(findOutcome("stock", item))}
    </div>`).join("");
  const market = validation?.marketSnapshot || {};
  const indices = Array.isArray(market.indices) ? market.indices : [];
  const indexStrip = indices.length ? `<section class="market-index-strip">${indices.map((item) => {
    const change = Number(item.changePct || 0);
    return `<div><span>${escapeHtml(item.name)}</span><b class="${change > 0 ? "positive" : change < 0 ? "negative" : ""}">${change > 0 ? "+" : ""}${change.toFixed(2)}%</b></div>`;
  }).join("")}</section>` : "";
  const marketHeadline = validation
    ? Number(market.trackedSectorDown || 0) >= 5
      ? "社媒偏多，但跟踪科技板块午盘普跌"
      : `${number(overall.matches)} 项方向一致，${number(overall.misses)} 项背离`
    : "午盘结束后将自动连接真实行情";
  const validationNote = validation
    ? isObservation
      ? `该情绪快照采集于 11:30 之后，只用于揭示同日背离，不计作预测成绩。11:30 前快照有 ${number(validation.strictDirectionalSignals)} 个可判定方向。`
      : `使用 11:30 前最后一份冻结快照，预测值未按行情重算。`
    : "连接腾讯指数/个股和东方财富板块行情后计算；预测快照保持不变。";
  return `<section class="history-summary">
      <div><span>快照综合情绪</span><b class="${score >= 0 ? "bull" : "bear"}">${score > 0 ? "+" : ""}${score}</b></div>
      <div><span>去重帖子</span><b>${number(summary.posts)}</b></div>
      <div><span>${validation ? (isObservation ? "同日一致率" : "预测命中率") : "看多占比"}</span><b class="${validation && Number(accuracy) < 50 ? "bear" : ""}">${validation ? `${accuracy ?? "--"}%` : `${number(summary.bullRatio)}%`}</b></div>
    </section>
    <div class="history-validation-banner ${validation && Number(accuracy) < 50 ? "mismatch" : ""}">${status}<h3>${escapeHtml(marketHeadline)}</h3><p>${escapeHtml(validationNote)}</p><small>${validation ? `行情截止 ${escapeHtml(validation.marketAsOf || "--")} · ${escapeHtml(validation.marketSource || "")}` : ""}</small></div>
    ${indexStrip}
    <section class="drawer-section forecast-section"><div class="drawer-section-head"><div><span>SECTORS</span><h3>板块情绪 vs 午盘涨跌</h3></div><small>红涨绿跌</small></div><div class="forecast-column-labels"><span>对象</span><span>情绪</span><span>分数</span><span>实际</span></div><div class="forecast-list">${sectorRows}</div></section>
    <section class="drawer-section forecast-section"><div class="drawer-section-head"><div><span>STOCKS</span><h3>个股情绪 vs 午盘涨跌</h3></div><small>仅显示有帖子覆盖</small></div><div class="forecast-column-labels"><span>对象</span><span>情绪</span><span>分数</span><span>实际</span></div><div class="forecast-list">${stockRows || '<div class="sector-stock-empty">暂无明确个股观点</div>'}</div></section>`;
}

function renderSectorStockRanking(item) {
  const label = viewLabel();
  const stocks = Array.isArray(item.topStocks) ? item.topStocks : [];
  const coverage = item.stockRankingCoverage || {};
  const maxScore = Math.max(...stocks.map((stock) => Number(stock.heatScore || 0)), 1);
  const rows = stocks.length ? stocks.map((stock) => {
    const width = Math.max(6, Math.round((Number(stock.heatScore || 0) / maxScore) * 100));
    return `<div class="sector-stock-row">
      <span class="sector-stock-rank">${String(stock.rank).padStart(2, "0")}</span>
      <span class="sector-stock-name"><b>${escapeHtml(stock.name)}</b><small>${escapeHtml(stock.code)}</small></span>
      <span class="sector-stock-signal"><i style="width:${width}%"></i></span>
      <span class="sector-stock-metric"><b>${number(stock.postMentions)}</b><small>提及帖</small></span>
      <span class="sector-stock-metric"><b>${number(stock.imageMentions)}</b><small>图片识别</small></span>
      <span class="sector-stock-score"><b>${number(stock.engagement)}</b><small>帖内互动</small></span>
    </div>`;
  }).join("") : `<div class="sector-stock-empty">${label}的帖子标题、正文、tags 与图片文字中，暂未识别到明确股票名称。</div>`;
  return `<section class="drawer-section sector-stock-ranking">
    <div class="drawer-section-head"><div><span>TOP 10 STOCKS</span><h3>${label}板块热门股票</h3></div><small>帖子覆盖优先</small></div>
    <div class="sector-stock-list">${rows}</div>
    <div class="ranking-coverage">
      <span>扫描 ${number(coverage.postsScanned)} 篇帖子</span>
      <span>OCR ${number(coverage.ocrImages)} 张有文字图片</span>
      <span>跳过 ${number(coverage.ocrSkippedImages)} 张无文字图片</span>
      <span>${number(coverage.constituents)} 只板块成分股</span>
    </div>
    <p class="ranking-formula">按${label}提及帖数排序，互动量用于同提及帖数时的排序。股票名称从标题、正文、tags 和图片 OCR 文字识别；同一篇帖子重复出现只计 1 篇。</p>
  </section>`;
}

function renderDatePicker() {
  const input = $("#dateInput");
  const today = localIsoDate();
  input.max = today;
  input.value = state.selectedDate || state.data?.meta?.dataDate || today;
  const archivedDates = state.availableDates.map((item) => item.sourceDate).filter(Boolean);
  $("#availableDates").innerHTML = archivedDates.length
    ? archivedDates.slice(0, 8).map((date) => `<button class="date-chip ${date === state.selectedDate ? "active" : ""}" type="button" data-date="${escapeHtml(date)}">${escapeHtml(humanDate(date))} · ${number(state.availableDates.find((item) => item.sourceDate === date)?.postCount)} 帖</button>`).join("")
    : '<span class="date-no-archive">还没有可选的历史快照</span>';
  document.querySelectorAll(".date-chip").forEach((button) => {
    button.addEventListener("click", () => {
      input.value = button.dataset.date;
      applySelectedDate();
    });
  });
}

function openDatePicker() {
  renderDatePicker();
  $("#datePopover").hidden = false;
  $("#dateButton").setAttribute("aria-expanded", "true");
  $("#dateInput").focus();
}

function closeDatePicker() {
  $("#datePopover").hidden = true;
  $("#dateButton").setAttribute("aria-expanded", "false");
}

async function applySelectedDate() {
  const selectedDate = $("#dateInput").value;
  if (!selectedDate || state.dateLoading) return;
  state.dateLoading = true;
  $("#applyDateButton").disabled = true;
  try {
    await loadDashboard(selectedDate);
    closeDatePicker();
    showToast(selectedDate === localIsoDate() ? "已切回今天的看板" : `已切换到 ${humanDate(selectedDate)} 的历史快照`, 3000);
  } catch (error) {
    showToast(`无法切换日期：${error.message}`, 4200);
  } finally {
    state.dateLoading = false;
    $("#applyDateButton").disabled = false;
  }
}

async function returnToday() {
  if (state.dateLoading) return;
  state.dateLoading = true;
  try {
    await loadDashboard();
    closeDatePicker();
    showToast("已返回今天的看板");
  } catch (error) {
    showToast(`无法读取今天的数据：${error.message}`, 4200);
  } finally {
    state.dateLoading = false;
  }
}

function renderPost(post, index) {
  const link = post.url ? `<a class="post-link" href="${escapeHtml(post.url)}" target="_blank" rel="noopener noreferrer"><span>在小红书查看原帖</span><span class="ui-icon" aria-hidden="true">&#xE8A7;</span></a>` : "";
  const tags = Array.isArray(post.tags) && post.tags.length
    ? `<div class="post-tags">${post.tags.slice(0, 6).map((tag) => `<span>#${escapeHtml(tag)}</span>`).join("")}</div>`
    : "";
  const images = Array.isArray(post.images) ? post.images.filter(Boolean).slice(0, 9) : [];
  const gallery = images.length
    ? `<div class="post-gallery gallery-${Math.min(images.length, 4)}" aria-label="帖子图片，共 ${images.length} 张">${images.map((url, imageIndex) => `
        <button class="post-image" type="button" data-image-url="${escapeHtml(url)}" data-image-label="${escapeHtml(post.title)} · 图片 ${imageIndex + 1}">
          <img src="${escapeHtml(url)}" alt="${escapeHtml(post.title)}，图片 ${imageIndex + 1}" loading="lazy" referrerpolicy="no-referrer" />
          ${imageIndex === 8 && post.images.length > 9 ? `<span class="image-more">+${post.images.length - 9}</span>` : ""}
        </button>`).join("")}</div>`
    : "";
  const evidence = post.evidenceLevel || "C";
  const evidenceName = evidence === "A" ? "正文 / tags" : evidence === "B" ? "图片文字" : "仅标题";
  return `<article class="post-card">
    <div class="post-number"><span>TOP ${index + 1} · <i class="evidence-badge level-${evidence.toLowerCase()}">${evidence} 级 ${evidenceName}</i></span><span class="sentiment ${sentimentClass(post.sentiment)}">${escapeHtml(post.sentiment || "样本不足")}</span></div>
    <h3>${escapeHtml(post.title)}</h3>
    <p>${escapeHtml(excerpt(post.content) || (evidence === "B" ? "观点来自图片文字识别。" : "正文尚未补全；该帖只参与热度统计。"))}</p>
    ${gallery}
    ${tags}
    <div class="post-meta"><span class="post-author"><b>${escapeHtml(post.author)}</b><span>${escapeHtml(post.published || "刷新时采集")}</span></span><span class="engagement"><span><i class="ui-icon" aria-hidden="true">&#xE8E1;</i>${number(post.likes)}</span><span><i class="ui-icon" aria-hidden="true">&#xE90A;</i>${availableNumber(post.comments, post.commentCountAvailable)}</span></span></div>
    ${link}
  </article>`;
}

function bindPostImages() {
  document.querySelectorAll(".post-image").forEach((button) => {
    button.addEventListener("click", () => openImageViewer(button.dataset.imageUrl, button.dataset.imageLabel));
    const image = button.querySelector("img");
    image?.addEventListener("error", () => button.classList.add("image-error"), { once: true });
  });
}

function openImageViewer(url, label) {
  if (!url) return;
  const viewer = $("#imageViewer");
  const image = $("#viewerImage");
  image.src = url;
  image.alt = label || "小红书帖子图片";
  $("#viewerCaption").textContent = label || "小红书帖子图片";
  viewer.hidden = false;
  $("#closeImageViewer").focus();
}

function closeImageViewer() {
  $("#imageViewer").hidden = true;
  $("#viewerImage").removeAttribute("src");
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#overlay").hidden = true;
  document.body.style.overflow = "";
  state.lastFocus?.focus();
}

async function refreshData() {
  if (state.source === "xhs" && state.data?.meta?.mode === "history") {
    showToast("历史快照为只读；请先返回今天再刷新", 3200);
    return;
  }
  const button = $("#refreshButton");
  button.disabled = true;
  button.classList.add("loading");
  button.querySelector(".button-label").textContent = "正在刷新";
  showToast(state.source === "eastmoney" ? "正在分页扫描 100 只股票 06:00 后的全部发帖，这可能需要几分钟…" : "正在检查数据源并更新快照…", 8000);
  try {
    const headers = {};
    if (state.refreshTokenRequired) {
      let token = sessionStorage.getItem("sentiboardRefreshToken") || "";
      if (!token) token = window.prompt("请输入看板刷新令牌") || "";
      if (!token) throw new Error("未提供刷新令牌");
      headers["X-Refresh-Token"] = token;
      sessionStorage.setItem("sentiboardRefreshToken", token);
    }
    const endpoint = state.source === "eastmoney" ? "/api/eastmoney/refresh" : "/api/refresh";
    const response = await fetch(endpoint, { method: "POST", headers });
    if (response.status === 401) sessionStorage.removeItem("sentiboardRefreshToken");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || "刷新失败");
    if (state.source === "eastmoney") {
      state.eastmoneyData = payload;
      state.activeEastmoneySector = payload.sectors?.[0]?.code || null;
      render();
      showToast(`${payload.meta.message || "东方财富社区扫描完成"} · ${number(payload.summary?.pagesScanned)} 页 / ${number(payload.summary?.scannedPosts)} 帖`, 5200);
    } else {
      state.data = payload;
      state.selectedDate = null;
      render();
      renderDatePicker();
      const requestSummary = payload.meta.requestCount ? ` · ${number(payload.meta.searchRequests)} 次搜索 / ${number(payload.meta.detailRequests)} 次详情` : "";
      showToast(payload.meta.mode === "live" || payload.meta.mode === "mixed"
        ? `${payload.meta.xhsMessage || "刷新完成，已更新小红书公开数据"}${requestSummary}`
        : (payload.meta.xhsMessage || "刷新完成；今天暂无发布日期可验证的帖子"), 3400);
    }
  } catch (error) {
    showToast(`刷新失败：${error.message}`, 4200);
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
    button.querySelector(".button-label").textContent = "刷新数据";
  }
}

let toastTimer;
function showToast(message, duration = 2600) {
  clearTimeout(toastTimer);
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  toastTimer = setTimeout(() => toast.classList.remove("show"), duration);
}

document.addEventListener("DOMContentLoaded", () => {
  loadDashboard().catch((error) => showToast(error.message, 5000));
  $("#refreshButton").addEventListener("click", refreshData);
  $("#eastmoneyUnlockButton").addEventListener("click", unlockEastmoneySession);
  document.querySelectorAll(".source-tab").forEach((button) => button.addEventListener("click", () => {
    switchSource(button.dataset.source).catch((error) => showToast(error.message, 5000));
  }));
  $("#historyButton").addEventListener("click", (event) => openHistory(event.currentTarget));
  $("#dateButton").addEventListener("click", () => {
    if ($("#datePopover").hidden) openDatePicker(); else closeDatePicker();
  });
  $("#closeDatePicker").addEventListener("click", closeDatePicker);
  $("#applyDateButton").addEventListener("click", applySelectedDate);
  $("#todayButton").addEventListener("click", returnToday);
  $("#dateInput").addEventListener("keydown", (event) => { if (event.key === "Enter") applySelectedDate(); });
  $("#closeDrawer").addEventListener("click", closeDrawer);
  $("#overlay").addEventListener("click", closeDrawer);
  $("#closeImageViewer").addEventListener("click", closeImageViewer);
  $("#imageViewer").addEventListener("click", (event) => { if (event.target.id === "imageViewer") closeImageViewer(); });
  $("#methodButton").addEventListener("click", () => { $("#methodModal").hidden = false; $("#closeMethod").focus(); });
  $("#closeMethod").addEventListener("click", () => { $("#methodModal").hidden = true; $("#methodButton").focus(); });
  $("#methodModal").addEventListener("click", (event) => { if (event.target.id === "methodModal") $("#closeMethod").click(); });
  document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button));
    renderStocks(state.data.stocks);
    bindEntityButtons();
  }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#imageViewer").hidden) closeImageViewer();
    else if (event.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer();
    else if (event.key === "Escape" && !$("#methodModal").hidden) $("#closeMethod").click();
    else if (event.key === "Escape" && !$("#datePopover").hidden) closeDatePicker();
  });
  document.addEventListener("click", (event) => {
    if (!$("#datePopover").hidden && !event.target.closest(".date-control")) closeDatePicker();
  });
});
