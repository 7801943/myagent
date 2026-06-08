/**
 * workspace.js — 工作区面板
 * 职责：折叠/展开、Tab 切换、根据 workspace_state 打开 OnlyOffice 文档。
 */

import { on } from './state.js';
import { activateDocument, closeAllDocuments, closeDocument, openDocument } from './onlyoffice-editor.js';
import { send } from './connection.js';

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
let lastRenderedTreeSignature = '';
let optimisticActiveFilePath = '';
let optimisticActiveRequestedAt = 0;

const OPTIMISTIC_ACTIVE_TTL_MS = 4000;

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
    document.addEventListener('click', hideWorkspaceContextMenu);
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
        onClick();
    });
    return button;
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
    } else {
        workspaceContextMenu.appendChild(createContextMenuItem('打开', function () {
            openWorkspaceFile(file.path);
        }));
        workspaceContextMenu.appendChild(createContextMenuItem('在 OnlyOffice 中打开', function () {
            openWorkspaceFile(file.path);
        }, !isOnlyOfficeFile(file.path)));
    }
    workspaceContextMenu.appendChild(createContextMenuItem('复制相对路径', function () {
        copyText(file.path);
    }));

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
