const state = { data: null, filter: "全部", lastFocus: null, refreshTokenRequired: false };
const $ = (selector) => document.querySelector(selector);
const fmt = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });

function sentimentClass(value) {
  return value === "看多" ? "bull" : value === "看空" ? "bear" : "neutral";
}

function number(value) {
  return fmt.format(Number(value || 0));
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function excerpt(value = "", limit = 240) {
  const normalized = String(value).replace(/\s+/g, " ").trim();
  return normalized.length > limit ? `${normalized.slice(0, limit)}…` : normalized;
}

async function loadDashboard() {
  const [response, configResponse] = await Promise.all([
    fetch("/api/dashboard", { cache: "no-store" }),
    fetch("/api/config", { cache: "no-store" }),
  ]);
  if (!response.ok) throw new Error("无法读取看板数据");
  state.data = await response.json();
  if (configResponse.ok) {
    const config = await configResponse.json();
    state.refreshTokenRequired = Boolean(config.refreshTokenRequired);
  }
  render();
}

function render() {
  const { meta, summary, sectors, stocks } = state.data;
  $("#updatedLabel").textContent = `截止 ${meta.updatedLabel}`;
  $("#windowLabel").textContent = meta.window;
  $("#modeBadge").textContent = meta.modeLabel;
  $("#modeBadge").classList.toggle("live", meta.mode === "live" || meta.mode === "mixed");
  $("#sourceText").textContent = meta.mode === "live" || meta.mode === "mixed"
    ? (meta.xhsMessage || "Agent Reach 已连接小红书公开内容")
    : (meta.xhsMessage || "今天尚未采集到发布日期可验证的帖子");
  $("#sampleScope").textContent = `北京时间 ${meta.dataDate || "今日"} · 去重 ${meta.samplePoolSize || 0} 帖 · ${meta.sampledEntityCount || 0}/${sectors.length + stocks.length} 实体已覆盖`;
  $("#postsMetricLabel").textContent = "今日去重帖子";
  $("#commentsMetricLabel").textContent = "今日帖子评论";
  $("#stockSectionTitle").textContent = "今日股票讨论监控池";
  $("#marketSource").textContent = `行情来源：${meta.marketSource}`;

  const pulse = Math.max(0, Math.min(100, 50 + summary.sentimentScore / 2));
  $("#pulseScore").textContent = Math.round(pulse);
  $("#pulseBar").style.left = `${pulse}%`;
  $("#pulseTrend").textContent = summary.sentimentScore > 15 ? "偏多" : summary.sentimentScore < -15 ? "偏空" : "中性";
  $("#pulseTrend").className = `pulse-trend ${sentimentClass(summary.sentimentScore > 15 ? "看多" : summary.sentimentScore < -15 ? "看空" : "中性")}`;
  $("#postsMetric").textContent = number(summary.posts);
  $("#commentsMetric").textContent = number(summary.comments);
  $("#bullMetric").textContent = `${summary.bullRatio}%`;
  $("#sectorGrid").innerHTML = sectors.map(renderSector).join("");
  renderStocks(stocks);
  bindEntityButtons();
}

function renderSector(item) {
  const barWidth = Math.max(8, Math.min(100, 50 + item.score / 2));
  const keywords = Array.isArray(item.keywords) ? item.keywords : [];
  return `
    <article class="sector-card entity-open ${sentimentClass(item.sentiment)}" tabindex="0" role="button" aria-label="查看${escapeHtml(item.name)}热门帖子" data-kind="sector" data-name="${escapeHtml(item.name)}">
      <div class="sector-top">
        <div><span class="rank">0${item.rank}</span><h3>${escapeHtml(item.name)}</h3></div>
        <div class="source-stack"><span class="entity-source ${item.dataSource === "live" ? "sampled" : "placeholder"}">${item.dataSource === "live" ? "今日采样" : "今日暂无"}</span><span class="sentiment ${sentimentClass(item.sentiment)}">${item.sentiment}</span></div>
      </div>
      <div class="heat-row"><div class="heat-bar"><i style="width:${barWidth}%"></i></div><span class="heat-score">${item.score > 0 ? "+" : ""}${item.score}</span></div>
      <div class="sector-stats">
        <div class="sector-stat"><span>帖子数量</span><b>${number(item.posts)}</b></div>
        <div class="sector-stat"><span>评论数量</span><b>${number(item.comments)}</b></div>
      </div>
      <div class="sector-foot"><span class="keywords">${keywords.map((k) => `#${escapeHtml(k)}`).join("  ") || "今天暂无匹配标签"}</span><span class="view-arrow ui-icon" aria-hidden="true">&#xE8A7;</span></div>
    </article>`;
}

function renderStocks(stocks) {
  const visible = state.filter === "全部" ? stocks : stocks.filter((stock) => stock.sentiment === state.filter);
  const maxPosts = Math.max(...stocks.map((stock) => Number(stock.posts || 0)), 1);
  $("#stockTable").innerHTML = visible.length ? visible.map((stock) => {
    const hasQuote = stock.price !== null && stock.price !== undefined
      && stock.changePct !== null && stock.changePct !== undefined
      && Number.isFinite(Number(stock.price)) && Number.isFinite(Number(stock.changePct));
    const change = hasQuote ? Number(stock.changePct) : null;
    const changeClass = change === null ? "" : change >= 0 ? "positive" : "negative";
    const priceHtml = hasQuote
      ? `<b>¥${Number(stock.price).toFixed(2)}</b><span class="${changeClass}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</span>`
      : `<b>--</b><span>今日暂无</span>`;
    const heatWidth = stock.posts ? Math.max(8, Math.min(100, (stock.posts / maxPosts) * 100)) : 0;
    return `<tr class="entity-open ${sentimentClass(stock.sentiment)} ${stock.dataSource === "live" ? "sampled-row" : "placeholder-row"}" tabindex="0" data-kind="stock" data-name="${escapeHtml(stock.name)}">
      <td><div class="stock-name-cell"><span class="table-rank">${String(stock.rank).padStart(2, "0")}</span><span class="stock-name"><b>${escapeHtml(stock.name)}</b><small>${stock.code} · ${escapeHtml(stock.sector)} · ${stock.dataSource === "live" ? "今日采样" : "今日暂无"}</small></span></div></td>
      <td>${escapeHtml(stock.sector)}</td>
      <td><span class="price">${priceHtml}</span></td>
      <td><div class="mini-heat ${stock.heatChange >= 0 ? "up" : "down"}"><span><i style="width:${heatWidth}%"></i></span><b class="${stock.heatChange >= 0 ? "positive" : "negative"}">${stock.heatChange >= 0 ? "+" : ""}${stock.heatChange}%</b></div></td>
      <td class="counts">${number(stock.posts)} / ${number(stock.comments)}</td>
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
  $("#drawerKicker").textContent = kind === "sector" ? "SECTOR · STOCK HEAT" : `${item.code} · TOP POSTS`;
  $("#drawerTitle").textContent = item.name;
  const topPosts = Array.isArray(item.topPosts) ? item.topPosts : [];
  const topStocks = Array.isArray(item.topStocks) ? item.topStocks : [];
  $("#drawerSubtitle").textContent = kind === "sector"
    ? `今日板块热门股票 ${topStocks.length}/10 · 高互动帖子 ${Math.min(topPosts.length, 3)} 篇 · 当前情绪 ${item.sentiment}`
    : `今日互动量最高的 ${Math.min(topPosts.length, 3)} 篇帖子 · 当前情绪 ${item.sentiment} · ${item.dataSource === "live" ? "今日采样" : "今日暂无"}`;
  const postsHtml = topPosts.length
    ? topPosts.map((post, index) => renderPost(post, index)).join("")
    : `<div class="drawer-empty"><span class="ui-icon" aria-hidden="true">&#xE9D9;</span><h3>今天暂无可验证帖子</h3><p>为保证数据口径准确，非今天或发布日期不明确的帖子不会出现在这里。</p></div>`;
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

function renderSectorStockRanking(item) {
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
  }).join("") : `<div class="sector-stock-empty">今天的帖子标题、正文、tags 与图片文字中，暂未识别到明确股票名称。</div>`;
  return `<section class="drawer-section sector-stock-ranking">
    <div class="drawer-section-head"><div><span>TOP 10 STOCKS</span><h3>今日板块热门股票</h3></div><small>帖子覆盖优先</small></div>
    <div class="sector-stock-list">${rows}</div>
    <div class="ranking-coverage">
      <span>扫描 ${number(coverage.postsScanned)} 篇帖子</span>
      <span>OCR ${number(coverage.ocrImages)} 张有文字图片</span>
      <span>跳过 ${number(coverage.ocrSkippedImages)} 张无文字图片</span>
      <span>${number(coverage.constituents)} 只板块成分股</span>
    </div>
    <p class="ranking-formula">按今日提及帖数排序，互动量用于同提及帖数时的排序。股票名称从标题、正文、tags 和图片 OCR 文字识别；同一篇帖子重复出现只计 1 篇。</p>
  </section>`;
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
  return `<article class="post-card">
    <div class="post-number"><span>TOP ${index + 1}</span><span class="sentiment ${sentimentClass(post.sentiment)}">${post.sentiment}</span></div>
    <h3>${escapeHtml(post.title)}</h3>
    <p>${escapeHtml(excerpt(post.content))}</p>
    ${gallery}
    ${tags}
    <div class="post-meta"><span class="post-author"><b>${escapeHtml(post.author)}</b><span>${escapeHtml(post.published || "刷新时采集")}</span></span><span class="engagement"><span><i class="ui-icon" aria-hidden="true">&#xE8E1;</i>${number(post.likes)}</span><span><i class="ui-icon" aria-hidden="true">&#xE90A;</i>${number(post.comments)}</span></span></div>
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
  const button = $("#refreshButton");
  button.disabled = true;
  button.classList.add("loading");
  button.querySelector(".button-label").textContent = "正在刷新";
  showToast("正在检查数据源并更新快照…", 5000);
  try {
    const headers = {};
    if (state.refreshTokenRequired) {
      let token = sessionStorage.getItem("sentiboardRefreshToken") || "";
      if (!token) token = window.prompt("请输入看板刷新令牌") || "";
      if (!token) throw new Error("未提供刷新令牌");
      headers["X-Refresh-Token"] = token;
      sessionStorage.setItem("sentiboardRefreshToken", token);
    }
    const response = await fetch("/api/refresh", { method: "POST", headers });
    if (response.status === 401) sessionStorage.removeItem("sentiboardRefreshToken");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || payload.error || "刷新失败");
    state.data = payload;
    render();
    showToast(payload.meta.mode === "live" || payload.meta.mode === "mixed"
      ? (payload.meta.xhsMessage || "刷新完成，已更新小红书公开数据")
      : (payload.meta.xhsMessage || "刷新完成；今天暂无发布日期可验证的帖子"), 3400);
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
  });
});
