/**
 * header.js — Header UI、Sidebar、连接状态、会话列表渲染
 * 依赖：state.js, session.js, utils.js
 */

import { state, on } from './state.js';
import { escapeHtml, STATE_MAP } from './utils.js';
import { switchSession, deleteSession } from './session.js';
import { send } from './connection.js';

// ── DOM 元素 ──
let sidebar;
let sidebarToggle;
let newChatBtn;
let sessionListEl;
let headerConnDot;
let headerConnText;
let headerModelName;
let modelPicker;
let modelPickerButton;
let modelPickerLabel;
let modelPickerPopup;
let statusIndicator;
let fullscreenToggle;

export function initHeader() {
    sidebar = document.querySelector(".sidebar");
    sidebarToggle = document.getElementById("sidebarToggle");
    newChatBtn = document.getElementById("newChatBtn");
    sessionListEl = document.getElementById("sessionList");
    headerConnDot = document.getElementById("headerConnDot");
    headerConnText = document.getElementById("headerConnText");
    headerModelName = document.getElementById("headerModelName");
    modelPicker = document.getElementById("modelPicker");
    modelPickerButton = document.getElementById("modelPickerButton");
    modelPickerLabel = document.getElementById("modelPickerLabel");
    modelPickerPopup = document.getElementById("modelPickerPopup");
    statusIndicator = document.getElementById("statusIndicator");
    fullscreenToggle = document.getElementById("fullscreenToggle");

    // Sidebar 初始化
    initSidebar();
    initFullscreenToggle();
    initModelPicker();

    // 新建会话按钮
    const newChatHeaderBtn = document.getElementById("newChatHeaderBtn");
    const triggerNewChat = function () {
        import('./session.js').then(function (m) {
            m.createNewSession();
        });
    };
    if (newChatBtn) {
        newChatBtn.addEventListener("click", triggerNewChat);
    }
    if (newChatHeaderBtn) {
        newChatHeaderBtn.addEventListener("click", triggerNewChat);
    }

    // 监听连接状态事件（来自 chat.js setStatus）
    on('header:conn-status', function (data) {
        if (headerConnDot) {
            headerConnDot.className = "header-conn-dot" + (data.cls === "connected" ? " connected" : "");
        }
        if (headerConnText) {
            headerConnText.textContent = data.cls === "connected" ? "已连接" : "未连接";
        }
    });

    // 监听 WebSocket 打开/关闭事件
    on('ws:open', function () {
        updateHeaderConnStatus("connected", "已连接");
        updateModelPickerDisabled();
    });

    on('ws:close', function () {
        updateHeaderConnStatus("disconnected", "未连接");
        closeModelPicker();
        updateModelPickerDisabled();
    });

    on('ws:error', function (data) {
        updateHeaderConnStatus("disconnected", data.message || "连接错误");
        closeModelPicker();
        updateModelPickerDisabled();
    });

    on('processing:changed', function () {
        updateModelPickerDisabled();
    });
}

function updateHeaderConnStatus(cls, text) {
    if (headerConnDot) {
        headerConnDot.className = "header-conn-dot" + (cls === "connected" ? " connected" : "");
    }
    if (headerConnText) {
        headerConnText.textContent = text;
    }
    // 动态切换标题栏渐变色
    updateHeaderGradient(cls === "connected");
}

function updateHeaderGradient(isConnected) {
    const header = document.querySelector(".global-header");
    if (!header) return;
    if (isConnected) {
        header.classList.add("connected");
    } else {
        header.classList.remove("connected");
    }
}

// ── Sidebar Toggle ──

export function toggleSidebar() {
    if (!sidebar) return;
    const isCollapsed = sidebar.classList.contains("collapsed");

    if (isCollapsed) {
        sidebar.classList.remove("collapsed");
        localStorage.setItem("myagent-sidebar", "expanded");
        if (sidebarToggle) sidebarToggle.classList.add("active");
        // Mutual exclusion: collapse workspace sidebar when session sidebar opens
        var wsSidebar = document.getElementById("workspaceSidebar");
        if (wsSidebar && !wsSidebar.classList.contains("collapsed")) {
            wsSidebar.classList.add("collapsed");
            var wsToggle = document.getElementById("workspaceExplorerToggle");
            if (wsToggle) wsToggle.classList.remove("active");
        }
    } else {
        sidebar.classList.add("collapsed");
        localStorage.setItem("myagent-sidebar", "collapsed");
        if (sidebarToggle) sidebarToggle.classList.remove("active");
    }
}


function collapseSidebar() {
    if (!sidebar || sidebar.classList.contains("collapsed")) return;
    sidebar.classList.add("collapsed");
    localStorage.setItem("myagent-sidebar", "collapsed");
    if (sidebarToggle) sidebarToggle.classList.remove("active");
}

function initFullscreenToggle() {
    if (!fullscreenToggle) return;

    fullscreenToggle.addEventListener("click", function () {
        if (document.fullscreenElement) {
            document.exitFullscreen().catch(function (err) {
                console.error("Exit fullscreen failed:", err);
            });
            return;
        }

        document.documentElement.requestFullscreen().catch(function (err) {
            console.error("Enter fullscreen failed:", err);
        });
    });

    document.addEventListener("fullscreenchange", updateFullscreenButton);
    updateFullscreenButton();
}

function updateFullscreenButton() {
    if (!fullscreenToggle) return;
    const isFullscreen = !!document.fullscreenElement;
    fullscreenToggle.classList.toggle("is-fullscreen", isFullscreen);
    fullscreenToggle.title = isFullscreen ? "恢复窗口" : "全屏";
    fullscreenToggle.setAttribute("aria-label", isFullscreen ? "恢复窗口" : "全屏");
}

function updateModelDisplays(modelState) {
    const active = modelState.active || {};
    const available = modelState.available || [];
    const modelId = active.model_id || "";
    const providerType = active.provider_type || "";
    const providerName = active.provider_name || "";
    const label = modelId || "等待模型状态";
    const titleParts = [providerType, providerName, modelId].filter(Boolean);
    const title = titleParts.length ? titleParts.join(" / ") : label;

    if (headerModelName) {
        headerModelName.textContent = modelId;
        headerModelName.title = title;
        headerModelName.classList.toggle("visible", !!modelId);
    }

    if (modelPickerLabel) {
        modelPickerLabel.textContent = label;
    }
    if (modelPickerButton) {
        modelPickerButton.title = title;
        modelPickerButton.setAttribute("aria-label", "当前模型：" + label);
    }
    if (modelPicker) modelPicker.title = title;
    renderModelPickerPopup(available, active);
    updateModelPickerDisabled();
}

function initModelPicker() {
    if (!modelPicker || !modelPickerButton || !modelPickerPopup) return;

    modelPickerButton.addEventListener("click", function (event) {
        event.stopPropagation();
        if (isModelPickerDisabled()) return;
        const isOpen = !modelPickerPopup.hidden;
        if (isOpen) {
            closeModelPicker();
        } else {
            openModelPicker();
        }
    });

    document.addEventListener("click", function (event) {
        if (modelPicker && !modelPicker.contains(event.target)) {
            closeModelPicker();
        }
    });
}

function isModelPickerDisabled() {
    return !state.isConnected || state.isProcessing || !(state.model.available || []).length;
}

function updateModelPickerDisabled() {
    if (!modelPickerButton || !modelPicker) return;
    const disabled = isModelPickerDisabled();
    modelPickerButton.disabled = disabled;
    modelPicker.classList.toggle("disabled", disabled);
    if (disabled) closeModelPicker();
}

function openModelPicker() {
    if (!modelPickerPopup) return;
    renderModelPickerPopup(state.model.available || [], state.model.active || {});
    modelPickerPopup.hidden = false;
    if (modelPickerButton) {
        modelPickerButton.setAttribute("aria-expanded", "true");
    }
}

function closeModelPicker() {
    if (!modelPickerPopup) return;
    modelPickerPopup.hidden = true;
    if (modelPickerButton) {
        modelPickerButton.setAttribute("aria-expanded", "false");
    }
}

function renderModelPickerPopup(available, active) {
    if (!modelPickerPopup) return;
    modelPickerPopup.innerHTML = "";

    if (!available.length) {
        const empty = document.createElement("div");
        empty.className = "model-picker-empty";
        empty.textContent = "暂无可选模型";
        modelPickerPopup.appendChild(empty);
        return;
    }

    available.forEach(function (model) {
        const row = document.createElement("div");
        const isActive = model.provider_key === active.provider_key;
        row.className = "model-picker-item" + (isActive ? " active" : "");
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        row.title = [model.provider_name, model.model_id].filter(Boolean).join(" / ");

        const text = document.createElement("div");
        text.className = "model-picker-item-text";

        const name = document.createElement("div");
        name.className = "model-picker-item-name";
        name.textContent = model.model_id || "未知模型";

        const meta = document.createElement("div");
        meta.className = "model-picker-item-meta";
        meta.textContent = model.provider_name || model.provider_type || "";

        text.appendChild(name);
        text.appendChild(meta);

        const thinking = document.createElement("button");
        thinking.type = "button";
        thinking.className = "model-thinking-switch" + (model.thinking_enabled ? " on" : "");
        thinking.disabled = !model.thinking_supported || isModelPickerDisabled();
        thinking.title = model.thinking_supported ? "切换 Thinking" : "该模型不支持 Thinking";
        thinking.setAttribute("aria-label", "Thinking");
        thinking.innerHTML = '<span class="model-thinking-label">Thinking</span><span class="model-thinking-track"><span class="model-thinking-thumb"></span></span>';

        row.addEventListener("click", function () {
            selectModel(model, model.thinking_enabled);
        });
        row.addEventListener("keydown", function (event) {
            if (event.target !== row) return;
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                selectModel(model, model.thinking_enabled);
            }
        });
        thinking.addEventListener("click", function (event) {
            event.stopPropagation();
            if (!model.thinking_supported || isModelPickerDisabled()) return;
            selectModel(model, !model.thinking_enabled);
        });

        row.appendChild(text);
        row.appendChild(thinking);
        modelPickerPopup.appendChild(row);
    });
}

function selectModel(model, thinkingEnabled) {
    if (!model || isModelPickerDisabled()) return;
    closeModelPicker();
    send({
        type: "model_select",
        provider_key: model.provider_key,
        thinking_enabled: Boolean(thinkingEnabled),
    });
}

function initSidebar() {
    if (!sidebar) return;
    const saved = localStorage.getItem("myagent-sidebar");

    if (saved === "collapsed") {
        sidebar.classList.add("collapsed");
        if (sidebarToggle) sidebarToggle.classList.remove("active");
    } else {
        sidebar.classList.remove("collapsed");
        if (sidebarToggle) sidebarToggle.classList.add("active");
    }

    if (sidebarToggle) {
        sidebarToggle.addEventListener("click", function () {
            toggleSidebar();
        });
    }
}

// ── 会话列表渲染 ──

export function updateSessionList() {
    if (!sessionListEl) return;
    sessionListEl.innerHTML = "";
    state.sessions.forEach(function (s) {
        const item = document.createElement("div");
        item.className = "session-item" + (s.session_id === state.currentSessionId ? " active" : "");
        item.dataset.sessionId = s.session_id;
        item.innerHTML = `
            <span class="session-item-icon">💬</span>
            <span class="session-item-title">${escapeHtml(s.title || "新对话")}</span>
            <button class="session-item-delete" title="删除会话" data-id="${s.session_id}">✕</button>
        `;
        // 点击切换
        item.addEventListener("click", function (e) {
            if (e.target.classList.contains("session-item-delete")) return;
            if (s.session_id !== state.currentSessionId && !state.isProcessing) {
                switchSession(s.session_id);
                collapseSidebar();
            }
        });
        // 删除按钮
        const delBtn = item.querySelector(".session-item-delete");
        delBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            const title = s.title || "新对话";
            if (window.confirm(`确定要删除会话「${title}」吗？此操作不可撤销。`)) {
                deleteSession(s.session_id);
            }
        });
        sessionListEl.appendChild(item);
    });
}

// ── Agent 状态变化 ──

export function handleStateChange(agentState) {
    const info = STATE_MAP[agentState];
    if (!info) {
        console.warn("Unknown agent state:", agentState);
        return;
    }

    if (statusIndicator) {
        statusIndicator.className = "status-badge " + info.css;
        statusIndicator.textContent = info.text;
    }
    updateModelPickerDisabled();
}

// ── 会话状态 ──

export function handleConversationState(data) {
    state.model = data.model || { active: {}, available: [] };
    updateModelDisplays(state.model);
    state.safetyPolicy = data.safety || {
        active_policy: "",
        available_policies: [],
        mode: "",
    };

    // 更新上下文进度条（通过 context-bar.js）
    if (data.context && data.context.token_usage) {
        const tu = data.context.token_usage;
        // 直接导入避免循环依赖，通过事件通知
        import('./context-bar.js').then(function (m) {
            m.updateContextProgress({
                used_tokens: tu.used || 0,
                context_window_size: tu.total || state.contextWindowSize || 1,
            });
        });

        if (tu.total) {
            state.contextWindowSize = tu.total;
        }
    }
}
