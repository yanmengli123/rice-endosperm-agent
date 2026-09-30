// background.js — WebSocket 客户端状态机:配对检查、hello 握手、心跳保活、指数退避重连与服务端命令分发。

import {
  DEFAULT_HEARTBEAT_INTERVAL_S,
  DEAD_LINK_MS,
  HELLO_TIMEOUT_MS,
  MAX_FRAME_BYTES,
  frameByteLength,
  gatewayUrlFromOrigin,
  isCmdFrame,
  isHelloOk,
  isTokenRotated,
  makeHeartbeat,
  makeHello,
  makeResultError,
  makeResultOk,
  parseFrame,
} from "./protocol.js";
import { executeOp, OpError } from "./ops.js";
import * as state from "./state.js";

const EXTENSION_VERSION = chrome.runtime.getManifest().version;
const ALARM_KEEPALIVE = "yuxi-browser-keepalive";
const RECONNECT_BASE_MS = 1000; // 退避起点 1s
const RECONNECT_MAX_MS = 30000; // 退避上限 30s
const CONNECTING_STALE_MS = 15000; // connecting 状态卡死兜底

let ws = null;
let wsState = "disconnected"; // disconnected | connecting | ready
let reconnectAttempt = 0;
let reconnectTimer = null;
let helloTimer = null;
let heartbeatTimer = null;
let heartbeatIntervalS = DEFAULT_HEARTBEAT_INTERVAL_S;
let lastServerFrameAt = 0;
let lastHeartbeatSentAt = 0;
let connectStartedAt = 0;
// "revoked":收到 4403(设备被吊销/替换)后停止自动重连,需重新配对
let stopReason = null;

function normalizeHeartbeatInterval(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 1) return DEFAULT_HEARTBEAT_INTERVAL_S;
  return Math.min(300, Math.round(n));
}

function platformName() {
  const ua = navigator.userAgent || "";
  return /Edg\//.test(ua) ? "edge" : "chrome";
}

function setWsState(next) {
  if (wsState === next) return;
  wsState = next;
  persistRuntimeStatus();
}

function persistRuntimeStatus() {
  // 状态写入 storage.session,popup/options 可直接读取而不必唤醒 SW
  state
    .setRuntimeStatus({
      ws_state: wsState,
      stop_reason: stopReason,
      updated_at: new Date().toISOString(),
    })
    .catch(() => {});
}

// ---- 连接管理 ----

async function connect() {
  if (wsState !== "disconnected" || stopReason === "revoked") return;
  // 先同步占位状态,防止重入导致并发建连
  wsState = "connecting";
  connectStartedAt = Date.now();
  persistRuntimeStatus();

  const pairing = await state.getPairing();
  if (!pairing.server_origin || !pairing.device_token) {
    setWsState("disconnected");
    return;
  }
  heartbeatIntervalS = normalizeHeartbeatInterval(pairing.heartbeat_interval_s);
  const url = gatewayUrlFromOrigin(pairing.server_origin);
  if (!url) {
    console.warn("[语析连接] server_origin 非法,无法建立连接");
    setWsState("disconnected");
    return;
  }

  let socket;
  try {
    // MV3 WebSocket 无法自定义 Authorization 头,device_token 经 Sec-WebSocket-Protocol 以 ["bearer", token] 承载
    socket = new WebSocket(url, ["bearer", pairing.device_token]);
  } catch (err) {
    console.warn("[语析连接] 创建 WebSocket 失败:", err && err.message);
    setWsState("disconnected");
    scheduleReconnect();
    return;
  }
  ws = socket;
  socket.onopen = () => {
    if (ws !== socket) return;
    // 第一帧必须是 hello;收到 hello_ok 前不发送任何其他帧
    sendFrame(socket, makeHello(EXTENSION_VERSION, pairing.device_id, platformName(), navigator.userAgent));
    helloTimer = setTimeout(() => {
      console.warn("[语析连接] 5 秒内未收到 hello_ok,放弃本次连接");
      forceReconnect();
    }, HELLO_TIMEOUT_MS);
  };
  socket.onmessage = (event) => {
    if (ws === socket) handleServerFrame(socket, event.data);
  };
  socket.onclose = (event) => {
    handleClosed(socket, event);
  };
  socket.onerror = () => {
    // 具体原因由随后的 onclose 提供
  };
}

function sendFrame(socket, frame) {
  if (!socket || ws !== socket || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(frame));
    return true;
  } catch (err) {
    console.warn("[语析连接] 发送帧失败:", (err && err.message) || "未知错误");
    return false;
  }
}

function handleServerFrame(socket, raw) {
  // 任何入站字节都视为服务端活性信号
  lastServerFrameAt = Date.now();
  const parsed = typeof raw === "string" ? parseFrame(raw) : { ok: false };
  if (!parsed.ok) return;
  const frame = parsed.frame;

  if (isHelloOk(frame)) {
    if (helloTimer) {
      clearTimeout(helloTimer);
      helloTimer = null;
    }
    reconnectAttempt = 0;
    setWsState("ready");
    startHeartbeatLoop();
    console.log("[语析连接] 握手完成,连接就绪");
    return;
  }
  if (isTokenRotated(frame)) {
    handleTokenRotated(frame);
    return;
  }
  if (isCmdFrame(frame)) {
    runCommand(socket, frame);
    return;
  }
  // 其余帧(如服务端心跳回应)仅用于活性判断
}

async function runCommand(socket, frame) {
  const op = frame.op;
  const payload =
    frame.payload && typeof frame.payload === "object" && !Array.isArray(frame.payload) ? frame.payload : {};
  console.log(`[语析连接] 执行命令 ${op} (id=${frame.id})`);
  let resultFrame;
  try {
    const result = await executeOp(op, payload);
    resultFrame = makeResultOk(frame.id, result);
  } catch (err) {
    const code = err instanceof OpError ? err.code : "EXT_INTERNAL_ERROR";
    const message = err && err.message ? err.message : "扩展内部错误";
    if (!(err instanceof OpError)) {
      console.warn(`[语析连接] 命令 ${op} 未预期异常:`, err);
    }
    resultFrame = makeResultError(frame.id, code, message);
  }
  if (frameByteLength(resultFrame) > MAX_FRAME_BYTES) {
    resultFrame = makeResultError(frame.id, "EXT_RESULT_TOO_LARGE", "结果超过单帧 4MB 上限");
  }
  sendFrame(socket, resultFrame);
}

function handleTokenRotated(frame) {
  const newToken = typeof frame.device_token === "string" ? frame.device_token : "";
  if (!newToken) {
    console.warn("[语析连接] token_rotated 帧缺少 device_token,已忽略");
    return;
  }
  const patch = { device_token: newToken };
  if (typeof frame.token_expires_at === "string") patch.token_expires_at = frame.token_expires_at;
  state
    .setPairing(patch)
    .then(() => {
      console.log("[语析连接] 设备令牌已轮换,使用新令牌重连");
      forceReconnect();
    })
    .catch((err) => {
      console.warn("[语析连接] 新令牌持久化失败,保持现有连接:", err && err.message);
    });
}

function startHeartbeatLoop() {
  stopHeartbeatLoop();
  lastHeartbeatSentAt = Date.now();
  heartbeatTimer = setInterval(() => {
    if (wsState !== "ready") {
      stopHeartbeatLoop();
      return;
    }
    if (sendFrame(ws, makeHeartbeat())) lastHeartbeatSentAt = Date.now();
  }, heartbeatIntervalS * 1000);
}

function stopHeartbeatLoop() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function clearLinkTimers() {
  if (helloTimer) {
    clearTimeout(helloTimer);
    helloTimer = null;
  }
  stopHeartbeatLoop();
}

// 主动弃置旧连接(摘掉监听,避免陈旧回调串扰)。
function abandonSocket() {
  const socket = ws;
  ws = null;
  if (!socket) return;
  socket.onopen = null;
  socket.onmessage = null;
  socket.onclose = null;
  socket.onerror = null;
  try {
    socket.close();
  } catch (_) {}
}

async function handleClosed(socket, event) {
  if (ws !== socket) return; // 已被弃置的旧连接
  ws = null;
  clearLinkTimers();
  setWsState("disconnected");
  const code = event && event.code;
  console.log(`[语析连接] 连接关闭 code=${code}`);
  if (code === 4403) {
    // 设备被吊销/替换:关闭任务窗口、清除本地凭证并停止自动重连
    stopReason = "revoked";
    persistRuntimeStatus();
    await revokeLocalIdentity();
    return;
  }
  // 4401 未认证、4408 心跳超时以及其他异常关闭均按退避重连
  scheduleReconnect();
}

async function revokeLocalIdentity() {
  try {
    const ledger = await state.getLedger();
    if (ledger.task_window_id !== null) {
      try {
        await chrome.windows.remove(ledger.task_window_id);
      } catch (_) {
        // 窗口已不存在
      }
    }
    await state.clearLedger();
    await state.clearPairing();
    console.log("[语析连接] 已清除本地配对凭证,等待重新配对");
  } catch (err) {
    console.warn("[语析连接] 清除本地凭证失败:", err && err.message);
  }
}

function scheduleReconnect() {
  if (reconnectTimer || stopReason === "revoked") return;
  const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * Math.pow(2, reconnectAttempt));
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
  console.log(`[语析连接] ${delay}ms 后重连(第 ${reconnectAttempt} 次)`);
}

// 弃置当前连接并立即重连(死链、hello 超时、token 轮换等场景)。
function forceReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  clearLinkTimers();
  abandonSocket();
  setWsState("disconnected");
  connect();
}

// 断开并清除一切本地状态(设置页"断开并清除凭证")。
async function resetPairingNow() {
  stopReason = null;
  reconnectAttempt = 0;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  clearLinkTimers();
  abandonSocket();
  setWsState("disconnected");
  const ledger = await state.getLedger();
  if (ledger.task_window_id !== null) {
    try {
      await chrome.windows.remove(ledger.task_window_id);
    } catch (_) {}
  }
  await state.clearLedger();
  await state.clearPairing();
  persistRuntimeStatus();
  console.log("[语析连接] 已断开并清除本机配对凭证");
}

// ---- chrome.alarms 保活兜底:SW 被唤醒/定时触发时校验连接健康度 ----

async function ensureKeepaliveAlarm() {
  try {
    const existing = await chrome.alarms.get(ALARM_KEEPALIVE);
    if (!existing) {
      // 0.5 分钟为 chrome.alarms 允许的最小周期;WS 心跳本身也会让 SW 保持存活
      await chrome.alarms.create(ALARM_KEEPALIVE, { periodInMinutes: 0.5 });
    }
  } catch (_) {}
}

async function handleKeepaliveAlarm() {
  const now = Date.now();
  if (wsState === "ready") {
    if (lastServerFrameAt && now - lastServerFrameAt > DEAD_LINK_MS) {
      console.warn("[语析连接] 超过 45 秒未收到服务端帧,判定死连接并重连");
      forceReconnect();
      return;
    }
    // interval 计时器可能随 SW 生命周期丢失,这里补发迟到的心跳
    if (now - lastHeartbeatSentAt >= heartbeatIntervalS * 1000) {
      if (sendFrame(ws, makeHeartbeat())) lastHeartbeatSentAt = now;
    }
    return;
  }
  if (wsState === "connecting") {
    if (connectStartedAt && now - connectStartedAt > CONNECTING_STALE_MS) {
      console.warn("[语析连接] 连接建立超时,强制重建");
      forceReconnect();
    }
    return;
  }
  if (stopReason === "revoked") return;
  const paired = await state.isPaired();
  if (paired && !reconnectTimer) connect();
}

// 没有监听器时 chrome.alarms 不会唤醒 SW——保活/死链检测/唤醒自连全部依赖这里
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === ALARM_KEEPALIVE) handleKeepaliveAlarm();
});

// ---- 扩展页消息入口(popup/options → SW) ----

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return undefined;
  if (message.type === "reset_pairing") {
    resetPairingNow()
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true; // 异步 sendResponse
  }
  if (message.type === "pairing_updated") {
    // 新配对完成:解除吊销停机状态,弃置旧连接后立即用新凭证建连
    stopReason = null;
    reconnectAttempt = 0;
    persistRuntimeStatus();
    forceReconnect();
    sendResponse({ ok: true });
    return undefined;
  }
  return undefined;
});

// ---- 任务台账随标签/窗口生命周期同步 ----

chrome.tabs.onRemoved.addListener((tabId) => {
  state.removeTabRefs(tabId).catch(() => {});
});

chrome.windows.onRemoved.addListener((windowId) => {
  state
    .getLedger()
    .then((ledger) => {
      if (ledger.task_window_id === windowId) {
        // 用户手动关闭任务窗口视为任务结束:台账整体清空,借出标记一并归还
        return state.clearLedger();
      }
      return undefined;
    })
    .catch(() => {});
});

// ---- 启动引导 ----

chrome.runtime.onInstalled.addListener(() => {
  ensureKeepaliveAlarm();
  state.isPaired().then((paired) => {
    if (paired) connect();
  });
});

chrome.runtime.onStartup.addListener(() => {
  // 浏览器重启:storage.session 台账已自动清空;已配对则重建连接
  ensureKeepaliveAlarm();
  state.isPaired().then((paired) => {
    if (paired) connect();
  });
});

// SW 每次被唤醒都会执行;已配对且无活动连接时立即尝试建连
ensureKeepaliveAlarm();
state.isPaired().then((paired) => {
  if (paired && wsState === "disconnected" && !reconnectTimer && stopReason !== "revoked") connect();
});
