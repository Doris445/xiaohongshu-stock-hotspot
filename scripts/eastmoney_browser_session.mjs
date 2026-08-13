const [, , command = "status", endpoint = "http://127.0.0.1:9333", targetUrl = ""] = process.argv;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`CDP HTTP ${response.status}`);
  return response.json();
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
  }

  async connect() {
    this.ws = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("CDP connect timeout")), 8000);
      this.ws.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
      this.ws.addEventListener("error", () => { clearTimeout(timer); reject(new Error("CDP unavailable")); }, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve, reject, timer } = this.pending.get(message.id);
      clearTimeout(timer);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message || "CDP command failed"));
      else resolve(message.result || {});
    });
  }

  send(method, params = {}, timeoutMs = 15000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} timeout`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    try { this.ws?.close(); } catch {}
  }
}

function pageResult(expressionResult) {
  const value = expressionResult?.result?.value || {};
  const title = String(value.title || "");
  const text = String(value.text || "");
  const url = String(value.url || "");
  const blocked = /身份核实|滑块|拼图|验证码|访问过于频繁|captcha/i.test(`${title}\n${text}`);
  const supported = /guba\.eastmoney\.com\/news,|caifuhao\.eastmoney\.com\/news\//i.test(url);
  return {
    status: blocked ? "verification_required" : supported ? "ready" : "waiting",
    title,
    url,
    blocked,
    html: blocked ? "" : String(value.html || ""),
  };
}

async function inspectPage(page) {
  const client = new CdpClient(page.webSocketDebuggerUrl);
  await client.connect();
  try {
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => ({
        title: document.title,
        url: location.href,
        text: (document.body?.innerText || '').slice(0, 1200),
        html: document.documentElement?.outerHTML || ''
      }))()`,
      returnByValue: true,
    });
    return pageResult(result);
  } finally {
    client.close();
  }
}

async function getPages() {
  const pages = await jsonRequest(`${endpoint}/json/list`);
  return pages.filter((item) => item.type === "page" && item.webSocketDebuggerUrl);
}

async function status() {
  const pages = await getPages();
  const page = pages.find((item) => /eastmoney\.com/i.test(item.url || "")) || pages[0];
  if (!page) return { status: "offline", message: "隔离浏览器没有可用页面" };
  const inspected = await inspectPage(page);
  return {
    ...inspected,
    html: undefined,
    message: inspected.status === "ready"
      ? "人工核验已通过，可以复用隔离会话"
      : inspected.status === "verification_required"
      ? "请在隔离窗口中手动完成滑块"
      : "请在隔离窗口中打开东方财富帖子详情",
  };
}

async function fetchPage(url) {
  if (!/^https:\/\/(?:guba|caifuhao)\.eastmoney\.com\//i.test(url)) {
    throw new Error("target URL is not an allowed Eastmoney page");
  }
  const pages = await getPages();
  let page = pages.find((item) => /eastmoney\.com/i.test(item.url || ""));
  if (!page) {
    page = await jsonRequest(`${endpoint}/json/new?${encodeURIComponent(url)}`, { method: "PUT" });
  }
  const client = new CdpClient(page.webSocketDebuggerUrl);
  await client.connect();
  try {
    await client.send("Page.enable");
    await client.send("Runtime.enable");
    await client.send("Page.navigate", { url }, 20000);
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await sleep(350);
      const state = await client.send("Runtime.evaluate", {
        expression: "document.readyState",
        returnByValue: true,
      });
      if (state?.result?.value === "complete") break;
    }
    await sleep(900);
    const result = await client.send("Runtime.evaluate", {
      expression: `(() => ({
        title: document.title,
        url: location.href,
        text: (document.body?.innerText || '').slice(0, 1200),
        html: document.documentElement?.outerHTML || ''
      }))()`,
      returnByValue: true,
    }, 20000);
    return pageResult(result);
  } finally {
    client.close();
  }
}

try {
  const result = command === "fetch" ? await fetchPage(targetUrl) : await status();
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  process.stdout.write(JSON.stringify({ status: "offline", message: error.message || String(error) }));
  process.exitCode = 1;
}
