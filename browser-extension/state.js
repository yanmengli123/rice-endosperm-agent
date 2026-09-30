// state.js — 配对凭证(chrome.storage.local 持久化)与任务窗口/借出标签台账(chrome.storage.session)的统一存取。

import { DEFAULT_HEARTBEAT_INTERVAL_S } from "./protocol.js";

const PAIRING_FIELDS = [
  "server_origin",
  "device_token",
  "device_id",
  "device_name",
  "token_expires_at",
  "heartbeat_interval_s",
];

const LEDGER_KEY = "task_ledger";
const RUNTIME_STATUS_KEY = "runtime_status";

// storage.session 不可用时的进程内兜底(仅当前上下文存活期内有效)。
let memoryLedger = null;
let memoryStatus = null;

function sessionArea() {
  try {
    return chrome.storage && chrome.storage.session ? chrome.storage.session : null;
  } catch (_) {
    return null;
  }
}

function normalizeLedger(raw) {
  const ledger = raw && typeof raw === "object" ? raw : {};
  return {
    task_window_id: Number.isInteger(ledger.task_window_id) ? ledger.task_window_id : null,
    task_tab_ids: Array.isArray(ledger.task_tab_ids)
      ? ledger.task_tab_ids.filter((id) => Number.isInteger(id))
      : [],
    borrowed_tab_ids: Array.isArray(ledger.borrowed_tab_ids)
      ? ledger.borrowed_tab_ids.filter((id) => Number.isInteger(id))
      : [],
  };
}

// ---- 配对凭证(chrome.storage.local,跨浏览器重启保留) ----

export async function getPairing() {
  const empty = {
    server_origin: null,
    device_token: null,
    device_id: null,
    device_name: null,
    token_expires_at: null,
    heartbeat_interval_s: DEFAULT_HEARTBEAT_INTERVAL_S,
  };
  try {
    const bag = await chrome.storage.local.get(PAIRING_FIELDS);
    return {
      server_origin: typeof bag.server_origin === "string" && bag.server_origin ? bag.server_origin : null,
      device_token: typeof bag.device_token === "string" && bag.device_token ? bag.device_token : null,
      device_id: typeof bag.device_id === "string" && bag.device_id ? bag.device_id : null,
      device_name: typeof bag.device_name === "string" && bag.device_name ? bag.device_name : null,
      token_expires_at: typeof bag.token_expires_at === "string" && bag.token_expires_at ? bag.token_expires_at : null,
      heartbeat_interval_s:
        Number.isFinite(bag.heartbeat_interval_s) && bag.heartbeat_interval_s > 0
          ? bag.heartbeat_interval_s
          : DEFAULT_HEARTBEAT_INTERVAL_S,
    };
  } catch (_) {
    return empty;
  }
}

export async function setPairing(patch) {
  const clean = {};
  for (const key of PAIRING_FIELDS) {
    if (patch && patch[key] !== undefined) clean[key] = patch[key];
  }
  if (Object.keys(clean).length === 0) return;
  await chrome.storage.local.set(clean);
}

export async function clearPairing() {
  try {
    await chrome.storage.local.remove(PAIRING_FIELDS);
  } catch (_) {
    // 清除失败无需阻断调用方
  }
}

export async function isPaired() {
  const pairing = await getPairing();
  return Boolean(pairing.server_origin && pairing.device_token);
}

// ---- 任务台账:任务窗口 id、任务窗口标签、借出标签集合 ----

export async function getLedger() {
  const area = sessionArea();
  if (area) {
    try {
      const bag = await area.get(LEDGER_KEY);
      if (bag && bag[LEDGER_KEY]) return normalizeLedger(bag[LEDGER_KEY]);
      return normalizeLedger(null);
    } catch (_) {
      // 读取失败落到内存兜底
    }
  }
  return normalizeLedger(memoryLedger);
}

export async function setLedger(ledger) {
  const normalized = normalizeLedger(ledger);
  memoryLedger = normalized;
  const area = sessionArea();
  if (area) {
    try {
      await area.set({ [LEDGER_KEY]: normalized });
    } catch (_) {
      // 写入失败时内存副本仍可用
    }
  }
  return normalized;
}

export async function clearLedger() {
  memoryLedger = null;
  const area = sessionArea();
  if (area) {
    try {
      await area.remove(LEDGER_KEY);
    } catch (_) {}
  }
}

export async function addTaskTab(tabId) {
  const ledger = await getLedger();
  if (!ledger.task_tab_ids.includes(tabId)) ledger.task_tab_ids.push(tabId);
  return setLedger(ledger);
}

// 标签被关闭时,从任务标签与借出集合中同步移除。
export async function removeTabRefs(tabId) {
  const ledger = await getLedger();
  const taskTabIds = ledger.task_tab_ids.filter((id) => id !== tabId);
  const borrowedTabIds = ledger.borrowed_tab_ids.filter((id) => id !== tabId);
  if (taskTabIds.length === ledger.task_tab_ids.length && borrowedTabIds.length === ledger.borrowed_tab_ids.length) {
    return ledger;
  }
  ledger.task_tab_ids = taskTabIds;
  ledger.borrowed_tab_ids = borrowedTabIds;
  return setLedger(ledger);
}

// 借出 = 仅打标记;归还 = 仅解除标记,均不关闭标签。
export async function borrowTab(tabId) {
  const ledger = await getLedger();
  if (!ledger.borrowed_tab_ids.includes(tabId)) ledger.borrowed_tab_ids.push(tabId);
  return setLedger(ledger);
}

export async function unborrowTab(tabId) {
  const ledger = await getLedger();
  ledger.borrowed_tab_ids = ledger.borrowed_tab_ids.filter((id) => id !== tabId);
  return setLedger(ledger);
}

// 授权判定:任务窗口内的标签(以实时 windowId 为准,天然覆盖用户新开的标签)或已借出的标签。
export async function isTabAuthorized(tabId, ledgerHint) {
  const ledger = ledgerHint || (await getLedger());
  if (ledger.borrowed_tab_ids.includes(tabId)) return true;
  if (ledger.task_window_id !== null) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.windowId === ledger.task_window_id) return true;
    } catch (_) {
      // 标签不存在
    }
  }
  return false;
}

// ---- 运行状态(由 service worker 写入,popup/options 直接读取,避免只为读状态而唤醒 SW) ----

export async function setRuntimeStatus(status) {
  memoryStatus = status;
  const area = sessionArea();
  if (area) {
    try {
      await area.set({ [RUNTIME_STATUS_KEY]: status });
    } catch (_) {}
  }
}

export async function getRuntimeStatus() {
  const area = sessionArea();
  if (area) {
    try {
      const bag = await area.get(RUNTIME_STATUS_KEY);
      if (bag && bag[RUNTIME_STATUS_KEY]) return bag[RUNTIME_STATUS_KEY];
    } catch (_) {}
  }
  return memoryStatus;
}
