// ops.js — 服务端命令实现:navigate / read_page / click / type / screenshot / get_status / end_task。
// 安全模型:仅允许操作任务窗口内的标签页或被显式借出的标签页(见 state.isTabAuthorized)。

import { MAX_FRAME_BYTES } from "./protocol.js";
import * as state from "./state.js";

const READ_TEXT_LIMIT = 20000;
const SCREENSHOT_QUALITY_FIRST = 60;
const SCREENSHOT_QUALITY_RETRY = 35;
// 文本匹配兜底时最多扫描的元素数,防止极端页面拖垮命令超时。
const TEXT_FALLBACK_SCAN_LIMIT = 30000;

export class OpError extends Error {
  constructor(code, message) {
    super(message || code);
    this.name = "OpError";
    this.code = code;
  }
}

function badRequest(message) {
  return new OpError("EXT_BAD_REQUEST", message);
}

function asPayload(payload) {
  return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
}

// 解析目标标签:payload.tab_id 优先;缺省取任务窗口当前活动标签。
async function resolveTargetTab(payload) {
  const p = asPayload(payload);
  const ledger = await state.getLedger();
  if (p.tab_id !== undefined && p.tab_id !== null) {
    const tabId = Number(p.tab_id);
    if (!Number.isInteger(tabId) || tabId <= 0) throw badRequest("payload.tab_id 非法");
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (_) {
      // 不区分"不存在"与"无权访问",统一拒绝
      throw new OpError("EXT_TAB_NOT_AUTHORIZED", `标签 ${tabId} 不存在或不允许访问`);
    }
    if (!(await state.isTabAuthorized(tabId, ledger))) {
      throw new OpError("EXT_TAB_NOT_AUTHORIZED", "目标标签既不在任务窗口内,也未被借出");
    }
    return tab;
  }
  if (ledger.task_window_id === null) {
    throw new OpError("EXT_NO_TASK_WINDOW", "尚无任务窗口,请先下发 navigate 创建");
  }
  const tabs = await chrome.tabs.query({ windowId: ledger.task_window_id, active: true });
  if (!tabs.length) {
    throw new OpError("EXT_NO_TASK_WINDOW", "任务窗口内没有活动标签");
  }
  return tabs[0];
}

function assertHttpUrl(rawUrl) {
  const url = typeof rawUrl === "string" ? rawUrl.trim() : "";
  if (!/^https?:\/\//i.test(url)) {
    throw new OpError("EXT_INVALID_URL", "仅支持 http:// 或 https:// 链接");
  }
  return url;
}

// 向页面 MAIN world 注入自包含函数;注入失败(浏览器内置页等)统一抛 EXT_PAGE_UNSUPPORTED。
async function injectPageFunction(tabId, func, args) {
  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func,
      args,
    });
  } catch (_) {
    throw new OpError("EXT_PAGE_UNSUPPORTED", "该页面不支持脚本注入(如浏览器内置页)");
  }
  return results && results[0] ? results[0].result : undefined;
}

// 注入 MAIN world:元素定位 + 点击/输入。必须自包含,不得引用外部作用域。
// opts = { clear: bool, submit: bool }(仅 type 使用)。
function pageInteract(mode, selector, text, opts) {
  // 精确文本匹配前做空白归一化(连续空白折叠为单空格再比较)。
  function normText(value) {
    return String(value).replace(/\s+/g, " ").trim();
  }
  function findElement(sel) {
    let el = null;
    try {
      el = document.querySelector(sel);
    } catch (_) {
      el = null; // 选择器语法错误按未找到处理
    }
    if (el) return el;
    // 兜底:支持 text=/::-p-text 前缀写法,按元素可见文本精确匹配,取面积最小的元素。
    let wanted = String(sel);
    if (wanted.startsWith("text=")) wanted = wanted.slice(5);
    else if (wanted.startsWith("::-p-text=")) wanted = wanted.slice(10);
    else if (wanted.startsWith("::-p-text(") && wanted.endsWith(")")) wanted = wanted.slice(10, -1);
    if (
      (wanted.startsWith('"') && wanted.endsWith('"') && wanted.length >= 2) ||
      (wanted.startsWith("'") && wanted.endsWith("'") && wanted.length >= 2)
    ) {
      wanted = wanted.slice(1, -1);
    }
    const target = normText(wanted);
    if (!target) return null;
    const all = document.querySelectorAll("body *");
    let best = null;
    let bestArea = Infinity;
    let scanned = 0;
    for (const cand of all) {
      if (++scanned > 30000) break;
      if (cand.getClientRects().length === 0) continue; // 不可见元素跳过
      if (normText(cand.textContent) !== target) continue;
      const rect = cand.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area < bestArea) {
        best = cand;
        bestArea = area;
      }
    }
    return best;
  }

  const el = findElement(selector);
  if (!el) return { found: false };
  try {
    el.scrollIntoView({ block: "center", inline: "center" });
  } catch (_) {
    // 滚动失败不阻断操作
  }

  if (mode === "click") {
    el.click();
    return { found: true, clicked: true };
  }

  // mode === "type":目标须为可编辑元素;定位到容器时尝试其内部第一个可编辑后代。
  let target = el;
  const isEditable = (node) =>
    node instanceof HTMLInputElement ||
    node instanceof HTMLTextAreaElement ||
    node.isContentEditable === true;
  if (!isEditable(target)) {
    target =
      target.querySelector &&
      target.querySelector(
        "input, textarea, [contenteditable=''], [contenteditable='true'], [contenteditable='plaintext-only']"
      );
  }
  if (!target) return { found: true, editable: false };
  target.focus();

  const isFormField = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
  const dispatchInput = () => {
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
  };

  if (opts.clear) {
    if (isFormField) {
      target.setSelectionRange(0, target.value.length);
      let cleared = false;
      try {
        cleared = document.execCommand("delete");
      } catch (_) {
        cleared = false;
      }
      if (!cleared && target.value !== "") {
        target.value = "";
        dispatchInput();
      }
    } else {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(target);
      sel.removeAllRanges();
      sel.addRange(range);
      let cleared = false;
      try {
        cleared = document.execCommand("delete");
      } catch (_) {
        cleared = false;
      }
      if (!cleared) target.textContent = "";
    }
  }

  // 光标归位到末尾,保证插入位置确定(未 clear 时为追加语义)。
  if (isFormField) {
    target.setSelectionRange(target.value.length, target.value.length);
  } else {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(target);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  let inserted = false;
  try {
    inserted = document.execCommand("insertText", false, text);
  } catch (_) {
    inserted = false;
  }
  if (!inserted) {
    if (isFormField) {
      target.value += text;
      dispatchInput();
    } else {
      target.appendChild(document.createTextNode(String(text)));
      target.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  if (opts.submit) {
    const init = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    target.dispatchEvent(new KeyboardEvent("keydown", init));
    target.dispatchEvent(new KeyboardEvent("keyup", init));
  }
  return { found: true, editable: true };
}

// 注入 MAIN world:提取页面标题、地址与正文可读文本(压缩空白,截断 20000 字符)。
function extractReadablePage(limit) {
  const compressed = String((document.body && document.body.innerText) || "")
    .replace(/\r/g, "")
    .replace(/[ \t\f\v]+/g, " ")
    .replace(/ ?\n ?/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  const truncated = compressed.length > limit;
  return {
    url: location.href,
    title: document.title || "",
    text: truncated ? compressed.slice(0, limit) : compressed,
    truncated,
  };
}

async function opNavigate(payload) {
  const p = asPayload(payload);
  const url = assertHttpUrl(p.url);
  const newTab = p.new_tab === true;
  const ledger = await state.getLedger();

  if (p.tab_id !== undefined && p.tab_id !== null) {
    if (newTab) throw badRequest("指定 tab_id 时不能同时设置 new_tab=true");
    const target = await resolveTargetTab(p);
    const tab = await chrome.tabs.update(target.id, { url, active: true });
    return { tab_id: tab.id, url, title: tab.title || "" };
  }

  let windowId = ledger.task_window_id;
  let windowAlive = false;
  if (windowId !== null) {
    try {
      await chrome.windows.get(windowId);
      windowAlive = true;
    } catch (_) {
      windowAlive = false;
    }
  }

  let tab;
  if (!windowAlive) {
    // 无任务窗口:navigate 负责创建
    const win = await chrome.windows.create({ url, focused: true });
    tab = win.tabs && win.tabs[0] ? win.tabs[0] : null;
    if (!tab) {
      const tabs = await chrome.tabs.query({ windowId: win.id });
      tab = tabs[0] || null;
    }
    await state.setLedger({
      task_window_id: win.id,
      task_tab_ids: tab ? [tab.id] : [],
      borrowed_tab_ids: ledger.borrowed_tab_ids,
    });
  } else if (newTab) {
    tab = await chrome.tabs.create({ windowId, url, active: true });
    await state.addTaskTab(tab.id);
  } else {
    const current = await chrome.tabs.query({ windowId, active: true });
    if (current.length) {
      tab = await chrome.tabs.update(current[0].id, { url });
    } else {
      tab = await chrome.tabs.create({ windowId, url, active: true });
      await state.addTaskTab(tab.id);
    }
  }
  // title 取更新时点的快照,页面未加载完成时可能为空或保留旧标题(协议允许)
  return { tab_id: tab ? tab.id : null, url, title: (tab && tab.title) || "" };
}

async function opReadPage(payload) {
  const tab = await resolveTargetTab(payload);
  const data = await injectPageFunction(tab.id, extractReadablePage, [READ_TEXT_LIMIT]);
  if (!data || typeof data !== "object") {
    throw new OpError("EXT_PAGE_UNSUPPORTED", "未能读取页面内容");
  }
  return data;
}

async function opClick(payload) {
  const p = asPayload(payload);
  const selector = typeof p.selector === "string" ? p.selector : "";
  if (!selector.trim()) throw badRequest("缺少 selector");
  const tab = await resolveTargetTab(p);
  const outcome = await injectPageFunction(tab.id, pageInteract, ["click", selector, "", { clear: false, submit: false }]);
  if (!outcome || outcome.found !== true) {
    throw new OpError("EXT_SELECTOR_NOT_FOUND", `未找到元素:${selector}`);
  }
  return { clicked: true, selector };
}

async function opType(payload) {
  const p = asPayload(payload);
  const selector = typeof p.selector === "string" ? p.selector : "";
  if (!selector.trim()) throw badRequest("缺少 selector");
  if (typeof p.text !== "string") throw badRequest("缺少 text");
  const opts = { clear: p.clear === true, submit: p.submit === true };
  const tab = await resolveTargetTab(p);
  const outcome = await injectPageFunction(tab.id, pageInteract, ["type", selector, p.text, opts]);
  if (!outcome || outcome.found !== true) {
    throw new OpError("EXT_SELECTOR_NOT_FOUND", `未找到元素:${selector}`);
  }
  if (outcome.editable !== true) {
    throw new OpError("EXT_ELEMENT_NOT_EDITABLE", "定位到的元素不是可输入的文本控件");
  }
  return { ok: true };
}

function estimateDataUrlBytes(dataUrl) {
  // base64 长度近似还原为字节数(前缀误差可忽略)
  return Math.floor((dataUrl.length * 3) / 4);
}

async function captureVisible(windowId, quality) {
  // 被遮挡/最小化窗口在部分 Chromium 上会让 captureVisibleTab 永不 resolve
  // (hang 不进 catch):必须竞速超时,把挂起转成结构化错误而不是拖死命令。
  const captured = chrome.tabs.captureVisibleTab(windowId, { format: "jpeg", quality }).then(
    (dataUrl) => dataUrl,
    (err) => {
      throw new OpError("EXT_SCREENSHOT_FAILED", `无法截取当前页面:${(err && err.message) || "未知错误"}`);
    }
  );
  return Promise.race([
    captured,
    new Promise((_, reject) =>
      setTimeout(
        () => reject(new OpError("EXT_SCREENSHOT_TIMEOUT", "截图超时:任务窗口被遮挡或不可渲染")),
        8000
      )
    ),
  ]);
}

// 尽力解码截图尺寸;失败则省略可选字段。
async function decodeImageSize(dataUrl, timeoutMs = 3000) {
  // 尺寸是可选增强字段:个别 Chromium 的 SW 里 fetch(dataUrl)/createImageBitmap
  // 会挂起(hang 不进 catch),必须带超时竞速,绝不拖垮截图主流程。
  const work = (async () => {
    const blob = await (await fetch(dataUrl)).blob();
    const bitmap = await createImageBitmap(blob);
    const size = { width: bitmap.width, height: bitmap.height };
    if (bitmap.close) bitmap.close();
    return size;
  })();
  return Promise.race([
    work,
    new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs)),
  ]);
}

async function waitForHttpCommit(tabId, timeoutMs = 8000) {
  // 任务窗口以 about:blank 起步;about:/chrome: 方案不被任何 host 权限覆盖,
  // 在导航提交前 captureVisibleTab 必然报权限错——等待目标页真正提交。
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const tab = await chrome.tabs.get(tabId);
    if (/^https?:/i.test(tab.url || "")) return tab;
    if (Date.now() >= deadline) return tab;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

async function opScreenshot(payload) {
  let tab = await resolveTargetTab(payload);
  tab = await waitForHttpCommit(tab.id);
  // captureVisibleTab 只能截取窗口当前可见(活动)标签;目标非活动时先激活并稍等渲染。
  if (tab.active === false) {
    tab = await chrome.tabs.update(tab.id, { active: true });
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  // 被遮挡/最小化的窗口在部分 Chromium 上会让 captureVisibleTab 挂起或报错:置前兜底。
  try {
    await chrome.windows.update(tab.windowId, { focused: true });
    await new Promise((resolve) => setTimeout(resolve, 100));
  } catch (_) {}
  let dataUrl = await captureVisible(tab.windowId, SCREENSHOT_QUALITY_FIRST);
  if (estimateDataUrlBytes(dataUrl) > MAX_FRAME_BYTES) {
    dataUrl = await captureVisible(tab.windowId, SCREENSHOT_QUALITY_RETRY);
    if (estimateDataUrlBytes(dataUrl) > MAX_FRAME_BYTES) {
      throw new OpError("EXT_SCREENSHOT_TOO_LARGE", "截图超过单帧 4MB 上限");
    }
  }
  const out = { data_url: dataUrl };
  try {
    const size = await decodeImageSize(dataUrl);
    if (size && Number.isFinite(size.width) && Number.isFinite(size.height)) {
      out.width = size.width;
      out.height = size.height;
    }
  } catch (_) {
    // 尺寸解码失败不影响截图返回
  }
  return out;
}

async function opGetStatus() {
  const ledger = await state.getLedger();
  let taskWindowId = ledger.task_window_id;
  let tabs = [];
  if (taskWindowId !== null) {
    try {
      await chrome.windows.get(taskWindowId);
      tabs = await chrome.tabs.query({ windowId: taskWindowId });
    } catch (_) {
      // 任务窗口已被关闭:校正台账
      taskWindowId = null;
      tabs = [];
    }
  }
  // 借出集合剔除已关闭的标签
  const borrowed = [];
  const borrowedTabs = [];
  for (const id of ledger.borrowed_tab_ids) {
    try {
      const tab = await chrome.tabs.get(id);
      borrowed.push(id);
      borrowedTabs.push({ tab_id: tab.id, url: tab.url || "", title: tab.title || "" });
    } catch (_) {
      // 标签已不存在
    }
  }
  await state.setLedger({
    task_window_id: taskWindowId,
    task_tab_ids: tabs.map((t) => t.id),
    borrowed_tab_ids: borrowed,
  });
  return {
    task_window_id: taskWindowId,
    tabs: tabs.map((t) => ({ tab_id: t.id, url: t.url || "", title: t.title || "" })),
    borrowed,
    borrowed_tabs: borrowedTabs,
  };
}

async function opEndTask() {
  const ledger = await state.getLedger();
  if (ledger.task_window_id !== null) {
    try {
      await chrome.windows.remove(ledger.task_window_id);
    } catch (_) {
      // 窗口已被关闭,视为结束成功
    }
  }
  // 清空台账:借出标签仅解除标记,不关闭
  await state.clearLedger();
  return { ok: true };
}

// 命令分发入口;OpError 携带协议错误码,其余异常由调用方归一为 EXT_INTERNAL_ERROR。
export async function executeOp(op, payload) {
  switch (op) {
    case "navigate":
      return opNavigate(payload);
    case "read_page":
      return opReadPage(payload);
    case "click":
      return opClick(payload);
    case "type":
      return opType(payload);
    case "screenshot":
      return opScreenshot(payload);
    case "get_status":
      return opGetStatus();
    case "end_task":
      return opEndTask();
    default:
      throw new OpError("EXT_UNKNOWN_OP", `未知操作:${op}`);
  }
}
