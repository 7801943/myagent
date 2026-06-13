/**
 * header.js — Header UI、Sidebar、连接状态、会话列表渲染
 * 依赖：state.js, session.js, utils.js
 */

import { state, on } from './state.js';
import { escapeHtml, STATE_MAP } from './utils.js';
import { switchSession, deleteSession } from './session.js';

// ── DOM 元素 ──
let sidebar;
let sidebarToggle;
let newChatBtn;
let sessionListEl;
let headerConnDot;
let headerConnText;
let headerModelName;
let modelSelect;
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
    modelSelect = document.getElementById("modelSelect");
    statusIndicator = document.getElementById("statusIndicator");
    fullscreenToggle = document.getElementById("fullscreenToggle");

    // Sidebar 初始化
    initSidebar();
    initFullscreenToggle();

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
    });

    on('ws:close', function () {
        updateHeaderConnStatus("disconnected", "未连接");
    });

    on('ws:error', function (data) {
        updateHeaderConnStatus("disconnected", data.message || "连接错误");
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

function updateModelDisplays(active) {
    const modelId = active.model_id || "";
    const providerType = active.provider_type || "";
    const label = modelId || "等待模型状态";
    const title = providerType && modelId ? (providerType + " / " + modelId) : label;

    if (headerModelName) {
        headerModelName.textContent = modelId;
        headerModelName.title = title;
        headerModelName.classList.toggle("visible", !!modelId);
    }

    if (modelSelect) {
        modelSelect.innerHTML = "";
        const option = document.createElement("option");
        option.value = modelId;
        option.textContent = label;
        option.selected = true;
        modelSelect.appendChild(option);
        modelSelect.title = title;
    }
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
}

// ── 会话状态 ──

export function handleConversationState(data) {
    updateModelDisplays((data.model && data.model.active) || {});
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