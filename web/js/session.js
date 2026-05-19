/**
 * session.js — 会话 CRUD 业务逻辑
 * 依赖：state.js, connection.js
 */

import { state } from './state.js';
import { send } from './connection.js';

export function requestSessionList() {
    send({ type: "session_list" });
}

export function createNewSession() {
    if (state.isProcessing) return;
    send({ type: "session_create" });
}

export function switchSession(sessionId) {
    if (state.isProcessing) return;
    send({ type: "session_switch", session_id: sessionId });
}

export function deleteSession(sessionId) {
    if (state.isProcessing) return;
    send({ type: "session_delete", session_id: sessionId });
}