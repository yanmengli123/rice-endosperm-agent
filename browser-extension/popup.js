// popup.js — 弹出页:连接状态展示、当前标签借出/收回开关、设置页入口。

import * as state from "./state.js";

const els = {
  dot: document.getElementById("status-dot"),
  text: document.getElementById("status-text"),
  server: document.getElementById("server-line"),
  task: document.getElementById("task-line"),
  tabTitle: document.getElementById("tab-title"),
  borrow: document.getElementById("borrow-btn"),
  hint: document.getElementById("borrow-hint"),
  version: document.getElementById("version"),
  options: document.getElementById("open-options"),
};

let currentTab = null;
let borrowBusy = false;

function applyStatus(pairing, runtime) {
  const wsState = runtime && runtime.ws_state;
  let cls = "off";
  let label = "未配对";
  if (pairing.server_origin && pairing.device_token) {
    if (wsState === "ready") {
      cls = "on";
      label = "已连接";
    } else if (wsState === "connecting") {
      cls = "wait";
      label = "连接中…";
    } else if (runtime && runtime.stop_reason === "revoked") {
      cls = "off";
      label = "设备已吊销,请重新配对";
    } else {
      cls = "off";
      label = "离线";
    }
  }
  els.dot.className = `dot ${cls}`;
  els.text.textContent = label;
  els.server.textContent = pairing.server_origin
    ? `服务器:${pairing.server_origin}`
    : "尚未配对,请先在设置页粘贴配对链接完成配对";
}

async function renderBorrow(ledger) {
  let tabs;
  try {
    tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  } catch (_) {
    tabs = [];
  }
  currentTab = tabs && tabs[0] ? tabs[0] : null;
  if (!currentTab) {
    els.tabTitle.textContent = "未找到当前标签";
    els.borrow.disabled = true;
    els.borrow.textContent = "";
    els.hint.textContent = "";
    return;
  }
  els.tabTitle.textContent = currentTab.title || currentTab.url || "(无标题标签)";
  if (ledger.task_window_id !== null && currentTab.windowId === ledger.task_window_id) {
    els.borrow.disabled = true;
    els.borrow.textContent = "该标签已在任务窗口中";
    els.hint.textContent = "";
    return;
  }
  const borrowed = ledger.borrowed_tab_ids.includes(currentTab.id);
  els.borrow.disabled = false;
  els.borrow.textContent = borrowed ? "从任务收回该标签" : "借出当前标签给任务";
  els.hint.textContent = borrowed
    ? "已借出:服务端命令可以操作此标签;收回仅解除标记,不会关闭标签。"
    : "任务窗口以外的标签须先借出,服务端命令才允许操作。";
}

async function refresh() {
  const [pairing, runtime, ledger] = await Promise.all([
    state.getPairing(),
    state.getRuntimeStatus(),
    state.getLedger(),
  ]);
  applyStatus(pairing, runtime);
  if (ledger.task_window_id !== null) {
    els.task.textContent = `任务窗口运行中,共 ${ledger.task_tab_ids.length} 个标签;已借出 ${ledger.borrowed_tab_ids.length} 个。`;
  } else {
    els.task.textContent = "当前没有任务窗口(服务端下发 navigate 时自动创建)。";
  }
  await renderBorrow(ledger);
}

els.borrow.addEventListener("click", async () => {
  if (!currentTab || borrowBusy) return;
  borrowBusy = true;
  try {
    const ledger = await state.getLedger();
    if (ledger.borrowed_tab_ids.includes(currentTab.id)) {
      await state.unborrowTab(currentTab.id);
    } else {
      await state.borrowTab(currentTab.id);
    }
  } catch (_) {
    // 状态刷新在 finally 中统一处理
  } finally {
    borrowBusy = false;
    await refresh();
  }
});

els.options.addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

els.version.textContent = `版本 v${chrome.runtime.getManifest().version}`;
refresh();
// 弹出页打开期间低频轮询,保证连接状态实时
setInterval(refresh, 2000);
