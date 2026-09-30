// options.js — 设置页:粘贴配对链接完成配对、展示配对与连接状态、断开并清除凭证。

import { authorizeUrlFromOrigin, parsePairingLink } from "./protocol.js";
import * as state from "./state.js";

const els = {
  version: document.getElementById("version"),
  link: document.getElementById("pairing-link"),
  deviceName: document.getElementById("device-name"),
  pairBtn: document.getElementById("pair-btn"),
  pairMsg: document.getElementById("pair-msg"),
  origin: document.getElementById("st-origin"),
  device: document.getElementById("st-device"),
  conn: document.getElementById("st-conn"),
  expiry: document.getElementById("st-expiry"),
  resetBtn: document.getElementById("reset-btn"),
};

const PAIRING_ERROR_TEXT = {
  pairing_link_invalid: "配对链接无效",
  pairing_link_expired: "配对链接已过期,请重新生成",
  pairing_link_consumed: "配对链接已被使用,请重新生成",
  user_unavailable: "用户暂不可用,请联系服务端管理员",
};

function showMsg(text, kind) {
  els.pairMsg.textContent = text;
  els.pairMsg.className = `msg ${kind || "info"}`;
}

function setBusy(busy) {
  els.pairBtn.disabled = busy;
  els.resetBtn.disabled = busy;
}

// 设备名默认值:浏览器名 + 操作系统,如 "Chrome on Windows"。
function defaultDeviceName() {
  const ua = navigator.userAgent || "";
  const browser = /Edg\//.test(ua) ? "Edge" : "Chrome";
  const platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
  let os = "PC";
  if (/win/i.test(platform)) os = "Windows";
  else if (/mac/i.test(platform)) os = "macOS";
  else if (/linux|arm/i.test(platform)) os = "Linux";
  return `${browser} on ${os}`;
}

function clampHeartbeat(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 1) return 10;
  return Math.min(300, Math.round(n));
}

// 消息送达后台失败(SW 暂不可用)时静默降级,由调用方自行完成本地清理。
function notifyBackground(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(payload, (resp) => {
        if (chrome.runtime.lastError) resolve(null);
        else resolve(resp || null);
      });
    } catch (_) {
      resolve(null);
    }
  });
}

function describePairingError(status, detail) {
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const known = PAIRING_ERROR_TEXT[detail.code];
    if (known) return known;
    if (detail.message) return String(detail.message);
    if (detail.code) return String(detail.code);
  }
  if (typeof detail === "string" && detail) return detail;
  return `服务器返回 ${status}`;
}

async function startPairing() {
  const parsed = parsePairingLink(els.link.value);
  if (!parsed) {
    showMsg("配对链接格式不正确:需要以 http(s) 开头且包含 ?p= 一次性码。", "error");
    return;
  }
  const authorizeUrl = authorizeUrlFromOrigin(parsed.origin);
  if (!authorizeUrl) {
    showMsg("配对链接中的服务器地址无效。", "error");
    return;
  }
  const deviceName = (els.deviceName.value || "").trim() || defaultDeviceName();
  setBusy(true);
  showMsg("正在配对…", "info");
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    let resp;
    try {
      resp = await fetch(authorizeUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: parsed.code,
          device_name: deviceName,
          extension_version: chrome.runtime.getManifest().version,
        }),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || typeof data.device_token !== "string" || !data.device_token) {
      showMsg(`配对失败:${describePairingError(resp.status, data && data.detail)}`, "error");
      return;
    }
    await state.setPairing({
      server_origin: parsed.origin,
      device_token: data.device_token,
      device_id: typeof data.device_id === "string" ? data.device_id : null,
      device_name:
        typeof data.device_name === "string" && data.device_name ? data.device_name : deviceName,
      token_expires_at: typeof data.token_expires_at === "string" ? data.token_expires_at : null,
      heartbeat_interval_s: clampHeartbeat(data.heartbeat_interval_s),
    });
    await notifyBackground({ type: "pairing_updated" });
    showMsg("配对成功,已保存凭证并开始连接。", "ok");
    await renderStatus();
  } catch (_) {
    showMsg("配对失败:无法连接服务器,请检查地址与网络后重试。", "error");
  } finally {
    setBusy(false);
  }
}

async function resetCredentials() {
  const confirmed = window.confirm("确定断开连接并清除本机配对凭证吗?清除后需要重新配对。");
  if (!confirmed) return;
  setBusy(true);
  try {
    await notifyBackground({ type: "reset_pairing" });
    // 本地兜底清理(消息送达失败时也能保证凭证被清掉)
    await state.clearLedger();
    await state.clearPairing();
    showMsg("已断开连接并清除凭证。", "ok");
    await renderStatus();
  } finally {
    setBusy(false);
  }
}

function formatExpiry(iso) {
  if (!iso) return "未知";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const expired = t <= Date.now();
  return `${expired ? "已于 " : ""}${new Date(t).toLocaleString()}${expired ? " 过期" : ""}`;
}

async function renderStatus() {
  const [pairing, runtime] = await Promise.all([state.getPairing(), state.getRuntimeStatus()]);
  const paired = Boolean(pairing.server_origin && pairing.device_token);
  els.origin.textContent = pairing.server_origin || "未配对";
  els.device.textContent = pairing.device_name || "-";
  if (!paired) {
    els.conn.textContent = "未配对";
  } else if (runtime && runtime.ws_state === "ready") {
    els.conn.textContent = "已连接";
  } else if (runtime && runtime.ws_state === "connecting") {
    els.conn.textContent = "连接中…";
  } else if (runtime && runtime.stop_reason === "revoked") {
    els.conn.textContent = "设备已吊销(请重新配对)";
  } else {
    els.conn.textContent = "离线";
  }
  els.expiry.textContent = formatExpiry(pairing.token_expires_at);
  if (!els.deviceName.value && pairing.device_name) {
    els.deviceName.value = pairing.device_name;
  }
}

els.version.textContent = `版本 v${chrome.runtime.getManifest().version}`;
els.pairBtn.addEventListener("click", () => {
  startPairing();
});
els.resetBtn.addEventListener("click", () => {
  resetCredentials();
});
renderStatus();
// 打开期间低频刷新连接状态
setInterval(renderStatus, 3000);
window.addEventListener("focus", renderStatus);
