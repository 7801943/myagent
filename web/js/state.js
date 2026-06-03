/**
 * state.js — 共享状态仓库 + 简易事件总线
 * 只管理连接/会话级状态，DOM 渲染状态内聚到各模块内部
 */

// ── 共享状态 ──
export const state = {
    ws: null,
    isConnected: false,
    isProcessing: false,
    currentSessionId: null,
    sessions: [],
    contextWindowSize: 0,
    // 认证状态
    isAuthenticated: false,
    authToken: '',
    authUsername: '',
};

// ── 常量 ──
export const MAX_RECONNECT = 10;
export const WS_URL = `ws://${window.location.host}/ws`;

// ── 事件总线 ──
const listeners = {};

export function on(event, handler) {
    if (!listeners[event]) listeners[event] = [];
    listeners[event].push(handler);
}

export function off(event, handler) {
    if (!listeners[event]) return;
    listeners[event] = listeners[event].filter(h => h !== handler);
}

export function emit(event, data) {
    if (!listeners[event]) return;
    listeners[event].forEach(handler => {
        try {
            handler(data);
        } catch (e) {
            console.error(`Event handler error [${event}]:`, e);
        }
    });
}