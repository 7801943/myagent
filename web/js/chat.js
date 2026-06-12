/**
 * chat.js — 聊天核心：消息收发、DOM 操作、渲染状态
 * DOM 渲染状态内聚在本模块内部，不暴露给 state.js
 * 依赖：state.js, connection.js, markdown.js, utils.js, tool-chip.js
 */

import { state, emit, on } from './state.js';
import { send } from './connection.js';
import { renderMarkdown, renderMarkdownStream } from './markdown.js';
import { resetToolState, appendToolStart, appendToolEnd } from './tool-chip.js';
import { escapeHtml, scrollToEnd } from './utils.js';
import { getWorkspaceClientState } from './workspace.js';

// ── DOM 渲染状态（内聚在 chat.js 内部） ──
let currentAssistantEl = null;
let currentContentEl = null;
let currentThinkingList = null;
let currentThinkingItem = null;
let thinkingCount = 0;

// ── DOM 元素引用 ──
let messageList;
let welcomeMsg;
let userInput;
let sendBtn;
let stopBtn;
let clearBtn;
let statusIndicator;

export function initChat() {
    messageList = document.getElementById("messageList");
    userInput = document.getElementById("userInput");
    sendBtn = document.getElementById("sendBtn");
    stopBtn = document.getElementById("stopBtn");
    clearBtn = document.getElementById("clearBtn");
    statusIndicator = document.getElementById("statusIndicator");

    // Initialize empty state
    updateChatEmptyState(true);

    // 输入事件
    userInput.addEventListener("input", autoResizeInput);

    userInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage(userInput.value);
        }
    });

    sendBtn.addEventListener("click", function () {
        sendMessage(userInput.value);
    });

    stopBtn.addEventListener("click", function () {
        send({ type: "cancel" });
    });

    if (clearBtn) {
        clearBtn.addEventListener("click", function () {
            messageList.innerHTML = "";
            resetProcessingState();
            updateChatEmptyState(true);
        });
    }


    // 快捷提示
    document.querySelectorAll(".quick-prompt").forEach(function (btn) {
        btn.addEventListener("click", function () {
            const text = this.dataset.text;
            if (text) sendMessage(text);
        });
    });

    on('chat:send', function (payload) {
        const text = typeof payload === 'string' ? payload : (payload && payload.text);
        if (text) sendMessage(text);
    });
}

// ── 获取当前 assistant 元素（供 router 调用 tool-chip 时使用） ──
export function getCurrentAssistantEl() {
    return currentAssistantEl;
}

// ── 发送消息 ──
export function sendMessage(text) {
    if (!text || !text.trim()) return;
    if (!state.isConnected) {
        appendErrorMessage("未连接到服务器，请等待连接恢复");
        return;
    }
    if (state.isProcessing) return;

    text = text.trim();
    updateChatEmptyState(false);
    appendUserMessage(text);

    state.isProcessing = true;
    setStatus("processing", "处理中...");
    showStopButton();

    currentAssistantEl = createAssistantMessage();
    currentContentEl = currentAssistantEl.querySelector(".message-content");
    currentThinkingList = currentAssistantEl.querySelector(".msg-thinking-list");
    currentContentEl.classList.add("streaming-cursor");
    resetToolState();
    thinkingCount = 0;
    currentThinkingItem = null;

    send({ type: "chat", text: text, client_state: buildClientStateSnapshot() });
    userInput.value = "";
    autoResizeInput();
}

function buildClientStateSnapshot() {
    const workspace = getWorkspaceClientState();
    return {
        workspace: workspace || null,
    };
}

// ── 停止/恢复按钮 ──

export function showStopButton() {
    if (sendBtn) sendBtn.style.display = "none";
    if (stopBtn) stopBtn.style.display = "flex";
}

export function showSendButton() {
    if (stopBtn) stopBtn.style.display = "none";
    if (sendBtn) {
        sendBtn.style.display = "flex";
        sendBtn.disabled = !state.isConnected;
    }
}

// ── 状态显示 ──

export function setStatus(cls, text) {
    if (statusIndicator) {
        statusIndicator.className = "status-badge status-" + cls;
        statusIndicator.textContent = text;
    }

    // 通知 header 更新连接状态
    if (cls === "connected" || cls === "disconnected") {
        emit('header:conn-status', { cls, text });
    }
}

// ── DOM 操作函数 ──

export function appendUserMessage(text) {
    const el = document.createElement("div");
    el.className = "message message-user";
    el.innerHTML = `
        <div class="message-avatar">U</div>
        <div class="message-body">
            <div class="message-content">${escapeHtml(text)}</div>
        </div>
    `;
    messageList.appendChild(el);
    scrollToEnd(true);
}

export function createAssistantMessage() {
    const el = document.createElement("div");
    el.className = "message message-assistant";
    el.innerHTML = `
        <div class="message-avatar">AI</div>
        <div class="message-body">
            <div class="msg-section msg-thinking-section" style="display:none;">
                <div class="msg-section-label">💭 Thinking</div>
                <div class="msg-thinking-list"></div>
            </div>
            <div class="msg-section msg-tools-section" style="display:none;">
                <div class="msg-tools-container"></div>
            </div>
            <div class="msg-section msg-reply-section">
                <div class="message-content"></div>
            </div>
        </div>
    `;

    messageList.appendChild(el);
    scrollToEnd(true);
    return el;
}

export function appendTextDelta(text) {
    if (!currentContentEl) return;
    currentContentEl.classList.remove("streaming-cursor");
    let rawText = currentContentEl._rawText || "";
    rawText += text;
    currentContentEl._rawText = rawText;
    // 流式渲染（不使用 KaTeX，提升性能）
    currentContentEl.innerHTML = renderMarkdownStream(rawText);
    const replySection = currentContentEl.closest(".msg-reply-section");
    if (replySection) replySection.style.display = "";

    if (state.isProcessing) {
        currentContentEl.classList.add("streaming-cursor");
    }
    scrollToEnd(false);
}

export function forceNewLine() {
    if (currentContentEl && currentContentEl._rawText && !currentContentEl._rawText.endsWith('\n\n')) {
        if (currentContentEl._rawText.endsWith('\n')) {
            appendTextDelta('\n');
        } else {
            appendTextDelta('\n\n');
        }
    }
}

export function appendThinkingDelta(text) {
    if (!currentThinkingList) return;
    forceNewLine();

    const thinkingSection = currentThinkingList.closest(".msg-thinking-section");
    if (thinkingSection) thinkingSection.style.display = "";

    if (!currentThinkingItem) {
        thinkingCount++;
        currentThinkingItem = document.createElement("div");
        currentThinkingItem.className = "msg-thinking-item";
        currentThinkingItem.innerHTML = `
            <span class="thinking-num">#${thinkingCount}</span>
            <span class="thinking-text"></span>
        `;
        currentThinkingList.appendChild(currentThinkingItem);
    }

    const textSpan = currentThinkingItem.querySelector(".thinking-text");
    textSpan.textContent += text;

    currentThinkingList.scrollTop = currentThinkingList.scrollHeight;
    scrollToEnd(false);
}

// ── 历史消息加载 ──

export function loadHistoryMessages(messages) {
    messageList.innerHTML = "";

    if (messages && messages.length > 0) {
        updateChatEmptyState(false);
    } else {
        updateChatEmptyState(true);
    }

    let hasActiveAssistant = false;

    messages.forEach(function (msg) {
        try {
            if (msg.role === "user") {
                appendUserMessage(msg.content);
                hasActiveAssistant = false;
            } else if (msg.role === "assistant") {
                if (!hasActiveAssistant) {
                    currentAssistantEl = createAssistantMessage();
                    currentContentEl = currentAssistantEl.querySelector(".message-content");
                    currentThinkingList = currentAssistantEl.querySelector(".msg-thinking-list");
                    resetToolState();
                    thinkingCount = 0;
                    currentThinkingItem = null;
                    hasActiveAssistant = true;
                }

                if (msg.metadata && msg.metadata.thinking) {
                    appendThinkingDelta(msg.metadata.thinking);
                    currentThinkingItem = null;
                }

                if (msg.tool_calls && msg.tool_calls.length > 0) {
                    msg.tool_calls.forEach(function (tc, idx) {
                        let args = tc.arguments;
                        if (typeof args === "string") {
                            try { args = JSON.parse(args); } catch (e) { }
                        }
                        appendToolStart(currentAssistantEl, tc.name || "tool", args, tc.id || ("hist_" + idx));
                    });
                }

                if (msg.content) {
                    let rawText = currentContentEl._rawText || "";
                    rawText += msg.content;
                    currentContentEl._rawText = rawText;
                    currentContentEl.innerHTML = renderMarkdown(rawText);
                    const replySection = currentContentEl.closest(".msg-reply-section");
                    if (replySection) replySection.style.display = "";
                }
            } else if (msg.role === "tool") {
                if (hasActiveAssistant) {
                    let latency = 0;
                    if (msg.metadata && msg.metadata.latency_ms) latency = msg.metadata.latency_ms;
                    let toolName = msg.tool_name || "tool";
                    let callId = msg.tool_call_id || "";
                    appendToolEnd(toolName, msg.content, latency, callId);
                }
            }
        } catch (e) {
            console.error("Error loading history message:", e, msg);
        }
    });

    resetProcessingState();
    scrollToEnd(true);
}

// ── 统一的 resetProcessingState（收敛所有重复代码） ──

export function resetProcessingState() {
    currentAssistantEl = null;
    currentContentEl = null;
    currentThinkingList = null;
    currentThinkingItem = null;
    thinkingCount = 0;
    resetToolState();
    state.isProcessing = false;
    showSendButton();
}

// ── 消息完成 ──

export function finalizeAssistantMessage(stopReason) {
    if (currentContentEl) {
        currentContentEl.classList.remove("streaming-cursor");
        const rawText = currentContentEl._rawText || "";
        if (rawText) {
            currentContentEl.innerHTML = renderMarkdown(rawText);
        }
    }

    if (stopReason === "cancelled") {
        if (currentContentEl && !currentContentEl._rawText) {
            currentContentEl.innerHTML = '<p style="color: var(--text-tertiary); font-style: italic;">⏹ 已停止</p>';
        }
    }

    if (stopReason !== "cancelled") {
        // 刷新会话列表以更新标题
        emit('session:requestList');
    }

    resetProcessingState();
    setStatus("connected", "空闲");
    scrollToEnd(true);
}

// ── 错误消息 ──

export function appendErrorMessage(message) {
    if (welcomeMsg) welcomeMsg.classList.add("hidden");
    const el = document.createElement("div");
    el.className = "message-error";
    el.innerHTML = `
        <div class="message-content">❌ ${escapeHtml(message)}</div>
    `;
    messageList.appendChild(el);
    scrollToEnd(true);
}

// ── 输入自适应 ──

function autoResizeInput() {
    if (!userInput) return;
    userInput.style.height = "auto";
    userInput.style.height = Math.min(userInput.scrollHeight, 120) + "px";

    const wrapper = userInput.closest(".input-card");
    if (wrapper) {
        if (userInput.value.length > 0) {
            wrapper.classList.add("has-content");
        } else {
            wrapper.classList.remove("has-content");
        }
    }
}

export function updateChatEmptyState(isEmpty) {
    const chatPanel = document.getElementById("chatPanel");
    const centeredTitle = document.getElementById("centeredChatTitle");

    if (centeredTitle) {
        centeredTitle.textContent = "欢迎";
    }

    if (chatPanel) {
        if (isEmpty) {
            chatPanel.classList.add("empty-chat");
            chatPanel.classList.remove("has-chat");
        } else {
            chatPanel.classList.remove("empty-chat");
            chatPanel.classList.add("has-chat");
        }
    }
}