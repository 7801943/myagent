/**
 * workspace.js — 工作区面板
 * 职责：折叠/展开、Tab 切换、根据 workspace_state 打开 OnlyOffice 文档。
 */

import { on } from './state.js';
import { closeDocument, openDocument } from './onlyoffice-editor.js';
import { send } from './connection.js';

let panel;
let chatPanel;
let layoutContainer;
let activeWorkspaceTab = 'empty';
let canvasTabsContainer;
let activeDocumentSignature = '';
let latestWorkspaceState = null;

const OFFICE_EXTENSIONS = new Set([
    '.doc', '.docx', '.odt', '.rtf', '.txt',
    '.xls', '.xlsx', '.ods', '.csv',
    '.ppt', '.pptx', '.odp',
    '.pdf',
]);

export function initWorkspace() {
    panel = document.getElementById("workspacePanel");
    chatPanel = document.getElementById("chatPanel");
    layoutContainer = document.querySelector(".main-layout-container");
    canvasTabsContainer = document.querySelector(".workspace-canvas-tabs");

    // Tab 切换 — 绑定到 activity-bar 中的 ws-tab-btn 按钮
    const abTabBtns = document.querySelectorAll(".ab-item.ws-tab-btn");
    abTabBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            switchWorkspaceTab(this.dataset.tab);
        });
    });

    // ── Layout Mode Toggle ──
    initLayoutToggle();

    // 恢复上次布局状态
    const savedLayout = localStorage.getItem("myagent-layout-mode") || "split";
    applyLayoutMode(savedLayout);

    // workspace_state 可能来自独立推送，也可能来自 conversation_state。
    on('workspace:state', handleWorkspaceState);
    on('auth:logout', function () {
        closeDocument();
        setDocumentTitle('文档预览');
        showOnlyOfficeHint('');
    });
}

// ── Workspace State ──

function handleWorkspaceState(workspaceState) {
    if (!workspaceState) return;
    latestWorkspaceState = normalizeWorkspaceState(workspaceState);

    renderFileTabs(latestWorkspaceState);

    const activeFile = getActiveFile(latestWorkspaceState);
    if (!activeFile || !isOnlyOfficeFile(activeFile.path)) {
        activeDocumentSignature = '';
        closeDocument();
        setDocumentTitle(activeFile ? fileName(activeFile.path) : '文档预览');
        showOnlyOfficeHint(activeFile ? '该文件类型暂不支持 OnlyOffice 预览' : '');
        return;
    }

    setDocumentTitle(fileName(activeFile.path));
    switchWorkspaceTab('editor');
    ensureWorkspaceVisible();

    const mode = activeFile.path.toLowerCase().endsWith('.pdf') ? 'view' : 'edit';
    const signature = getDocumentSignature(latestWorkspaceState, activeFile.path);
    const force = signature !== activeDocumentSignature;
    activeDocumentSignature = signature;
    openDocument(activeFile.path, mode, { force: force });
}

function getActiveFile(workspaceState) {
    const files = workspaceState.open_files || [];
    const index = workspaceState.active_file_index;
    if (typeof index === 'number' && index >= 0 && index < files.length) {
        return files[index];
    }
    return null;
}

function isOnlyOfficeFile(path) {
    const lower = (path || '').toLowerCase();
    const dot = lower.lastIndexOf('.');
    return dot >= 0 && OFFICE_EXTENSIONS.has(lower.slice(dot));
}

function fileName(path) {
    return (path || '').split('/').pop() || path || '文档预览';
}

function setDocumentTitle(title) {
    const titleEl = document.getElementById('workspaceDocumentTitle');
    if (titleEl) titleEl.textContent = title || '文档预览';
}

function showOnlyOfficeHint(message) {
    const statusEl = document.getElementById('onlyofficeStatus');
    if (statusEl) statusEl.textContent = message || '';
}

function ensureWorkspaceVisible() {
    if (!layoutContainer || !panel) return;
    const currentMode = localStorage.getItem("myagent-layout-mode") || "split";
    if (currentMode === 'chat-only') {
        applyLayoutMode('split');
        localStorage.setItem("myagent-layout-mode", 'split');
    }
}

function renderFileTabs(workspaceState) {
    if (!canvasTabsContainer) return;

    const files = workspaceState.open_files || [];
    const activeIndex = workspaceState.active_file_index;

    canvasTabsContainer.innerHTML = '';

    if (!files.length) {
        canvasTabsContainer.appendChild(createPlaceholderTab());
        canvasTabsContainer.appendChild(createAddButton());
        return;
    }

    files.forEach(function (file, index) {
        const tab = createFileTab(file, index, index === activeIndex);
        canvasTabsContainer.appendChild(tab);
    });
    canvasTabsContainer.appendChild(createAddButton());
}

function createFileTab(file, index, isActive) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'canvas-tab' + (isActive ? ' active' : '');
    tab.title = file.path;
    tab.dataset.index = String(index);

    const titleId = isActive ? ' id="workspaceDocumentTitle"' : '';
    tab.innerHTML = `
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
        </svg>
        <span${titleId}>${escapeHtml(fileName(file.path))}</span>
        <span class="canvas-tab-dirty" aria-hidden="true">${file.is_dirty ? '*' : ''}</span>
    `;

    tab.addEventListener('click', function () {
        activateFileTab(index);
    });

    const closeBtn = document.createElement('span');
    closeBtn.className = 'canvas-tab-close';
    closeBtn.title = '关闭';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function (event) {
        event.stopPropagation();
        closeFileTab(index);
    });
    tab.appendChild(closeBtn);

    return tab;
}

function createPlaceholderTab() {
    const tab = document.createElement('div');
    tab.className = 'canvas-tab active';
    tab.innerHTML = `
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
        </svg>
        <span id="workspaceDocumentTitle">文档预览</span>
    `;
    return tab;
}

function createAddButton() {
    const button = document.createElement('button');
    button.className = 'canvas-tab-add';
    button.type = 'button';
    button.title = '新建文档标签';
    button.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
    `;
    return button;
}

function activateFileTab(index) {
    if (!latestWorkspaceState) return;
    const files = latestWorkspaceState.open_files || [];
    if (index < 0 || index >= files.length) return;

    console.info('[Workspace] activating file tab', {
        index: index,
        path: files[index].path,
    });

    latestWorkspaceState = Object.assign({}, latestWorkspaceState, {
        active_file_index: index,
    });
    handleWorkspaceState(latestWorkspaceState);
    send({ type: 'workspace_set_active_file', index: index });
}

function closeFileTab(index) {
    if (!latestWorkspaceState) return;
    const files = latestWorkspaceState.open_files || [];
    if (index < 0 || index >= files.length) return;

    console.info('[Workspace] closing file tab', {
        index: index,
        path: files[index].path,
    });

    const nextFiles = files.slice();
    nextFiles.splice(index, 1);
    let nextActive = latestWorkspaceState.active_file_index;
    if (typeof nextActive === 'number') {
        if (index < nextActive) {
            nextActive -= 1;
        } else if (index === nextActive) {
            nextActive = nextFiles.length ? Math.min(index, nextFiles.length - 1) : null;
        }
    }

    latestWorkspaceState = Object.assign({}, latestWorkspaceState, {
        open_files: nextFiles,
        active_file_index: nextActive,
    });
    handleWorkspaceState(latestWorkspaceState);
    send({ type: 'workspace_close_file', index: index });
}

function normalizeWorkspaceState(workspaceState) {
    return Object.assign({}, workspaceState, {
        files: workspaceState.files || [],
        open_files: workspaceState.open_files || [],
    });
}

function getDocumentSignature(workspaceState, path) {
    const tab = (workspaceState.open_files || []).find(function (file) {
        return file.path === path;
    });
    const info = (workspaceState.files || []).find(function (file) {
        return file.path === path;
    });
    return [
        path,
        tab && typeof tab.revision === 'number' ? tab.revision : 0,
        info ? info.modified_at || '' : '',
        info ? info.size || 0 : 0,
    ].join('|');
}

function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, function (char) {
        return {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[char];
    });
}

function switchWorkspaceTab(target) {
    if (!target) return;
    activeWorkspaceTab = target;

    const abTabBtns = document.querySelectorAll(".ab-item.ws-tab-btn");
    const contents = document.querySelectorAll(".workspace-tab-content");

    // 更新 activity-bar 按钮状态
    abTabBtns.forEach(function (btn) {
        btn.classList.toggle("active", btn.dataset.tab === target);
    });

    // 更新 workspace 面板内容
    contents.forEach(function (content) {
        content.classList.toggle("active", content.dataset.tab === target);
    });
}

// ── Layout Toggle ──

function initLayoutToggle() {
    const layoutBtns = document.querySelectorAll(".layout-btn");

    layoutBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            const mode = this.dataset.layout;
            applyLayoutMode(mode);
            localStorage.setItem("myagent-layout-mode", mode);
        });
    });
}

function applyLayoutMode(mode) {
    if (!layoutContainer || !panel) return;

    // 更新按钮 active 状态
    const layoutBtns = document.querySelectorAll(".layout-btn");
    layoutBtns.forEach(function (btn) {
        btn.classList.toggle("active", btn.dataset.layout === mode);
    });

    // 清除所有布局类
    layoutContainer.classList.remove("layout-doc-only", "layout-split", "layout-chat-only");

    switch (mode) {
        case "doc-only":
            layoutContainer.classList.add("layout-doc-only");
            panel.classList.add("open");
            panel.style.width = "";
            break;
        case "chat-only":
            layoutContainer.classList.add("layout-chat-only");
            panel.classList.remove("open");
            panel.style.width = "0px";
            break;
        case "split":
        default:
            layoutContainer.classList.add("layout-split");
            panel.classList.add("open");
            panel.style.width = "50%";
            break;
    }
}

export function toggleWorkspace() {
    // Layout toggle replaces workspace toggle, cycle through modes
    if (!layoutContainer) return;
    const currentMode = localStorage.getItem("myagent-layout-mode") || "split";
    const modes = ["split", "doc-only", "chat-only"];
    const idx = modes.indexOf(currentMode);
    const nextMode = modes[(idx + 1) % modes.length];
    applyLayoutMode(nextMode);
    localStorage.setItem("myagent-layout-mode", nextMode);
}
