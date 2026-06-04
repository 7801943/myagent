/**
 * router.js — WebSocket 消息路由
 * 独立监听 state.js 事件总线上的 ws:message 事件
 * 不被 connection.js 直接 import，避免循环依赖
 */

import { state, on, emit } from './state.js';
import { requestSessionList, createNewSession } from './session.js';
import { resetProcessingState, loadHistoryMessages, finalizeAssistantMessage, appendTextDelta, appendThinkingDelta, appendErrorMessage, setStatus, getCurrentAssistantEl, updateChatEmptyState } from './chat.js';
import { appendToolStart, appendToolEnd, appendToolError, appendSafetyBlocked, showHitlApproval } from './tool-chip.js';
import { updateContextProgress, setStateAnimation } from './context-bar.js';
import { handleConversationState, handleStateChange, updateSessionList } from './header.js';
import { STATE_MAP } from './utils.js';

function handleMessage(data) {
    const type = data.type;

    switch (type) {
        case "connected":
            state.currentSessionId = data.session_id;
            state.contextWindowSize = data.context_window_size || 0;
            console.log("Session:", data.session_id, "Context Window:", state.contextWindowSize);

            resetProcessingState();
            setStatus("connected", "空闲");

            // 过滤出真正需要显示的聊天消息
            const displayableMessages = (data.messages || []).filter(function (msg) {
                return msg.role === "user" || msg.role === "assistant";
            });

            if (displayableMessages.length > 0) {
                const welcomeMsg = document.getElementById("welcomeMsg");
                if (welcomeMsg) welcomeMsg.classList.add("hidden");
                loadHistoryMessages(data.messages);
            } else {
                const messageList = document.getElementById("messageList");
                const welcomeMsg = document.getElementById("welcomeMsg");
                if (messageList) messageList.innerHTML = "";
                if (welcomeMsg) welcomeMsg.classList.remove("hidden");
                updateChatEmptyState(true);
            }

            requestSessionList();
            break;

        case "session_list_result":
            state.sessions = data.sessions || [];
            updateSessionList();
            break;

        case "session_created":
            state.currentSessionId = data.session_id;
            {
                const messageList = document.getElementById("messageList");
                const welcomeMsg = document.getElementById("welcomeMsg");
                if (messageList) messageList.innerHTML = "";
                if (welcomeMsg) welcomeMsg.classList.remove("hidden");
            }
            updateChatEmptyState(true);
            resetProcessingState();
            requestSessionList();
            break;

        case "session_switched":
            state.currentSessionId = data.session_id;
            resetProcessingState();
            {
                const welcomeMsg = document.getElementById("welcomeMsg");
                if (welcomeMsg) welcomeMsg.classList.add("hidden");
            }
            loadHistoryMessages(data.messages || []);
            requestSessionList();
            break;

        case "session_deleted":
            state.sessions = state.sessions.filter(function (s) { return s.session_id !== data.session_id; });
            if (data.session_id === state.currentSessionId) {
                createNewSession();
            }
            updateSessionList();
            break;

        case "state_change":
            handleStateChange(data.state);
            setStateAnimation(data.state);
            break;

        case "text_delta":
            appendTextDelta(data.text || "");
            break;

        case "thinking_delta":
            appendThinkingDelta(data.text || "");
            break;

        case "stream_start":
            break;

        case "stream_end":
            if (data.resuming) {
                // stream_end 时 resuming 不需要额外操作
                // currentThinkingItem 重置由 chat.js 在适当时机处理
            }
            break;

        case "tool_start":
            {
                const assistantEl = getCurrentAssistantEl();
                appendToolStart(assistantEl, data.tool_name, data.args, data.call_id);
            }
            break;

        case "tool_end":
            appendToolEnd(data.tool_name, data.result, data.latency_ms, data.call_id);
            break;

        case "tool_error":
            appendToolError(data.tool_name, data.error, data.call_id);
            break;

        case "safety_blocked":
            {
                const assistantEl = getCurrentAssistantEl();
                appendSafetyBlocked(assistantEl, data.rule, data.reason, data.action, data.call_id, data.tool_name);
            }
            break;

        case "hitl_request":
            {
                const assistantEl = getCurrentAssistantEl();
                showHitlApproval(assistantEl, data.tool_name, data.reason, data.args, data.call_id);
            }
            break;

        case "message_end":
            if (data.context_usage) {
                updateContextProgress(data.context_usage);
            }
            finalizeAssistantMessage(data.stop_reason || "completed");
            requestSessionList();
            break;

        case "error":
            handleError(data.message);
            break;

        case "conversation_state":
            handleConversationState(data);
            if (data.workspace_state) {
                emit('workspace:state', data.workspace_state);
            }
            break;

        case "workspace_state":
            emit('workspace:state', data);
            break;

        case "pong":
            break;

        default:
            console.warn("Unknown message type:", type, data);
    }
}

/**
 * 错误处理 — 作为消息路由的异常逻辑
 */
function handleError(message) {
    // 从 chat.js 导入的 resetProcessingState 会处理 DOM 清理
    // 这里额外处理错误显示
    appendErrorMessage(message);
    resetProcessingState();
    setStatus("connected", "空闲");
}

/**
 * 初始化路由：监听 ws:message 事件
 */
export function initRouter() {
    on('ws:message', handleMessage);
}