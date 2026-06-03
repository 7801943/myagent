/**
 * connection.js — WebSocket 连接管理
 * 职责：连接、重连、心跳、发送
 * 通过事件总线 emit('ws:message') 分发消息，不直接引入 router
 */

import { state, emit, MAX_RECONNECT, WS_URL } from './state.js';
import { getToken } from './auth.js';

let reconnectTimer = null;
let reconnectAttempts = 0;
let heartbeatTimer = null;

// ── 心跳 ──
function startHeartbeat() {
    stopHeartbeat();
    heartbeatTimer = setInterval(function () {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(JSON.stringify({ type: "ping" }));
        }
    }, 30000);
}

function stopHeartbeat() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
}

// ── 连接 ──
export function connect() {
    if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    try {
        // 携带认证 Token
        const token = getToken();
        const wsUrl = token ? `${WS_URL}?token=${encodeURIComponent(token)}` : WS_URL;
        state.ws = new WebSocket(wsUrl);
    } catch (e) {
        console.error("WebSocket creation failed:", e);
        emit('ws:error', { message: "无法创建 WebSocket 连接" });
        scheduleReconnect();
        return;
    }

    state.ws.onopen = function () {
        state.isConnected = true;
        reconnectAttempts = 0;
        startHeartbeat();
        emit('ws:open');
    };

    state.ws.onclose = function () {
        state.isConnected = false;
        stopHeartbeat();
        emit('ws:close');
        scheduleReconnect();
    };

    state.ws.onerror = function (err) {
        console.error("WebSocket error:", err);
        emit('ws:error', { message: "连接错误" });
    };

    state.ws.onmessage = function (event) {
        try {
            const data = JSON.parse(event.data);
            emit('ws:message', data);
        } catch (e) {
            console.error("Failed to parse WebSocket message:", e, event.data);
        }
    };
}

// ── 重连 ──
export function scheduleReconnect() {
    if (reconnectAttempts >= MAX_RECONNECT) {
        return;
    }
    reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 10000);

    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, delay);
}

// ── 发送 ──
export function send(data) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(typeof data === "string" ? data : JSON.stringify(data));
        return true;
    }
    return false;
}