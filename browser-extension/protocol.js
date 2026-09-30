// protocol.js — 语析 Browser Gateway 扩展侧协议常量与帧构造/校验工具。

// 协议版本:与服务端 Browser Gateway 约定一致,帧内 `v` 字段即此值。
export const PROTOCOL_VERSION = 1;

// 单帧上限 4MB(与服务端一致);超出上限的回复必须降级为错误帧。
export const MAX_FRAME_BYTES = 4 * 1024 * 1024;

// 连接建立后等待 hello_ok 的时长,超时视为握手失败,按退避重连。
export const HELLO_TIMEOUT_MS = 5000;

// 心跳死链阈值:连续 45 秒未收到任何服务端帧即判定连接已死,主动重连。
export const DEAD_LINK_MS = 45000;

// 配对响应未提供 heartbeat_interval_s 时的默认心跳间隔。
export const DEFAULT_HEARTBEAT_INTERVAL_S = 10;

export const WS_PATH = "/api/browser/extension/ws";
export const AUTHORIZE_PATH = "/api/browser/extension/authorize";
export const PAIRING_LINK_PATH = "/api/browser/pairing";

// hello 必须是连接后的第一帧;收到 hello_ok 之前不得发送任何其他帧。
export function makeHello(extensionVersion, deviceId, platform, userAgent) {
  return {
    v: PROTOCOL_VERSION,
    type: "hello",
    protocol: PROTOCOL_VERSION,
    extension_version: extensionVersion,
    device_id: deviceId || "",
    platform: platform,
    user_agent: userAgent || "",
  };
}

export function makeHeartbeat() {
  return { v: PROTOCOL_VERSION, type: "heartbeat", ts: Date.now() };
}

export function makeResultOk(id, result) {
  return {
    v: PROTOCOL_VERSION,
    id: id,
    type: "result",
    ok: true,
    result: result === undefined ? {} : result,
  };
}

export function makeResultError(id, code, message) {
  return {
    v: PROTOCOL_VERSION,
    id: id,
    type: "result",
    ok: false,
    error: { code: code, message: message || "" },
  };
}

export function isHelloOk(frame) {
  return Boolean(frame) && frame.type === "hello_ok";
}

export function isTokenRotated(frame) {
  return Boolean(frame) && frame.type === "token_rotated";
}

export function isCmdFrame(frame) {
  return (
    Boolean(frame) &&
    frame.type === "cmd" &&
    typeof frame.id === "string" &&
    typeof frame.op === "string"
  );
}

// 解析入站帧:非 JSON、非对象、版本不符一律判为无效(仅当作活性信号处理)。
export function parseFrame(raw) {
  let frame;
  try {
    frame = JSON.parse(raw);
  } catch (_) {
    return { ok: false, reason: "not_json" };
  }
  if (!frame || typeof frame !== "object" || Array.isArray(frame)) {
    return { ok: false, reason: "not_object" };
  }
  if (frame.v !== PROTOCOL_VERSION) {
    return { ok: false, reason: "bad_version" };
  }
  if (typeof frame.type !== "string") {
    return { ok: false, reason: "bad_type" };
  }
  return { ok: true, frame };
}

export function frameByteLength(frame) {
  return new TextEncoder().encode(JSON.stringify(frame)).length;
}

// http(s) origin → ws(s) 网关地址;非法 origin 返回 null。
export function gatewayUrlFromOrigin(origin) {
  if (typeof origin !== "string" || !/^https?:\/\//i.test(origin)) return null;
  try {
    const url = new URL(WS_PATH, origin);
    // http→ws、https→wss:替换协议前缀的 "http" 部分
    return url.href.replace(/^http/i, "ws");
  } catch (_) {
    return null;
  }
}

export function authorizeUrlFromOrigin(origin) {
  if (typeof origin !== "string" || !/^https?:\/\//i.test(origin)) return null;
  try {
    return new URL(AUTHORIZE_PATH, origin).href;
  } catch (_) {
    return null;
  }
}

// 解析配对链接:http://host[:port]/api/browser/pairing?p=<一次性码>
// 返回 { origin, code };格式不符返回 null。
export function parsePairingLink(link) {
  if (typeof link !== "string") return null;
  let url;
  try {
    url = new URL(link.trim());
  } catch (_) {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const code = url.searchParams.get("p");
  if (!code) return null;
  return { origin: url.origin, code: code };
}
