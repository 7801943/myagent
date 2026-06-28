/**
 * workspace.js — 工作区面板
 * 职责：折叠/展开、Tab 切换、根据 workspace_state 打开 OnlyOffice 文档。
 */

import { state, emit, on } from './state.js';
import { activateDocument, closeAllDocuments, closeDocument, openDocument } from './onlyoffice-editor.js';
import { send } from './connection.js';
import { getToken } from './auth.js';

let panel;
let chatPanel;
let layoutContainer;
let activeWorkspaceTab = 'empty';
let canvasTabsContainer;
let activeDocumentSignature = '';
let activeDocumentPath = '';
let latestWorkspaceState = null;
let workspaceSidebar;
let workspaceExplorerToggle;
let explorerContainer;
let selectedWorkspacePath = '';
let workspaceContextMenu = null;
let tabListPopup = null;
let lastRenderedTreeSignature = '';
let optimisticActiveFilePath = '';
let optimisticActiveRequestedAt = 0;

const OPTIMISTIC_ACTIVE_TTL_MS = 4000;

const OFFICE_EXTENSIONS = new Set([
    '.doc', '.docx', '.odt', '.rtf', '.txt', '.md', '.markdown',
    '.xls', '.xlsx', '.ods', '.csv',
    '.ppt', '.pptx', '.odp',
    '.pdf',
]);

const FORBIDDEN_ARCHIVE_SUFFIXES = [
    '.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.zst',
    '.cab', '.iso', '.jar', '.war', '.ear', '.apk', '.dmg',
];

let workspaceUploadFileInput = null;
let workspaceUploadFolderInput = null;
let pendingUploadTargetDir = '';

export function initWorkspace() {
    panel = document.getElementById("workspacePanel");
    chatPanel = document.getElementById("chatPanel");
    layoutContainer = document.querySelector(".main-layout-container");
    canvasTabsContainer = document.querySelector(".workspace-canvas-tabs");
    workspaceSidebar = document.getElementById("workspaceSidebar");
    workspaceExplorerToggle = document.getElementById("workspaceExplorerToggle");
    explorerContainer = document.getElementById("workspaceExplorer");

    // Tab 切换 — 绑定到 activity-bar 中的 ws-tab-btn 按钮
    const abTabBtns = document.querySelectorAll(".ab-item.ws-tab-btn");
    abTabBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            switchWorkspaceTab(this.dataset.tab);
        });
    });
    if (workspaceExplorerToggle) {
        workspaceExplorerToggle.addEventListener("click", toggleWorkspaceSidebar);
    }

    // ── Layout Mode Toggle ──
    initLayoutToggle();

    // 恢复上次布局状态
    const savedLayout = localStorage.getItem("myagent-layout-mode") || "split";
    applyLayoutMode(savedLayout);

    // workspace_state 可能来自独立推送，也可能来自 conversation_state。
    on('workspace:state', handleWorkspaceState);
    on('auth:logout', resetWorkspacePreview);
    on('session:changed', resetWorkspacePreview);
    ensureWorkspaceUploadInputs();
    document.addEventListener('click', hideWorkspaceContextMenu);
    document.addEventListener('click', function (e) {
        if (tabListPopup && !tabListPopup.contains(e.target)) {
            hideTabListPopup();
        }
    });
}

function resetWorkspacePreview() {
    activeDocumentSignature = '';
    activeDocumentPath = '';
    latestWorkspaceState = null;
    selectedWorkspacePath = '';
    hideWorkspaceContextMenu();
    lastRenderedTreeSignature = '';
    clearOptimisticActiveFile();
    closeAllDocuments();
    setDocumentTitle('文档预览');
    showOnlyOfficeHint('');
}

// ── Workspace State ──

function handleWorkspaceState(workspaceState, options) {
    if (!workspaceState) return;
    latestWorkspaceState = applyOptimisticActiveFile(
        normalizeWorkspaceState(workspaceState),
        options || {}
    );

    renderFileTabs(latestWorkspaceState);
    renderFileTreeIfChanged(latestWorkspaceState);

    const activeFile = getActiveFile(latestWorkspaceState);
    if (!activeFile || !isOnlyOfficeFile(activeFile.path)) {
        activeDocumentSignature = '';
        activeDocumentPath = '';
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
    const pathChanged = activeDocumentPath !== activeFile.path;
    const signatureChanged = activeDocumentSignature !== signature;

    if (!pathChanged && !signatureChanged) return;

    activeDocumentPath = activeFile.path;
    activeDocumentSignature = signature;

    if (pathChanged && activateDocument(activeFile.path)) {
        return;
    }

    openDocument(activeFile.path, mode, { signature: signature });
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

function toggleWorkspaceSidebar() {
    if (!workspaceSidebar) return;
    const willOpen = workspaceSidebar.classList.contains('collapsed');
    workspaceSidebar.classList.toggle('collapsed', !willOpen);
    if (workspaceExplorerToggle) {
        workspaceExplorerToggle.classList.toggle('active', willOpen);
    }
    // Mutual exclusion: collapse session sidebar when workspace sidebar opens
    if (willOpen) {
        var sessionSidebar = document.getElementById('sessionSidebar');
        if (sessionSidebar && !sessionSidebar.classList.contains('collapsed')) {
            sessionSidebar.classList.add('collapsed');
            localStorage.setItem('myagent-sidebar', 'collapsed');
            var sidebarToggle = document.getElementById('sidebarToggle');
            if (sidebarToggle) sidebarToggle.classList.remove('active');
        }
        if (latestWorkspaceState) {
            renderFileTree(latestWorkspaceState);
        }
    }
}

function renderFileTreeIfChanged(workspaceState) {
    const signature = getWorkspaceTreeSignature(workspaceState);
    if (signature === lastRenderedTreeSignature) return;
    lastRenderedTreeSignature = signature;
    renderFileTree(workspaceState);
}

function renderFileTabs(workspaceState) {
    if (!canvasTabsContainer) return;

    const files = workspaceState.open_files || [];
    const activeIndex = workspaceState.active_file_index;

    canvasTabsContainer.innerHTML = '';

    // Left scroll button
    const scrollLeftBtn = createScrollButton('left');
    canvasTabsContainer.appendChild(scrollLeftBtn);

    // Scroll wrapper + scroll area
    const scrollWrapper = document.createElement('div');
    scrollWrapper.className = 'canvas-tab-scroll-wrapper';
    const scrollArea = document.createElement('div');
    scrollArea.className = 'canvas-tab-scroll-area';

    if (!files.length) {
        scrollArea.appendChild(createPlaceholderTab());
    } else {
        files.forEach(function (file, index) {
            const tab = createFileTab(file, index, index === activeIndex);
            scrollArea.appendChild(tab);
        });
    }

    scrollWrapper.appendChild(scrollArea);
    canvasTabsContainer.appendChild(scrollWrapper);

    // Right scroll button
    const scrollRightBtn = createScrollButton('right');
    canvasTabsContainer.appendChild(scrollRightBtn);

    // Add button (outside scroll area)
    canvasTabsContainer.appendChild(createAddButton());

    // Setup scroll detection
    setupTabScroll(scrollArea, scrollLeftBtn, scrollRightBtn);

    // Auto-scroll to active tab
    if (files.length) {
        requestAnimationFrame(function () {
            var activeTab = scrollArea.querySelector('.canvas-tab.active');
            if (activeTab) {
                activeTab.scrollIntoView({ inline: 'nearest', behavior: 'smooth' });
            }
        });
    }
}

function createScrollButton(direction) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'canvas-tab-scroll-btn';
    btn.title = direction === 'left' ? '向左滚动' : '向右滚动';
    var arrow = direction === 'left'
        ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>'
        : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 6 15 12 9 18"/></svg>';
    btn.innerHTML = arrow;
    btn.dataset.direction = direction;
    return btn;
}

function setupTabScroll(scrollArea, leftBtn, rightBtn) {
    function updateButtons() {
        var sl = scrollArea.scrollLeft;
        var maxScroll = scrollArea.scrollWidth - scrollArea.clientWidth;
        leftBtn.classList.toggle('visible', sl > 4);
        rightBtn.classList.toggle('visible', sl < maxScroll - 4);
    }

    scrollArea.addEventListener('scroll', updateButtons);

    leftBtn.addEventListener('click', function () {
        scrollArea.scrollBy({ left: -160, behavior: 'smooth' });
    });

    rightBtn.addEventListener('click', function () {
        scrollArea.scrollBy({ left: 160, behavior: 'smooth' });
    });

    // Initial check + deferred check (after layout settles)
    updateButtons();
    requestAnimationFrame(updateButtons);
    setTimeout(updateButtons, 200);
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
    button.title = '查看所有打开的标签';
    button.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
    `;
    button.addEventListener('click', function (event) {
        event.stopPropagation();
        toggleTabListPopup(button);
    });
    return button;
}

function toggleTabListPopup(anchorButton) {
    if (tabListPopup) {
        hideTabListPopup();
        return;
    }
    showTabListPopup(anchorButton);
}

function showTabListPopup(anchorButton) {
    hideTabListPopup();
    if (!latestWorkspaceState) return;

    const files = latestWorkspaceState.open_files || [];
    const activeIndex = latestWorkspaceState.active_file_index;

    const popup = document.createElement('div');
    popup.className = 'canvas-tab-list-popup';

    if (!files.length) {
        const empty = document.createElement('div');
        empty.className = 'canvas-tab-list-empty';
        empty.textContent = '暂无打开的文件';
        popup.appendChild(empty);
    } else {
        files.forEach(function (file, index) {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'canvas-tab-list-item' + (index === activeIndex ? ' active' : '');
            item.title = file.path || '';
            item.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="2">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="canvas-tab-list-item-name">${escapeHtml(fileName(file.path))}</span>
            `;
            item.addEventListener('click', function () {
                hideTabListPopup();
                activateFileTab(index);
            });
            popup.appendChild(item);
        });
    }

    anchorButton.style.position = 'relative';
    anchorButton.appendChild(popup);
    tabListPopup = popup;
}

function hideTabListPopup() {
    if (tabListPopup && tabListPopup.parentNode) {
        tabListPopup.parentNode.removeChild(tabListPopup);
    }
    tabListPopup = null;
}

function activateFileTab(index) {
    if (!latestWorkspaceState) return;
    const files = latestWorkspaceState.open_files || [];
    if (index < 0 || index >= files.length) return;

    console.info('[Workspace] activating file tab', {
        index: index,
        path: files[index].path,
    });

    optimisticActiveFilePath = files[index].path;
    optimisticActiveRequestedAt = Date.now();
    latestWorkspaceState = Object.assign({}, latestWorkspaceState, {
        active_file_index: index,
    });
    handleWorkspaceState(latestWorkspaceState, { keepOptimisticActive: true });
    send({ type: 'workspace_set_active_file', index: index });
}

function closeFileTab(index) {
    if (!latestWorkspaceState) return;
    const files = latestWorkspaceState.open_files || [];
    if (index < 0 || index >= files.length) return;

    const closedPath = files[index].path;
    if (closedPath === optimisticActiveFilePath) clearOptimisticActiveFile();
    console.info('[Workspace] closing file tab', {
        index: index,
        path: closedPath,
    });
    closeDocument(closedPath);

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

// -- File Explorer --

function renderFileTree(workspaceState) {
    if (!explorerContainer) return;

    // Save scroll position before rebuilding DOM
    const oldTree = explorerContainer.querySelector('.workspace-tree');
    const savedScrollTop = oldTree ? oldTree.scrollTop : 0;

    const files = workspaceState.files || [];
    const rootName = workspaceState.root_path ? fileName(workspaceState.root_path) : '工作空间';

    explorerContainer.innerHTML = '';
    explorerContainer.appendChild(createExplorerHeader(rootName, workspaceState.root_path));

    if (!workspaceState.root_path) {
        explorerContainer.appendChild(createExplorerEmpty('未设置工作空间目录'));
        return;
    }
    if (!files.length) {
        explorerContainer.appendChild(createExplorerEmpty('目录为空'));
        return;
    }

    const tree = buildTree(files);
    const treeEl = document.createElement('div');
    treeEl.className = 'workspace-tree';
    renderTreeChildren(tree.children, treeEl, 0, workspaceState);
    explorerContainer.appendChild(treeEl);

    // Restore scroll position after rebuild
    if (savedScrollTop > 0) {
        requestAnimationFrame(function () {
            treeEl.scrollTop = savedScrollTop;
        });
    }
}

function createExplorerHeader(rootName, rootPath) {
    const header = document.createElement('div');
    header.className = 'workspace-explorer-header';

    const title = document.createElement('div');
    title.className = 'workspace-explorer-title';
    title.textContent = rootName || '工作空间';
    title.title = rootPath || '';

    const actions = document.createElement('div');
    actions.className = 'workspace-explorer-actions';
    actions.appendChild(createExplorerAction('刷新', '↻', function () {
        send({ type: 'workspace_refresh' });
    }));
    actions.appendChild(createExplorerAction('上传', '↑', function (button) {
        showWorkspaceUploadMenu(button, getDefaultUploadDir());
    }));
    actions.appendChild(createExplorerAction('折叠全部', '−', function () {
        if (!latestWorkspaceState) return;
        latestWorkspaceState = Object.assign({}, latestWorkspaceState, { expanded_dirs: [] });
        renderFileTree(latestWorkspaceState);
    }));

    header.appendChild(title);
    header.appendChild(actions);
    return header;
}

function createExplorerAction(title, label, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-explorer-action';
    button.title = title;
    button.textContent = label;
    button.addEventListener('click', function (event) {
        event.stopPropagation();
        onClick(button, event);
    });
    return button;
}

function ensureWorkspaceUploadInputs() {
    if (!workspaceUploadFileInput) {
        workspaceUploadFileInput = document.createElement('input');
        workspaceUploadFileInput.type = 'file';
        workspaceUploadFileInput.multiple = true;
        workspaceUploadFileInput.hidden = true;
        workspaceUploadFileInput.addEventListener('change', function () {
            startWorkspaceUpload(workspaceUploadFileInput.files, false, pendingUploadTargetDir);
        });
        document.body.appendChild(workspaceUploadFileInput);
    }
    if (!workspaceUploadFolderInput) {
        workspaceUploadFolderInput = document.createElement('input');
        workspaceUploadFolderInput.type = 'file';
        workspaceUploadFolderInput.multiple = true;
        workspaceUploadFolderInput.hidden = true;
        workspaceUploadFolderInput.setAttribute('webkitdirectory', '');
        workspaceUploadFolderInput.addEventListener('change', function () {
            startWorkspaceUpload(workspaceUploadFolderInput.files, true, pendingUploadTargetDir);
        });
        document.body.appendChild(workspaceUploadFolderInput);
    }
}

function showWorkspaceUploadMenu(anchorButton, defaultDir) {
    hideWorkspaceContextMenu();
    workspaceContextMenu = document.createElement('div');
    workspaceContextMenu.className = 'workspace-context-menu';

    workspaceContextMenu.appendChild(createContextMenuItem('上传文件', function () {
        openWorkspaceUploadPicker(false, defaultDir);
    }));
    workspaceContextMenu.appendChild(createContextMenuItem('上传文件夹', function () {
        openWorkspaceUploadPicker(true, defaultDir);
    }));

    const rect = anchorButton.getBoundingClientRect();
    workspaceContextMenu.style.left = `${rect.left}px`;
    workspaceContextMenu.style.top = `${rect.bottom + 4}px`;
    document.body.appendChild(workspaceContextMenu);
    keepContextMenuInViewport(workspaceContextMenu);
}

function openWorkspaceUploadPicker(isFolder, defaultDir) {
    if (!latestWorkspaceState || !latestWorkspaceState.root_path) {
        window.alert('未设置工作空间目录');
        return;
    }
    pendingUploadTargetDir = defaultDir || '';
    ensureWorkspaceUploadInputs();
    const input = isFolder ? workspaceUploadFolderInput : workspaceUploadFileInput;
    input.value = '';
    input.click();
}

async function startWorkspaceUpload(fileList, isFolder, defaultTargetDir) {
    const entries = collectUploadEntries(fileList, isFolder);
    clearWorkspaceUploadInputs();
    if (!entries.length) return;
    if (!state.currentSessionId) {
        window.alert('当前会话尚未连接');
        return;
    }

    const invalid = entries.filter(function (entry) {
        return hasForbiddenArchiveSuffix(entry.path);
    });
    if (invalid.length) {
        window.alert(`不允许上传压缩或归档文件：\n${invalid.slice(0, 8).map(function (entry) { return entry.path; }).join('\n')}`);
        return;
    }

    const targetDir = requestWorkspaceUploadTarget(defaultTargetDir);
    if (targetDir === null) return;

    try {
        const preflight = await workspaceFilesJson('/preflight', {
            session_id: state.currentSessionId,
            target_dir: targetDir,
            paths: entries.map(function (entry) { return entry.path; }),
        });
        if (preflight.rejected && preflight.rejected.length) {
            window.alert(formatRejectedMessage('部分文件无法上传', preflight.rejected));
            return;
        }

        let overwrite = false;
        if (preflight.conflicts && preflight.conflicts.length) {
            overwrite = window.confirm(formatConflictMessage(preflight.conflicts));
            if (!overwrite) return;
        }

        const form = new FormData();
        form.append('session_id', state.currentSessionId);
        form.append('target_dir', targetDir);
        form.append('overwrite', overwrite ? 'true' : 'false');
        entries.forEach(function (entry) {
            form.append('files[]', entry.file, entry.file.name);
            form.append('paths[]', entry.path);
        });

        const result = await workspaceFilesForm('/upload', form);
        if (result.rejected && result.rejected.length) {
            window.alert(formatRejectedMessage('部分文件未上传', result.rejected));
            return;
        }
        showOnlyOfficeHint(`已上传 ${result.uploaded ? result.uploaded.length : entries.length} 个文件`);
    } catch (err) {
        window.alert(err.message || '上传失败');
    }
}

function collectUploadEntries(fileList, isFolder) {
    return Array.from(fileList || []).map(function (file) {
        return {
            file: file,
            path: normalizeClientUploadPath(isFolder ? (file.webkitRelativePath || file.name) : file.name),
        };
    }).filter(function (entry) {
        return !!entry.path;
    });
}

function clearWorkspaceUploadInputs() {
    if (workspaceUploadFileInput) workspaceUploadFileInput.value = '';
    if (workspaceUploadFolderInput) workspaceUploadFolderInput.value = '';
}

function requestWorkspaceUploadTarget(defaultTargetDir) {
    const dirs = getKnownWorkspaceDirs();
    const preview = dirs.slice(0, 12).join('\n') || '根目录';
    const message = `请输入上传目标目录（留空表示 workspace 根目录）。\n已加载目录示例：\n${preview}`;
    const value = window.prompt(message, defaultTargetDir || '');
    if (value === null) return null;
    return normalizeClientUploadPath(value);
}

function getDefaultUploadDir() {
    const selected = findWorkspaceFile(selectedWorkspacePath);
    if (selected && selected.is_dir) return selected.path;
    if (selected && selected.path) return parentDir(selected.path);
    return '';
}

function getKnownWorkspaceDirs() {
    if (!latestWorkspaceState) return [''];
    const dirs = (latestWorkspaceState.files || [])
        .filter(function (file) { return file.is_dir; })
        .map(function (file) { return file.path; })
        .sort(function (a, b) { return a.localeCompare(b, 'zh-Hans-CN'); });
    return [''].concat(dirs);
}

function findWorkspaceFile(path) {
    if (!path || !latestWorkspaceState) return null;
    return (latestWorkspaceState.files || []).find(function (file) {
        return file.path === path;
    }) || null;
}

function parentDir(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    parts.pop();
    return parts.join('/');
}

function normalizeClientUploadPath(path) {
    return String(path || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
}

function hasForbiddenArchiveSuffix(path) {
    const lower = String(path || '').toLowerCase();
    return FORBIDDEN_ARCHIVE_SUFFIXES.some(function (suffix) {
        return lower.endsWith(suffix);
    });
}

async function workspaceFilesJson(route, payload) {
    const response = await fetch(`/api/workspace/files${route}`, {
        method: 'POST',
        headers: {
            'Authorization': 'Bearer ' + getToken(),
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
    });
    return parseWorkspaceFilesResponse(response);
}

async function workspaceFilesForm(route, form) {
    const response = await fetch(`/api/workspace/files${route}`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + getToken() },
        body: form,
    });
    return parseWorkspaceFilesResponse(response);
}

async function parseWorkspaceFilesResponse(response) {
    let data = null;
    try {
        data = await response.json();
    } catch (e) {
        data = null;
    }
    if (!response.ok) {
        const detail = data && data.detail ? data.detail : null;
        if (detail && typeof detail === 'object') {
            throw new Error(formatRejectedMessage('操作失败', detail.rejected || []));
        }
        throw new Error((detail && String(detail)) || (data && data.error) || '请求失败');
    }
    return data || {};
}

function formatConflictMessage(conflicts) {
    const lines = conflicts.slice(0, 10).map(function (item) {
        return item.target_path || item.path;
    });
    const more = conflicts.length > lines.length ? `\n... 还有 ${conflicts.length - lines.length} 个冲突` : '';
    return `以下文件已存在，是否覆盖？\n${lines.join('\n')}${more}`;
}

function formatRejectedMessage(title, rejected) {
    if (!rejected || !rejected.length) return title;
    const lines = rejected.slice(0, 10).map(function (item) {
        return `${item.target_path || item.path || ''}: ${item.reason || '失败'}`;
    });
    const more = rejected.length > lines.length ? `\n... 还有 ${rejected.length - lines.length} 项` : '';
    return `${title}：\n${lines.join('\n')}${more}`;
}

async function renameWorkspaceEntry(file) {
    if (!file || !file.path || !state.currentSessionId) return;
    const currentName = fileName(file.path);
    const nextName = window.prompt('请输入新名称', currentName);
    if (nextName === null) return;
    const trimmed = nextName.trim();
    if (!trimmed || trimmed === currentName) return;

    try {
        await workspaceFilesJson('/rename', {
            session_id: state.currentSessionId,
            path: file.path,
            new_name: trimmed,
        });
    } catch (err) {
        window.alert(err.message || '重命名失败');
    }
}

async function downloadWorkspaceEntry(file) {
    if (!file || !file.path || !state.currentSessionId) return;
    const params = new URLSearchParams({
        session_id: state.currentSessionId,
        path: file.path,
    });

    try {
        const response = await fetch(`/api/workspace/files/download?${params.toString()}`, {
            headers: { 'Authorization': 'Bearer ' + getToken() },
        });
        if (!response.ok) {
            let message = '下载失败';
            try {
                const data = await response.json();
                message = data.detail || data.error || message;
            } catch (e) {
                // 保持默认文案。
            }
            throw new Error(message);
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = file.is_dir ? `${fileName(file.path)}.zip` : fileName(file.path);
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    } catch (err) {
        window.alert(err.message || '下载失败');
    }
}

function aiReadWorkspaceFile(file) {
    if (!file || !file.path || file.is_dir) return;
    const text = [
        '请使用 file_read 工具读取以下 workspace 文件，并总结主要内容，指出与当前任务相关的关键信息。',
        `相对路径：${file.path}`,
        '请直接使用上述工作区相对路径，不要改写为系统绝对路径。',
    ].join('\n');

    const input = document.getElementById('userInput');
    const sendButton = document.getElementById('sendBtn');
    if (input && sendButton) {
        input.value = text;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        sendButton.click();
        return;
    }

    emit('chat:send', { text: text });
}

async function deleteWorkspaceEntry(file) {
    if (!file || !file.path || !state.currentSessionId) return;
    const message = file.is_dir
        ? `确定删除目录及其全部内容？\n${file.path}`
        : `确定删除文件？\n${file.path}`;
    if (!window.confirm(message)) return;

    try {
        const result = await workspaceFilesJson('/delete', {
            session_id: state.currentSessionId,
            paths: [file.path],
            recursive: Boolean(file.is_dir),
        });
        if (result.rejected && result.rejected.length) {
            window.alert(formatRejectedMessage('部分路径未删除', result.rejected));
        }
    } catch (err) {
        window.alert(err.message || '删除失败');
    }
}

function createExplorerEmpty(message) {
    const empty = document.createElement('div');
    empty.className = 'workspace-explorer-empty';
    empty.textContent = message;
    return empty;
}

function buildTree(files) {
    const root = { children: new Map() };
    files.slice().sort(function (a, b) {
        if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
        return a.path.localeCompare(b.path, 'zh-Hans-CN');
    }).forEach(function (file) {
        const parts = String(file.path || '').split('/').filter(Boolean);
        let node = root;
        parts.forEach(function (part, index) {
            if (!node.children.has(part)) {
                node.children.set(part, { name: part, path: parts.slice(0, index + 1).join('/'), children: new Map(), file: null });
            }
            node = node.children.get(part);
        });
        node.file = file;
    });
    return root;
}

function renderTreeChildren(children, container, depth, workspaceState) {
    Array.from(children.values()).sort(function (a, b) {
        const aDir = a.file && a.file.is_dir;
        const bDir = b.file && b.file.is_dir;
        if (aDir !== bDir) return aDir ? -1 : 1;
        return a.name.localeCompare(b.name, 'zh-Hans-CN');
    }).forEach(function (node) {
        const file = node.file || { path: node.path, is_dir: node.children.size > 0 };
        const row = createTreeRow(file, depth, workspaceState);
        container.appendChild(row);
        if (file.is_dir && isDirExpanded(workspaceState, file.path)) {
            renderTreeChildren(node.children, container, depth + 1, workspaceState);
        }
    });
}

function createTreeRow(file, depth, workspaceState) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'workspace-tree-row';
    row.classList.toggle('selected', selectedWorkspacePath === file.path);
    row.classList.toggle('opened', Boolean(file.is_user_opened));
    row.classList.toggle('llm-read', Boolean(file.is_llm_read));
    row.dataset.path = file.path;
    row.dataset.kind = file.is_dir ? 'dir' : 'file';
    row.title = file.path;
    row.style.setProperty('--tree-depth', String(depth));

    const expanded = file.is_dir && isDirExpanded(workspaceState, file.path);
    const folderIcon = file.is_dir
        ? (expanded ? '<i class="fa-solid fa-folder-open" style="color:#e8a87c"></i>' : '<i class="fa-solid fa-folder" style="color:#e8a87c"></i>')
        : fileIcon(file.path);
    row.innerHTML = `
        <span class="workspace-tree-caret">${file.is_dir ? (expanded ? '⌄' : '›') : ''}</span>
        <span class="workspace-tree-icon">${folderIcon}</span>
        <span class="workspace-tree-name">${escapeHtml(fileName(file.path))}</span>
        <span class="workspace-tree-badge">${file.is_user_opened ? '已打开' : (file.is_llm_read ? '已读' : '')}</span>
    `;

    row.addEventListener('click', function () {
        selectWorkspaceEntry(file);
        if (file.is_dir) toggleDirectory(file.path);
    });
    row.addEventListener('dblclick', function (event) {
        event.preventDefault();
        if (!file.is_dir) openWorkspaceFile(file.path);
    });
    row.addEventListener('contextmenu', function (event) {
        event.preventDefault();
        selectWorkspaceEntry(file);
        showWorkspaceContextMenu(file, event.clientX, event.clientY);
    });

    return row;
}

function selectWorkspaceEntry(file) {
    selectedWorkspacePath = file.path;
    if (explorerContainer) {
        explorerContainer.querySelectorAll('.workspace-tree-row').forEach(function (row) {
            row.classList.toggle('selected', row.dataset.path === selectedWorkspacePath);
        });
    }
}

function toggleDirectory(path) {
    if (!latestWorkspaceState || !path) return;
    const expanded = new Set(latestWorkspaceState.expanded_dirs || []);
    if (expanded.has(path)) {
        expanded.delete(path);
        latestWorkspaceState = Object.assign({}, latestWorkspaceState, { expanded_dirs: Array.from(expanded) });
        renderFileTree(latestWorkspaceState);
        send({ type: 'workspace_collapse_dir', path: path });
        return;
    }
    expanded.add(path);
    latestWorkspaceState = Object.assign({}, latestWorkspaceState, { expanded_dirs: Array.from(expanded) });
    renderFileTree(latestWorkspaceState);
    send({ type: 'workspace_scan_dir', path: path });
}

function isDirExpanded(workspaceState, path) {
    return (workspaceState.expanded_dirs || []).indexOf(path) >= 0;
}

function openWorkspaceFile(path) {
    if (!path) return;
    send({ type: 'workspace_open_file', path: path, open_with: 'onlyoffice' });
}

function showWorkspaceContextMenu(file, x, y) {
    hideWorkspaceContextMenu();
    workspaceContextMenu = document.createElement('div');
    workspaceContextMenu.className = 'workspace-context-menu';
    workspaceContextMenu.style.left = `${x}px`;
    workspaceContextMenu.style.top = `${y}px`;

    if (file.is_dir) {
        workspaceContextMenu.appendChild(createContextMenuItem('展开/刷新目录', function () {
            const expanded = new Set((latestWorkspaceState && latestWorkspaceState.expanded_dirs) || []);
            expanded.add(file.path);
            latestWorkspaceState = Object.assign({}, latestWorkspaceState, { expanded_dirs: Array.from(expanded) });
            renderFileTree(latestWorkspaceState);
            send({ type: 'workspace_scan_dir', path: file.path });
        }));
        workspaceContextMenu.appendChild(createContextMenuItem('上传到此目录', function () {
            openWorkspaceUploadPicker(false, file.path);
        }, file.can_upload === false));
    } else {
        workspaceContextMenu.appendChild(createContextMenuItem('ai读取', function () {
            aiReadWorkspaceFile(file);
        }));
        workspaceContextMenu.appendChild(createContextMenuItem('预览/编辑', function () {
            openWorkspaceFile(file.path);
        }, !isOnlyOfficeFile(file.path)));
    }
    workspaceContextMenu.appendChild(createContextMenuItem('重命名', function () {
        renameWorkspaceEntry(file);
    }, file.can_rename === false));
    workspaceContextMenu.appendChild(createContextMenuItem('下载', function () {
        downloadWorkspaceEntry(file);
    }));
    workspaceContextMenu.appendChild(createContextMenuItem('复制相对路径', function () {
        copyText(file.path);
    }));
    workspaceContextMenu.appendChild(createContextMenuItem('删除', function () {
        deleteWorkspaceEntry(file);
    }, file.can_delete === false));

    document.body.appendChild(workspaceContextMenu);
    keepContextMenuInViewport(workspaceContextMenu);
}

function createContextMenuItem(label, onClick, disabled) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-context-menu-item';
    button.disabled = Boolean(disabled);
    button.textContent = label;
    button.addEventListener('click', function (event) {
        event.stopPropagation();
        hideWorkspaceContextMenu();
        if (!disabled) onClick();
    });
    return button;
}

function hideWorkspaceContextMenu() {
    if (workspaceContextMenu && workspaceContextMenu.parentNode) {
        workspaceContextMenu.parentNode.removeChild(workspaceContextMenu);
    }
    workspaceContextMenu = null;
}

function keepContextMenuInViewport(menu) {
    const rect = menu.getBoundingClientRect();
    const left = Math.min(rect.left, window.innerWidth - rect.width - 8);
    const top = Math.min(rect.top, window.innerHeight - rect.height - 8);
    menu.style.left = `${Math.max(8, left)}px`;
    menu.style.top = `${Math.max(8, top)}px`;
}

function copyText(value) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).catch(function () { });
    }
}

function fileIcon(path) {
    const lower = String(path || '').toLowerCase();
    if (lower.endsWith('.py')) return '<i class="fa-brands fa-python"></i>';
    if (lower.endsWith('.js') || lower.endsWith('.mjs')) return '<i class="fa-brands fa-js"></i>';
    if (lower.endsWith('.ts')) return '<i class="fa-solid fa-file-code" style="color:#3178c6"></i>';
    if (lower.endsWith('.css')) return '<i class="fa-solid fa-file-code" style="color:#1572b6"></i>';
    if (lower.endsWith('.html') || lower.endsWith('.htm')) return '<i class="fa-brands fa-html5"></i>';
    if (lower.endsWith('.json')) return '<i class="fa-solid fa-file-code" style="color:#f5a623"></i>';
    if (lower.endsWith('.yaml') || lower.endsWith('.yml')) return '<i class="fa-solid fa-file-code" style="color:#cb171e"></i>';
    if (lower.endsWith('.md') || lower.endsWith('.markdown')) return '<i class="fa-solid fa-file-lines" style="color:#519aba"></i>';
    if (lower.endsWith('.sql')) return '<i class="fa-solid fa-database" style="color:#e38c00"></i>';
    if (lower.endsWith('.sh') || lower.endsWith('.bash')) return '<i class="fa-solid fa-terminal" style="color:#4eaa25"></i>';
    if (lower.endsWith('.dockerfile') || lower.endsWith('.docker')) return '<i class="fa-brands fa-docker"></i>';
    if (lower.endsWith('.doc') || lower.endsWith('.docx')) return '<i class="fa-solid fa-file-word" style="color:#2b579a"></i>';
    if (lower.endsWith('.xls') || lower.endsWith('.xlsx') || lower.endsWith('.csv')) return '<i class="fa-solid fa-file-excel" style="color:#217346"></i>';
    if (lower.endsWith('.ppt') || lower.endsWith('.pptx')) return '<i class="fa-solid fa-file-powerpoint" style="color:#d24726"></i>';
    if (lower.endsWith('.pdf')) return '<i class="fa-solid fa-file-pdf" style="color:#e44332"></i>';
    if (lower.endsWith('.png') || lower.endsWith('.jpg') || lower.endsWith('.jpeg') || lower.endsWith('.gif') || lower.endsWith('.svg') || lower.endsWith('.webp')) return '<i class="fa-solid fa-file-image" style="color:#a259ff"></i>';
    if (lower.endsWith('.zip') || lower.endsWith('.tar') || lower.endsWith('.gz') || lower.endsWith('.rar')) return '<i class="fa-solid fa-file-zipper" style="color:#f5a623"></i>';
    if (lower.endsWith('.txt')) return '<i class="fa-solid fa-file-lines"></i>';
    return '<i class="fa-regular fa-file"></i>';
}

function applyOptimisticActiveFile(workspaceState, options) {
    if (!optimisticActiveFilePath) return workspaceState;

    const files = workspaceState.open_files || [];
    const pendingIndex = files.findIndex(function (file) {
        return file.path === optimisticActiveFilePath;
    });
    const isExpired = Date.now() - optimisticActiveRequestedAt > OPTIMISTIC_ACTIVE_TTL_MS;
    if (isExpired || pendingIndex < 0) {
        clearOptimisticActiveFile();
        return workspaceState;
    }

    const activeFile = getActiveFile(workspaceState);
    if (activeFile && activeFile.path === optimisticActiveFilePath) {
        if (!options.keepOptimisticActive) clearOptimisticActiveFile();
        return workspaceState;
    }

    return Object.assign({}, workspaceState, {
        active_file_index: pendingIndex,
    });
}

function clearOptimisticActiveFile() {
    optimisticActiveFilePath = '';
    optimisticActiveRequestedAt = 0;
}

function normalizeWorkspaceState(workspaceState) {
    return Object.assign({}, workspaceState, {
        files: workspaceState.files || [],
        open_files: workspaceState.open_files || [],
        expanded_dirs: workspaceState.expanded_dirs || [],
    });
}

export function getWorkspaceClientState() {
    if (!latestWorkspaceState) return null;
    const state = normalizeWorkspaceState(latestWorkspaceState);
    return {
        open_files: state.open_files.map(function (file) {
            return {
                path: file.path || '',
                is_dirty: !!file.is_dirty,
                cursor_line: Number.isFinite(file.cursor_line) ? file.cursor_line : 0,
                cursor_column: Number.isFinite(file.cursor_column) ? file.cursor_column : 0,
                scroll_top: Number.isFinite(file.scroll_top) ? file.scroll_top : 0,
            };
        }).filter(function (file) {
            return !!file.path;
        }),
        active_file_index: typeof state.active_file_index === 'number'
            ? state.active_file_index
            : null,
        expanded_dirs: state.expanded_dirs.slice(),
    };
}

function getWorkspaceTreeSignature(workspaceState) {
    const filePart = (workspaceState.files || []).map(function (file) {
        return [
            file.path || '',
            file.is_dir ? '1' : '0',
            file.is_user_opened ? '1' : '0',
            file.is_llm_read ? '1' : '0',
            file.modified_at || '',
            file.size || 0,
        ].join(':');
    }).join('\n');
    const expandedPart = (workspaceState.expanded_dirs || []).join('\n');
    return [workspaceState.root_path || '', expandedPart, filePart].join('\n---\n');
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
