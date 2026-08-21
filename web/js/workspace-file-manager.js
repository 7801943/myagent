/**
 * Complete Workspace Explorer file-management UI.
 *
 * This module intentionally owns only the explorer. Document tabs and the
 * OnlyOffice editor remain in workspace.js / onlyoffice-editor.js.
 */

import { state } from './state.js';
import { send } from './connection.js';
import { getToken } from './auth.js';

const ARCHIVE_SUFFIXES = [
    '.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.zst',
    '.cab', '.iso', '.jar', '.war', '.ear', '.apk', '.dmg',
];
const STORED_UPLOADS_KEY = 'myagent-workspace-upload-tasks';

let explorer = null;
let callbacks = {};
let workspaceState = null;
let selectedPaths = new Set();
let selectionAnchor = '';
let viewMode = 'tree';
let sortBy = 'name';
let sortOrder = 'asc';
let searchQuery = '';
let searchTimer = null;
let searchResults = [];
let trashEntries = [];
let contextMenu = null;
let dialog = null;
let detailsDrawer = null;
let toastHost = null;
let taskHost = null;
let quota = null;
let uploadFileInput = null;
let uploadFolderInput = null;
let replaceFileInput = null;
let pendingUploadTarget = '';
let pendingResumeUploadId = '';
let pendingReplacement = null;
let dragCounter = 0;
let tasks = new Map();
let lastSessionId = '';

export function initWorkspaceFileManager(options) {
    explorer = options && options.container;
    callbacks = options || {};
    if (!explorer) return;
    ensureGlobalUi();
    ensureUploadInputs();
    explorer.addEventListener('contextmenu', handleExplorerBlankContextMenu);
    explorer.addEventListener('dragenter', handleExternalDragEnter);
    explorer.addEventListener('dragover', handleExternalDragOver);
    explorer.addEventListener('dragleave', handleExternalDragLeave);
    explorer.addEventListener('drop', handleExternalDrop);
    document.addEventListener('click', function (event) {
        if (contextMenu && !contextMenu.contains(event.target)) hideContextMenu();
    });
    document.addEventListener('keydown', handleKeyboard);
}

export function renderWorkspaceFileManager(nextState) {
    if (!explorer) return;
    workspaceState = normalizeState(nextState);
    if (lastSessionId !== state.currentSessionId) {
        lastSessionId = state.currentSessionId || '';
        selectedPaths.clear();
        viewMode = 'tree';
        searchResults = [];
        trashEntries = [];
        quota = null;
        restorePendingUploads();
    }
    pruneSelection();
    renderExplorer();
    if (!quota && state.currentSessionId) refreshQuota();
}

export function resetWorkspaceFileManager() {
    workspaceState = null;
    selectedPaths.clear();
    searchResults = [];
    trashEntries = [];
    quota = null;
    hideContextMenu();
    closeDialog();
    closeDetailsDrawer();
    if (explorer) explorer.innerHTML = '';
}

function renderExplorer() {
    if (!explorer) return;
    const oldTree = explorer.querySelector('.workspace-tree');
    const scrollTop = oldTree ? oldTree.scrollTop : 0;
    explorer.innerHTML = '';
    explorer.appendChild(createHeader());

    if (!workspaceState || !workspaceState.root_path) {
        explorer.appendChild(emptyState('未设置工作空间目录', '连接会话后即可管理文件'));
        return;
    }

    if (viewMode === 'search') {
        renderSearchView();
    } else if (viewMode === 'trash') {
        renderTrashView();
    } else {
        renderTreeView(scrollTop);
    }
    renderSelectionBar();
    renderQuotaBar();
}

function createHeader() {
    const header = document.createElement('div');
    header.className = 'workspace-fm-header';

    const top = document.createElement('div');
    top.className = 'workspace-fm-header-top';
    const title = document.createElement('div');
    title.className = 'workspace-fm-title';
    title.innerHTML = viewMode === 'trash'
        ? '<i class="fa-solid fa-trash-can"></i><span class="workspace-fm-title-copy"><span>回收站</span><small>保留 30 天</small></span>'
        : (viewMode === 'search'
            ? '<i class="fa-solid fa-magnifying-glass"></i><span class="workspace-fm-title-copy"><span>搜索文件</span><small>private + public</small></span>'
            : '<i class="fa-regular fa-folder-open"></i><span class="workspace-fm-title-copy"><span>工作空间</span><small>文件管理</small></span>');
    top.appendChild(title);

    const actions = document.createElement('div');
    actions.className = 'workspace-fm-actions';
    if (viewMode !== 'tree') {
        actions.appendChild(iconButton('返回文件树', 'fa-solid fa-arrow-left', function () {
            viewMode = 'tree';
            selectedPaths.clear();
            renderExplorer();
        }));
    } else {
        actions.appendChild(iconButton('新建文件夹', 'fa-solid fa-folder-plus', function () { createFolderFlow(); }));
        actions.appendChild(iconButton('上传', 'fa-solid fa-arrow-up-from-bracket', function (button) {
            showUploadMenu(button, getDefaultTargetDir());
        }));
        actions.appendChild(iconButton('搜索', 'fa-solid fa-magnifying-glass', function () {
            viewMode = 'search';
            selectedPaths.clear();
            renderExplorer();
            requestAnimationFrame(function () {
                const input = explorer.querySelector('.workspace-fm-search-input');
                if (input) input.focus();
            });
        }));
        actions.appendChild(iconButton('刷新', 'fa-solid fa-rotate', refreshWorkspace));
        actions.appendChild(iconButton('更多', 'fa-solid fa-ellipsis', showHeaderMenu));
    }
    top.appendChild(actions);
    header.appendChild(top);

    if (viewMode === 'search') {
        const search = document.createElement('div');
        search.className = 'workspace-fm-search';
        search.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i>';
        const input = document.createElement('input');
        input.className = 'workspace-fm-search-input';
        input.type = 'search';
        input.placeholder = '按文件名或路径搜索';
        input.value = searchQuery;
        input.setAttribute('aria-label', '搜索工作空间文件');
        input.addEventListener('input', function () {
            searchQuery = input.value;
            clearTimeout(searchTimer);
            searchTimer = setTimeout(runSearch, 260);
        });
        search.appendChild(input);
        header.appendChild(search);
    }
    return header;
}

function renderTreeView(savedScrollTop) {
    const files = workspaceState.files || [];
    if (!files.length) {
        explorer.appendChild(emptyState('工作空间为空', '可新建文件夹或上传文件'));
        return;
    }
    const tree = buildTree(files);
    const treeEl = document.createElement('div');
    treeEl.className = 'workspace-tree workspace-fm-tree';
    treeEl.setAttribute('role', 'tree');
    treeEl.setAttribute('aria-label', '工作空间文件');
    renderTreeChildren(tree.children, treeEl, 0);
    explorer.appendChild(treeEl);
    if (savedScrollTop) requestAnimationFrame(function () { treeEl.scrollTop = savedScrollTop; });
}

function renderSearchView() {
    const content = document.createElement('div');
    content.className = 'workspace-fm-list workspace-fm-search-results';
    if (!searchQuery.trim()) {
        content.appendChild(emptyState('搜索 private 和 public', '输入文件名或相对路径'));
    } else if (!searchResults.length) {
        content.appendChild(emptyState('没有找到匹配文件', searchQuery));
    } else {
        searchResults.forEach(function (file) {
            content.appendChild(createResultRow(file));
        });
    }
    explorer.appendChild(content);
}

function renderTrashView() {
    const content = document.createElement('div');
    content.className = 'workspace-fm-list workspace-fm-trash-list';
    if (!trashEntries.length) {
        content.appendChild(emptyState('回收站为空', 'private 删除项目会保留 30 天'));
    } else {
        trashEntries.forEach(function (entry) {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'workspace-fm-result-row';
            row.classList.toggle('selected', selectedPaths.has(`trash:${entry.id}`));
            row.innerHTML = `
                <span class="workspace-fm-result-icon">${entry.kind === 'dir' ? fileIcon('folder', true) : fileIcon(entry.name, false)}</span>
                <span class="workspace-fm-result-main">
                    <span class="workspace-fm-result-name">${escapeHtml(entry.name)}</span>
                    <span class="workspace-fm-result-path">${escapeHtml(entry.original_path)} · ${formatBytes(entry.size)} · ${formatDate(entry.deleted_at)}</span>
                </span>`;
            row.addEventListener('click', function (event) {
                updateSelection(`trash:${entry.id}`, event);
                renderExplorer();
            });
            row.addEventListener('contextmenu', function (event) {
                event.preventDefault();
                if (!selectedPaths.has(`trash:${entry.id}`)) selectedPaths = new Set([`trash:${entry.id}`]);
                showTrashContextMenu(entry, event.clientX, event.clientY);
            });
            content.appendChild(row);
        });
    }
    explorer.appendChild(content);
}

function renderSelectionBar() {
    if (!selectedPaths.size) return;
    const bar = document.createElement('div');
    bar.className = 'workspace-fm-selection-bar';
    const count = document.createElement('span');
    count.textContent = `已选择 ${selectedPaths.size} 项`;
    bar.appendChild(count);

    const buttons = document.createElement('div');
    buttons.className = 'workspace-fm-selection-actions';
    if (viewMode === 'trash') {
        buttons.appendChild(compactButton('恢复', 'fa-solid fa-rotate-left', restoreSelectedTrash));
        buttons.appendChild(compactButton('永久删除', 'fa-solid fa-trash', purgeSelectedTrash, true));
    } else {
        const selected = selectedFiles();
        const permits = function (operation) {
            return selected.length > 0 && selected.every(function (file) {
                return Boolean(getCapabilities(file)[operation]);
            });
        };
        buttons.appendChild(compactButton('下载', 'fa-solid fa-download', downloadSelection, false, !permits('download'), '所选项目不可下载'));
        if (selected.length === 1 && !selected[0].is_dir && getCapabilities(selected[0]).replace) {
            buttons.appendChild(compactButton('新版本', 'fa-solid fa-file-arrow-up', function () {
                replaceFileFlow(selected[0]);
            }));
        }
        buttons.appendChild(compactButton('复制', 'fa-regular fa-copy', function () { copyMoveFlow('copy'); }, false, !permits('copy'), '所选项目不可复制'));
        buttons.appendChild(compactButton('移动', 'fa-solid fa-arrow-right-arrow-left', function () { copyMoveFlow('move'); }, false, !permits('move'), '所选项目不可移动'));
        buttons.appendChild(compactButton('删除', 'fa-solid fa-trash', deleteSelection, true, !permits('delete'), '所选项目不可删除'));
    }
    buttons.appendChild(compactButton('取消', 'fa-solid fa-xmark', function () {
        selectedPaths.clear();
        renderExplorer();
    }));
    bar.appendChild(buttons);
    explorer.appendChild(bar);
}

function renderQuotaBar() {
    if (!quota || viewMode !== 'tree') return;
    const used = Number(quota.used_bytes || 0) + Number(quota.reserved_bytes || 0);
    const total = Number(quota.total_bytes || 0);
    const percent = total ? Math.min(100, Math.round(used / total * 100)) : 0;
    const bar = document.createElement('div');
    bar.className = 'workspace-fm-quota';
    bar.title = `private 已用 ${formatBytes(used)}，共 ${formatBytes(total)}`;
    bar.innerHTML = `
        <span class="workspace-fm-quota-label">private ${formatBytes(used)} / ${formatBytes(total)}</span>
        <span class="workspace-fm-quota-track"><span style="width:${percent}%"></span></span>`;
    explorer.appendChild(bar);
}

function buildTree(files) {
    const root = { children: new Map() };
    files.forEach(function (file) {
        const parts = String(file.path || '').split('/').filter(Boolean);
        let node = root;
        parts.forEach(function (part, index) {
            if (!node.children.has(part)) {
                node.children.set(part, {
                    name: part,
                    path: parts.slice(0, index + 1).join('/'),
                    children: new Map(),
                    file: null,
                });
            }
            node = node.children.get(part);
        });
        node.file = file;
    });
    return root;
}

function renderTreeChildren(children, parent, depth) {
    sortedNodes(children).forEach(function (node) {
        const file = node.file || { path: node.path, is_dir: node.children.size > 0 };
        parent.appendChild(createTreeRow(file, depth));
        if (file.is_dir && isExpanded(file.path)) {
            renderTreeChildren(node.children, parent, depth + 1);
        }
    });
}

function sortedNodes(children) {
    return Array.from(children.values()).sort(function (a, b) {
        const aFile = a.file || {};
        const bFile = b.file || {};
        if (Boolean(aFile.is_dir) !== Boolean(bFile.is_dir)) return aFile.is_dir ? -1 : 1;
        let left;
        let right;
        if (sortBy === 'size') {
            left = Number(aFile.size || 0);
            right = Number(bFile.size || 0);
        } else if (sortBy === 'modified') {
            left = String(aFile.modified_at || '');
            right = String(bFile.modified_at || '');
        } else if (sortBy === 'type') {
            left = extension(a.name);
            right = extension(b.name);
        } else {
            left = a.name;
            right = b.name;
        }
        const result = typeof left === 'number' ? left - right : String(left).localeCompare(String(right), 'zh-Hans-CN');
        return sortOrder === 'desc' ? -result : result;
    });
}

function createTreeRow(file, depth) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'workspace-tree-row workspace-fm-tree-row';
    row.classList.toggle('selected', selectedPaths.has(file.path));
    row.classList.toggle('opened', Boolean(file.is_user_opened));
    row.classList.toggle('llm-read', Boolean(file.is_llm_read));
    row.dataset.path = file.path;
    row.dataset.kind = file.is_dir ? 'dir' : 'file';
    row.style.setProperty('--tree-depth', String(depth));
    row.setAttribute('role', 'treeitem');
    row.setAttribute('aria-selected', selectedPaths.has(file.path) ? 'true' : 'false');
    row.draggable = !isRoot(file.path);

    const expanded = file.is_dir && isExpanded(file.path);
    const areaBadge = isRoot(file.path)
        ? `<span class="workspace-fm-area-badge ${escapeHtml(file.area || '')}">${file.area === 'public' ? '公共' : '私有'}</span>`
        : '';
    row.innerHTML = `
        <span class="workspace-tree-caret" data-caret="true">${file.is_dir ? (expanded ? '⌄' : '›') : ''}</span>
        <span class="workspace-tree-icon">${fileIcon(file.path, file.is_dir, expanded)}</span>
        <span class="workspace-tree-name">${escapeHtml(fileName(file.path))}</span>
        ${areaBadge || `<span class="workspace-tree-badge">${file.is_user_opened ? '已打开' : (file.is_llm_read ? '已读' : '')}</span>`}`;

    row.addEventListener('click', function (event) {
        if (event.target.closest('[data-caret]') && file.is_dir) {
            toggleDirectory(file.path);
            return;
        }
        updateSelection(file.path, event);
        if (file.is_dir && !event.ctrlKey && !event.metaKey && !event.shiftKey) toggleDirectory(file.path);
        else renderExplorer();
    });
    row.addEventListener('dblclick', function (event) {
        event.preventDefault();
        if (file.is_dir) toggleDirectory(file.path);
        else openFile(file);
    });
    row.addEventListener('contextmenu', function (event) {
        event.preventDefault();
        event.stopPropagation();
        if (!selectedPaths.has(file.path)) selectedPaths = new Set([file.path]);
        selectionAnchor = file.path;
        showEntryContextMenu(file, event.clientX, event.clientY);
        renderSelectionHighlights();
    });
    row.addEventListener('dragstart', function (event) {
        if (isRoot(file.path)) return event.preventDefault();
        if (!selectedPaths.has(file.path)) selectedPaths = new Set([file.path]);
        event.dataTransfer.effectAllowed = 'copyMove';
        event.dataTransfer.setData('application/x-myagent-workspace-paths', JSON.stringify(Array.from(selectedPaths)));
        row.classList.add('dragging');
    });
    row.addEventListener('dragend', function () { row.classList.remove('dragging'); });
    if (file.is_dir) {
        row.addEventListener('dragover', function (event) {
            event.preventDefault();
            event.stopPropagation();
            event.dataTransfer.dropEffect = event.ctrlKey || event.altKey ? 'copy' : 'move';
            row.classList.add('drop-target');
        });
        row.addEventListener('dragleave', function () { row.classList.remove('drop-target'); });
        row.addEventListener('drop', function (event) {
            row.classList.remove('drop-target');
            if (event.dataTransfer.types.includes('application/x-myagent-workspace-paths')) {
                event.preventDefault();
                event.stopPropagation();
                const paths = JSON.parse(event.dataTransfer.getData('application/x-myagent-workspace-paths') || '[]');
                selectedPaths = new Set(paths);
                startCopyMove(event.ctrlKey || event.altKey ? 'copy' : 'move', file.path, 'fail');
            }
        });
    }
    installLongPress(row, function (event) { showEntryContextMenu(file, event.clientX, event.clientY); });
    return row;
}

function createResultRow(file) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'workspace-fm-result-row';
    row.classList.toggle('selected', selectedPaths.has(file.path));
    row.innerHTML = `
        <span class="workspace-fm-result-icon">${fileIcon(file.path, file.is_dir)}</span>
        <span class="workspace-fm-result-main">
            <span class="workspace-fm-result-name">${escapeHtml(file.name || fileName(file.path))}</span>
            <span class="workspace-fm-result-path">${escapeHtml(file.path)}${file.is_dir ? '' : ` · ${formatBytes(file.size)}`}</span>
        </span>`;
    row.addEventListener('click', function (event) {
        updateSelection(file.path, event);
        renderExplorer();
    });
    row.addEventListener('dblclick', function () {
        if (file.is_dir) revealDirectory(file.path);
        else openFile(file);
    });
    row.addEventListener('contextmenu', function (event) {
        event.preventDefault();
        if (!selectedPaths.has(file.path)) selectedPaths = new Set([file.path]);
        showEntryContextMenu(file, event.clientX, event.clientY);
    });
    return row;
}

function updateSelection(path, event) {
    const visible = visiblePaths();
    if (event.shiftKey && selectionAnchor && visible.includes(selectionAnchor)) {
        const start = visible.indexOf(selectionAnchor);
        const end = visible.indexOf(path);
        const [from, to] = start < end ? [start, end] : [end, start];
        selectedPaths = new Set(visible.slice(from, to + 1));
    } else if (event.ctrlKey || event.metaKey) {
        if (selectedPaths.has(path)) selectedPaths.delete(path);
        else selectedPaths.add(path);
        selectionAnchor = path;
    } else {
        selectedPaths = new Set([path]);
        selectionAnchor = path;
    }
}

function visiblePaths() {
    if (viewMode === 'search') return searchResults.map(function (item) { return item.path; });
    if (viewMode === 'trash') return trashEntries.map(function (item) { return `trash:${item.id}`; });
    return Array.from(explorer.querySelectorAll('.workspace-fm-tree-row')).map(function (row) { return row.dataset.path; });
}

function renderSelectionHighlights() {
    explorer.querySelectorAll('[data-path]').forEach(function (row) {
        const selected = selectedPaths.has(row.dataset.path);
        row.classList.toggle('selected', selected);
        row.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
}

function pruneSelection() {
    if (viewMode !== 'tree' || !workspaceState) return;
    const known = new Set((workspaceState.files || []).map(function (file) { return file.path; }));
    selectedPaths.forEach(function (path) { if (!known.has(path)) selectedPaths.delete(path); });
}

function toggleDirectory(path) {
    const expanded = new Set(workspaceState.expanded_dirs || []);
    if (expanded.has(path)) {
        expanded.delete(path);
        workspaceState = Object.assign({}, workspaceState, { expanded_dirs: Array.from(expanded) });
        send({ type: 'workspace_collapse_dir', path: path });
    } else {
        expanded.add(path);
        workspaceState = Object.assign({}, workspaceState, { expanded_dirs: Array.from(expanded) });
        send({ type: 'workspace_scan_dir', path: path });
    }
    renderExplorer();
}

function revealDirectory(path) {
    viewMode = 'tree';
    selectedPaths = new Set([path]);
    const expanded = new Set(workspaceState.expanded_dirs || []);
    let current = path;
    while (current) {
        expanded.add(current);
        current = parentDir(current);
    }
    workspaceState = Object.assign({}, workspaceState, { expanded_dirs: Array.from(expanded) });
    send({ type: 'workspace_scan_dir', path: path });
    renderExplorer();
}

function isExpanded(path) {
    return (workspaceState.expanded_dirs || []).includes(path);
}

function isRoot(path) {
    return !String(path || '').includes('/');
}

// ---------------------------------------------------------------------------
// Menus and operation flows
// ---------------------------------------------------------------------------

function showEntryContextMenu(file, x, y) {
    const capabilities = getCapabilities(file);
    const items = [];
    if (file.is_dir) {
        items.push(menuItem('展开并刷新', 'fa-solid fa-rotate', function () {
            const expanded = new Set(workspaceState.expanded_dirs || []);
            expanded.add(file.path);
            workspaceState = Object.assign({}, workspaceState, { expanded_dirs: Array.from(expanded) });
            send({ type: 'workspace_scan_dir', path: file.path });
            renderExplorer();
        }));
        items.push(menuItem('新建文件夹', 'fa-solid fa-folder-plus', function () {
            createFolderFlow(file.path);
        }, !capabilities.create_folder, permissionHint(capabilities.create_folder)));
        items.push(menuItem('上传文件', 'fa-solid fa-file-arrow-up', function () {
            openUploadPicker(false, file.path);
        }, !capabilities.upload, permissionHint(capabilities.upload)));
        items.push(menuItem('上传文件夹', 'fa-solid fa-folder-tree', function () {
            openUploadPicker(true, file.path);
        }, !capabilities.upload, permissionHint(capabilities.upload)));
        items.push(separator());
    } else {
        items.push(menuItem('预览 / 编辑', 'fa-regular fa-pen-to-square', function () {
            openFile(file);
        }, false));
        items.push(menuItem('AI 读取', 'fa-solid fa-wand-magic-sparkles', function () {
            if (callbacks.aiRead) callbacks.aiRead(file);
        }));
        items.push(menuItem('上传新版本…', 'fa-solid fa-file-arrow-up', function () {
            replaceFileFlow(file);
        }, !capabilities.replace, permissionHint(capabilities.replace)));
        items.push(separator());
    }
    items.push(menuItem('下载', 'fa-solid fa-download', downloadSelection, !capabilities.download));
    items.push(menuItem('复制到…', 'fa-regular fa-copy', function () { copyMoveFlow('copy'); }, !capabilities.copy));
    items.push(menuItem('移动到…', 'fa-solid fa-arrow-right-arrow-left', function () { copyMoveFlow('move'); }, !capabilities.move, permissionHint(capabilities.move)));
    items.push(menuItem('重命名', 'fa-solid fa-i-cursor', function () { renameFlow(file); }, !capabilities.rename, permissionHint(capabilities.rename)));
    items.push(separator());
    items.push(menuItem('查看详情', 'fa-solid fa-circle-info', function () { showDetails(file.path); }));
    items.push(menuItem('复制相对路径', 'fa-regular fa-clipboard', function () {
        copyText(file.path);
        showToast('已复制相对路径', 'success');
    }));
    items.push(separator());
    items.push(menuItem(file.area === 'public' ? '永久删除' : '移到回收站', 'fa-solid fa-trash', deleteSelection, !capabilities.delete, permissionHint(capabilities.delete), true));
    showContextMenu(items, x, y);
}

function showTrashContextMenu(entry, x, y) {
    showContextMenu([
        menuItem('恢复到原位置', 'fa-solid fa-rotate-left', restoreSelectedTrash),
        menuItem('恢复到…', 'fa-solid fa-folder-open', function () { restoreSelectedTrash(true); }),
        separator(),
        menuItem('永久删除', 'fa-solid fa-trash', purgeSelectedTrash, false, '', true),
    ], x, y);
}

function handleExplorerBlankContextMenu(event) {
    if (event.target.closest('.workspace-fm-tree-row, .workspace-fm-result-row, .workspace-context-menu')) return;
    event.preventDefault();
    if (viewMode !== 'tree') return;
    const target = getDefaultTargetDir();
    const file = findFile(target);
    const capabilities = getCapabilities(file || { path: target, is_dir: true, area: areaForPath(target) });
    showContextMenu([
        menuItem('新建文件夹', 'fa-solid fa-folder-plus', function () { createFolderFlow(target); }, !capabilities.create_folder),
        menuItem('上传文件', 'fa-solid fa-file-arrow-up', function () { openUploadPicker(false, target); }, !capabilities.upload),
        menuItem('上传文件夹', 'fa-solid fa-folder-tree', function () { openUploadPicker(true, target); }, !capabilities.upload),
        separator(),
        menuItem('刷新', 'fa-solid fa-rotate', refreshWorkspace),
    ], event.clientX, event.clientY);
}

function showUploadMenu(button, targetDir) {
    const rect = button.getBoundingClientRect();
    showContextMenu([
        menuItem('上传文件', 'fa-solid fa-file-arrow-up', function () { openUploadPicker(false, targetDir); }),
        menuItem('上传文件夹', 'fa-solid fa-folder-tree', function () { openUploadPicker(true, targetDir); }),
    ], rect.left, rect.bottom + 6);
}

function showHeaderMenu(button) {
    const rect = button.getBoundingClientRect();
    const sortItems = ['name', 'modified', 'size', 'type'];
    const nextSort = sortItems[(sortItems.indexOf(sortBy) + 1) % sortItems.length];
    const sortLabels = { name: '名称', modified: '修改时间', size: '大小', type: '类型' };
    showContextMenu([
        menuItem(`排序：${sortLabels[sortBy]}`, 'fa-solid fa-arrow-down-a-z', function () {
            sortBy = nextSort;
            renderExplorer();
        }),
        menuItem(sortOrder === 'asc' ? '升序' : '降序', sortOrder === 'asc' ? 'fa-solid fa-arrow-up' : 'fa-solid fa-arrow-down', function () {
            sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
            renderExplorer();
        }),
        separator(),
        menuItem('全部折叠', 'fa-solid fa-angles-up', function () {
            workspaceState = Object.assign({}, workspaceState, { expanded_dirs: [] });
            renderExplorer();
        }),
        menuItem('回收站', 'fa-solid fa-trash-can-arrow-up', openTrash),
        menuItem('任务中心', 'fa-solid fa-list-check', toggleTaskPanel),
    ], rect.right - 190, rect.bottom + 6);
}

function showContextMenu(items, x, y) {
    hideContextMenu();
    contextMenu = document.createElement('div');
    contextMenu.className = 'workspace-context-menu workspace-fm-context-menu';
    items.forEach(function (item) { contextMenu.appendChild(item); });
    contextMenu.style.left = `${x}px`;
    contextMenu.style.top = `${y}px`;
    document.body.appendChild(contextMenu);
    const rect = contextMenu.getBoundingClientRect();
    contextMenu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    contextMenu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
}

function hideContextMenu() {
    if (contextMenu) contextMenu.remove();
    contextMenu = null;
}

function menuItem(label, icon, action, disabled, hint, danger) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-context-menu-item workspace-fm-menu-item';
    if (danger) button.classList.add('danger');
    button.disabled = Boolean(disabled);
    button.innerHTML = `<i class="${icon}"></i><span>${escapeHtml(label)}</span>${hint ? `<small>${escapeHtml(hint)}</small>` : ''}`;
    button.addEventListener('click', function (event) {
        event.stopPropagation();
        hideContextMenu();
        if (!disabled) action();
    });
    return button;
}

function separator() {
    const element = document.createElement('div');
    element.className = 'workspace-fm-menu-separator';
    return element;
}

async function createFolderFlow(parentPath) {
    const parent = parentPath || getDefaultTargetDir();
    const file = findFile(parent);
    if (!parent || !getCapabilities(file || {}).create_folder) {
        showToast('当前目录不允许新建文件夹', 'error');
        return;
    }
    const result = await showFormDialog({
        title: '新建文件夹',
        message: `位置：${parent}`,
        confirmLabel: '创建',
        fields: [{ id: 'name', label: '文件夹名称', type: 'text', required: true, autofocus: true }],
    });
    if (!result) return;
    await runImmediate('正在创建文件夹…', async function () {
        await apiJson('/folders', {
            method: 'POST',
            body: { session_id: state.currentSessionId, parent: parent, name: result.name },
        });
        showToast('文件夹已创建', 'success');
        refreshWorkspace();
    });
}

async function renameFlow(file) {
    if (!file || isRoot(file.path)) return;
    if ((workspaceState.open_files || []).some(function (tab) { return tab.path === file.path || tab.path.startsWith(file.path + '/'); })) {
        showToast('请先关闭该文件或目录中的文档标签', 'error');
        return;
    }
    const result = await showFormDialog({
        title: '重命名',
        message: file.path,
        confirmLabel: '保存',
        fields: [{ id: 'name', label: '新名称', type: 'text', value: fileName(file.path), required: true, autofocus: true }],
    });
    if (!result || result.name.trim() === fileName(file.path)) return;
    await runImmediate('正在重命名…', async function () {
        await apiJson('/rename', {
            method: 'POST',
            body: {
                session_id: state.currentSessionId,
                path: file.path,
                new_name: result.name.trim(),
                expected_version: file.version || '',
            },
        });
        selectedPaths.clear();
        showToast('重命名成功', 'success');
    });
}

async function copyMoveFlow(kind) {
    const entries = selectedFiles();
    if (!entries.length) return;
    const directories = knownDirectories().filter(function (directory) {
        return !entries.some(function (entry) {
            return directory.path === entry.path || directory.path.startsWith(entry.path + '/');
        });
    });
    if (!directories.length) {
        showToast('没有可用的目标目录', 'error');
        return;
    }
    const result = await showFormDialog({
        title: kind === 'copy' ? '复制到' : '移动到',
        message: `已选择 ${entries.length} 项`,
        confirmLabel: kind === 'copy' ? '开始复制' : '开始移动',
        fields: [
            { id: 'target', label: '目标目录', type: 'select', options: directories.map(function (item) { return { value: item.path, label: item.path }; }), value: getDefaultTargetDir() },
            { id: 'policy', label: '同名处理', type: 'select', options: [
                { value: 'fail', label: '发现冲突时停止' },
                { value: 'keep_both', label: '保留两份' },
                { value: 'overwrite', label: '合并并覆盖同名文件' },
                { value: 'skip', label: '跳过同名项目' },
            ] },
        ],
    });
    if (!result) return;
    startCopyMove(kind, result.target, result.policy);
}

async function startCopyMove(kind, targetDir, conflictPolicy) {
    const entries = selectedFiles();
    if (!entries.length) return;
    const expectedVersions = {};
    entries.forEach(function (item) { if (item.version) expectedVersions[item.path] = item.version; });
    try {
        const response = await apiJson(`/${kind}`, {
            method: 'POST',
            body: {
                session_id: state.currentSessionId,
                sources: entries.map(function (item) { return item.path; }),
                target_dir: targetDir,
                conflict_policy: conflictPolicy,
                expected_versions: expectedVersions,
            },
        });
        trackServerJob(response.job, kind === 'copy' ? '复制文件' : '移动文件');
        selectedPaths.clear();
        renderExplorer();
    } catch (error) {
        showApiError(error, kind === 'copy' ? '复制失败' : '移动失败');
    }
}

async function deleteSelection() {
    const entries = selectedFiles();
    if (!entries.length) {
        return;
    }
    if (entries.some(function (file) { return !getCapabilities(file).delete; })) {
        showToast('所选项目中包含没有删除权限的内容', 'error');
        return;
    }
    const hasPublic = entries.some(function (entry) { return entry.area === 'public'; });
    const open = entries.find(function (entry) {
        return (workspaceState.open_files || []).some(function (tab) {
            return tab.path === entry.path || tab.path.startsWith(entry.path + '/');
        });
    });
    if (open) {
        showToast('请先关闭所选项目中的文档标签', 'error');
        return;
    }
    const confirmed = await showConfirmDialog({
        title: hasPublic ? '永久删除公共文件？' : '移到回收站？',
        message: hasPublic
            ? `将永久删除 ${entries.length} 项公共内容，无法恢复。`
            : `将 ${entries.length} 项移到 private 回收站，30 天内可恢复。`,
        confirmLabel: hasPublic ? '永久删除' : '移到回收站',
        danger: true,
    });
    if (!confirmed) return;
    const versions = {};
    entries.forEach(function (item) { if (item.version) versions[item.path] = item.version; });
    await runImmediate('正在删除…', async function () {
        await apiJson('/delete', {
            method: 'POST',
            body: {
                session_id: state.currentSessionId,
                paths: entries.map(function (item) { return item.path; }),
                recursive: true,
                expected_versions: versions,
            },
        });
        selectedPaths.clear();
        showToast(hasPublic ? '已永久删除' : '已移到回收站', 'success');
    });
}

async function openTrash() {
    viewMode = 'trash';
    selectedPaths.clear();
    renderExplorer();
    try {
        const response = await apiGet('/trash', { session_id: state.currentSessionId });
        trashEntries = response.entries || [];
        renderExplorer();
    } catch (error) {
        showApiError(error, '无法打开回收站');
    }
}

async function restoreSelectedTrash(chooseTarget) {
    const ids = selectedTrashIds();
    if (!ids.length) return;
    let targetDir = '';
    let policy = 'fail';
    if (chooseTarget) {
        const result = await showFormDialog({
            title: '恢复到指定目录',
            confirmLabel: '恢复',
            fields: [
                { id: 'target', label: '目标目录', type: 'select', options: knownDirectories('private').map(function (item) { return { value: item.path, label: item.path }; }) },
                { id: 'policy', label: '同名处理', type: 'select', options: [
                    { value: 'keep_both', label: '保留两份' },
                    { value: 'overwrite', label: '覆盖现有项目' },
                    { value: 'skip', label: '跳过' },
                ] },
            ],
        });
        if (!result) return;
        targetDir = result.target;
        policy = result.policy;
    }
    await runImmediate('正在恢复…', async function () {
        await apiJson('/trash/restore', {
            method: 'POST',
            body: { session_id: state.currentSessionId, trash_ids: ids, target_dir: targetDir, conflict_policy: policy },
        });
        selectedPaths.clear();
        await openTrash();
        showToast('已恢复所选项目', 'success');
    });
}

async function purgeSelectedTrash() {
    const ids = selectedTrashIds();
    if (!ids.length) return;
    const confirmed = await showConfirmDialog({
        title: '永久删除？',
        message: `将永久删除 ${ids.length} 项内容，此操作无法撤销。`,
        confirmLabel: '永久删除',
        danger: true,
    });
    if (!confirmed) return;
    await runImmediate('正在清理回收站…', async function () {
        await apiJson('/trash/purge', {
            method: 'POST',
            body: { session_id: state.currentSessionId, trash_ids: ids },
        });
        selectedPaths.clear();
        await openTrash();
        showToast('已永久删除', 'success');
    });
}

async function showDetails(path) {
    closeDetailsDrawer();
    try {
        const item = await apiGet('/details', { session_id: state.currentSessionId, path: path });
        detailsDrawer = document.createElement('aside');
        detailsDrawer.className = 'workspace-fm-details';
        detailsDrawer.innerHTML = `
            <div class="workspace-fm-details-header"><strong>详细信息</strong><button type="button" aria-label="关闭">×</button></div>
            <div class="workspace-fm-details-icon">${fileIcon(item.path, item.is_dir)}</div>
            <h3>${escapeHtml(item.name)}</h3>
            <dl>
                <dt>位置</dt><dd>${escapeHtml(item.path)}</dd>
                <dt>类型</dt><dd>${item.is_dir ? '文件夹' : (escapeHtml(item.extension || '文件'))}</dd>
                <dt>大小</dt><dd>${formatBytes(item.size)}</dd>
                <dt>修改时间</dt><dd>${formatDate(item.modified_at)}</dd>
                <dt>区域</dt><dd>${item.area === 'public' ? '公共目录' : '私有目录'}</dd>
                ${item.is_dir ? `<dt>包含</dt><dd>${item.descendant_count} 项</dd>` : ''}
            </dl>`;
        detailsDrawer.querySelector('button').addEventListener('click', closeDetailsDrawer);
        explorer.appendChild(detailsDrawer);
    } catch (error) {
        showApiError(error, '读取详情失败');
    }
}

function closeDetailsDrawer() {
    if (detailsDrawer) detailsDrawer.remove();
    detailsDrawer = null;
}

async function runSearch() {
    const query = searchQuery.trim();
    if (!query || !state.currentSessionId) {
        searchResults = [];
        renderExplorer();
        return;
    }
    try {
        const response = await apiGet('/search', { session_id: state.currentSessionId, query: query, areas: 'private,public', limit: 200 });
        if (query !== searchQuery.trim()) return;
        searchResults = response.entries || [];
        renderExplorer();
    } catch (error) {
        showApiError(error, '搜索失败');
    }
}

function refreshWorkspace() {
    quota = null;
    send({ type: 'workspace_refresh' });
    refreshQuota();
    showToast('正在刷新工作空间', 'info');
}

async function refreshQuota() {
    const privateRoot = (workspaceState && workspaceState.files || []).find(function (file) {
        return file.is_dir && file.area === 'private' && isRoot(file.path);
    });
    if (!privateRoot || !state.currentSessionId) return;
    try {
        const response = await apiGet('/list', { session_id: state.currentSessionId, path: privateRoot.path, limit: 1 });
        quota = response.quota || null;
        renderQuotaOnly();
    } catch (_error) {
        // The tree remains usable if the optional quota summary cannot load.
    }
}

function renderQuotaOnly() {
    const existing = explorer && explorer.querySelector('.workspace-fm-quota');
    if (existing) existing.remove();
    if (explorer && quota && viewMode === 'tree') renderQuotaBar();
}

// ---------------------------------------------------------------------------
// Uploads and long-running tasks
// ---------------------------------------------------------------------------

function ensureUploadInputs() {
    uploadFileInput = document.createElement('input');
    uploadFileInput.type = 'file';
    uploadFileInput.multiple = true;
    uploadFileInput.hidden = true;
    uploadFileInput.addEventListener('change', function () {
        handlePickedFiles(uploadFileInput.files, false);
    });
    document.body.appendChild(uploadFileInput);

    uploadFolderInput = document.createElement('input');
    uploadFolderInput.type = 'file';
    uploadFolderInput.multiple = true;
    uploadFolderInput.hidden = true;
    uploadFolderInput.setAttribute('webkitdirectory', '');
    uploadFolderInput.addEventListener('change', function () {
        handlePickedFiles(uploadFolderInput.files, true);
    });
    document.body.appendChild(uploadFolderInput);

    replaceFileInput = document.createElement('input');
    replaceFileInput.type = 'file';
    replaceFileInput.hidden = true;
    replaceFileInput.addEventListener('change', function () {
        const replacement = pendingReplacement;
        const file = replaceFileInput.files && replaceFileInput.files[0];
        replaceFileInput.value = '';
        pendingReplacement = null;
        if (!replacement || !file) return;
        startChunkedUpload(
            [{ file: file, path: fileName(replacement.path) }],
            parentDir(replacement.path),
            'overwrite'
        );
    });
    document.body.appendChild(replaceFileInput);
}

async function replaceFileFlow(file) {
    if (!file || file.is_dir || !getCapabilities(file).replace) {
        showToast('当前文件没有替换权限', 'error');
        return;
    }
    if ((workspaceState.open_files || []).some(function (tab) { return tab.path === file.path; })) {
        showToast('请先关闭该文件的文档标签，避免覆盖未保存内容', 'error');
        return;
    }
    const confirmed = await showConfirmDialog({
        title: '上传新版本',
        message: `选择本地文件后将替换 ${file.path}。上传过程可暂停、取消并显示进度。`,
        confirmLabel: '选择文件',
    });
    if (!confirmed) return;
    pendingReplacement = file;
    replaceFileInput.value = '';
    replaceFileInput.click();
}

function openUploadPicker(folder, targetDir, resumeId) {
    if (!targetDir) {
        showToast('请选择 private 或 public 目录', 'error');
        return;
    }
    const target = findFile(targetDir);
    if (target && !getCapabilities(target).upload) {
        showToast('当前目录没有上传权限', 'error');
        return;
    }
    pendingUploadTarget = targetDir;
    pendingResumeUploadId = resumeId || '';
    const input = folder ? uploadFolderInput : uploadFileInput;
    input.value = '';
    input.click();
}

function handlePickedFiles(fileList, folder) {
    const entries = Array.from(fileList || []).map(function (file) {
        return {
            file: file,
            path: normalizeUploadPath(folder ? (file.webkitRelativePath || file.name) : file.name),
        };
    }).filter(function (item) { return item.path; });
    uploadFileInput.value = '';
    uploadFolderInput.value = '';
    if (!entries.length) return;
    if (pendingResumeUploadId) {
        const resumeId = pendingResumeUploadId;
        pendingResumeUploadId = '';
        resumeUpload(resumeId, entries);
    } else {
        startChunkedUpload(entries, pendingUploadTarget, 'fail');
    }
}

async function startChunkedUpload(entries, targetDir, conflictPolicy) {
    const invalid = entries.find(function (item) { return hasArchiveSuffix(item.path); });
    if (invalid) {
        showToast(`不允许上传归档文件：${invalid.path}`, 'error');
        return;
    }
    try {
        const initialized = await apiJson('/uploads', {
            method: 'POST',
            body: {
                session_id: state.currentSessionId,
                target_dir: targetDir,
                files: entries.map(function (item) {
                    return { path: item.path, size: item.file.size, last_modified: item.file.lastModified || 0 };
                }),
                conflict_policy: conflictPolicy,
            },
        });
        if (!initialized.upload_id) {
            showToast('同名文件已按策略跳过', 'info');
            return;
        }
        const task = createUploadTask(initialized, entries, targetDir);
        runUploadTask(task);
    } catch (error) {
        if (error.code === 'UPLOAD_CONFLICT') {
            const choice = await chooseConflictPolicy(error.details && error.details.conflicts || []);
            if (choice) startChunkedUpload(entries, targetDir, choice);
            return;
        }
        showApiError(error, '上传初始化失败');
    }
}

function createUploadTask(initialized, entries, targetDir) {
    const task = {
        id: `upload:${initialized.upload_id}`,
        uploadId: initialized.upload_id,
        type: 'upload',
        label: entries.length === 1 ? `上传 ${entries[0].path}` : `上传 ${entries.length} 个文件`,
        state: 'running',
        totalBytes: Number(initialized.total_bytes || 0),
        processedBytes: Number(initialized.uploaded_bytes || 0),
        currentPath: '',
        entries: entries,
        remoteFiles: initialized.files || [],
        chunkBytes: Number(initialized.chunk_bytes || 8 * 1024 * 1024),
        targetDir: targetDir,
        paused: false,
        cancelled: false,
        controllers: new Set(),
        startedAt: Date.now(),
    };
    tasks.set(task.id, task);
    persistUploadTasks();
    renderTasks();
    return task;
}

async function runUploadTask(task) {
    try {
        const queue = [];
        task.remoteFiles.forEach(function (remote) {
            const local = task.entries.find(function (entry) {
                return entry.path === remote.path && entry.file.size === Number(remote.size);
            });
            if (!local) throw new Error(`未找到匹配的本地文件：${remote.path}`);
            const completed = new Set((remote.completed_chunks || []).map(Number));
            for (let index = 0; index < Number(remote.chunk_count); index += 1) {
                if (!completed.has(index)) queue.push({ remote: remote, local: local, index: index });
            }
        });
        const workerCount = Math.min(3, queue.length || 1);
        let cursor = 0;
        const workers = Array.from({ length: workerCount }, async function () {
            while (cursor < queue.length) {
                if (task.cancelled) throw new Error('任务已取消');
                while (task.paused && !task.cancelled) await delay(160);
                const item = queue[cursor];
                cursor += 1;
                await uploadOneChunk(task, item);
            }
        });
        await Promise.all(workers);
        task.state = 'processing';
        task.currentPath = '服务端正在校验并写入文件';
        renderTasks();
        await apiJson(`/uploads/${task.uploadId}/complete`, {
            method: 'POST',
            body: { session_id: state.currentSessionId },
        });
        task.state = 'completed';
        task.processedBytes = task.totalBytes;
        task.currentPath = '';
        persistUploadTasks();
        renderTasks();
        refreshWorkspace();
        showToast('上传完成', 'success');
    } catch (error) {
        if (task.cancelled) task.state = 'cancelled';
        else {
            task.state = 'failed';
            task.error = error.message || '上传失败';
        }
        persistUploadTasks();
        renderTasks();
        if (!task.cancelled) showApiError(error, '上传失败');
    }
}

async function uploadOneChunk(task, item) {
    const start = item.index * task.chunkBytes;
    const end = Math.min(item.local.file.size, start + task.chunkBytes);
    const blob = item.local.file.slice(start, end);
    const body = await blob.arrayBuffer();
    const checksum = await sha256(body);
    task.currentPath = item.local.path;
    renderTasks();
    let lastError = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
            await xhrChunk(task, item.remote.id, item.index, body, checksum);
            task.processedBytes += body.byteLength;
            renderTasks();
            return;
        } catch (error) {
            lastError = error;
            if (task.cancelled) throw error;
            await delay(350 * (attempt + 1));
        }
    }
    throw lastError || new Error('分片上传失败');
}

function xhrChunk(task, fileId, chunkIndex, body, checksum) {
    return new Promise(function (resolve, reject) {
        const xhr = new XMLHttpRequest();
        const params = new URLSearchParams({ session_id: state.currentSessionId });
        xhr.open('PUT', `/api/workspace/files/uploads/${task.uploadId}/chunks/${fileId}/${chunkIndex}?${params}`);
        xhr.setRequestHeader('Authorization', `Bearer ${getToken()}`);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.setRequestHeader('X-Chunk-SHA256', checksum);
        task.controllers.add(xhr);
        xhr.upload.addEventListener('progress', function (event) {
            if (!event.lengthComputable) return;
            task.inflightBytes = event.loaded;
            renderTasks();
        });
        xhr.addEventListener('load', function () {
            task.controllers.delete(xhr);
            task.inflightBytes = 0;
            if (xhr.status >= 200 && xhr.status < 300) resolve();
            else reject(parseXhrError(xhr));
        });
        xhr.addEventListener('error', function () {
            task.controllers.delete(xhr);
            task.inflightBytes = 0;
            reject(new Error('网络连接中断'));
        });
        xhr.addEventListener('abort', function () {
            task.controllers.delete(xhr);
            task.inflightBytes = 0;
            reject(new Error('上传已取消'));
        });
        xhr.send(body);
    });
}

async function resumeUpload(uploadId, entries) {
    try {
        const initialized = await apiGet(`/uploads/${uploadId}`, { session_id: state.currentSessionId });
        const mismatch = initialized.files.find(function (remote) {
            return !entries.some(function (entry) {
                return entry.path === remote.path && entry.file.size === Number(remote.size);
            });
        });
        if (mismatch) throw new Error(`重新选择的文件与任务不匹配：${mismatch.path}`);
        const stored = readStoredUploads().find(function (item) { return item.uploadId === uploadId; });
        const task = createUploadTask(initialized, entries, stored ? stored.targetDir : getDefaultTargetDir());
        runUploadTask(task);
    } catch (error) {
        showApiError(error, '无法恢复上传');
    }
}

async function cancelUploadTask(task) {
    task.cancelled = true;
    task.controllers.forEach(function (xhr) { xhr.abort(); });
    try {
        await apiJson(`/uploads/${task.uploadId}?session_id=${encodeURIComponent(state.currentSessionId)}`, { method: 'DELETE' });
    } catch (_error) {
        // A missing/expired upload is already effectively cancelled.
    }
    task.state = 'cancelled';
    persistUploadTasks();
    renderTasks();
}

function restorePendingUploads() {
    readStoredUploads().filter(function (item) {
        return item.sessionId === state.currentSessionId;
    }).forEach(async function (stored) {
        try {
            const status = await apiGet(`/uploads/${stored.uploadId}`, { session_id: state.currentSessionId });
            const task = {
                id: `upload:${stored.uploadId}`,
                uploadId: stored.uploadId,
                type: 'upload',
                label: stored.label || '待恢复上传',
                state: 'waiting-source',
                totalBytes: status.total_bytes,
                processedBytes: status.uploaded_bytes,
                targetDir: stored.targetDir,
                folder: Boolean(stored.folder),
                remoteFiles: status.files || [],
                currentPath: '重新选择原文件后继续',
                controllers: new Set(),
            };
            tasks.set(task.id, task);
            renderTasks();
        } catch (_error) {
            removeStoredUpload(stored.uploadId);
        }
    });
}

function persistUploadTasks() {
    const stored = [];
    tasks.forEach(function (task) {
        if (task.type !== 'upload' || ['completed', 'cancelled'].includes(task.state)) return;
        stored.push({
            uploadId: task.uploadId,
            sessionId: state.currentSessionId,
            targetDir: task.targetDir,
            label: task.label,
            folder: task.entries ? task.entries.some(function (entry) { return entry.path.includes('/'); }) : task.folder,
        });
    });
    localStorage.setItem(STORED_UPLOADS_KEY, JSON.stringify(stored));
}

function readStoredUploads() {
    try { return JSON.parse(localStorage.getItem(STORED_UPLOADS_KEY) || '[]'); }
    catch (_error) { return []; }
}

function removeStoredUpload(uploadId) {
    const next = readStoredUploads().filter(function (item) { return item.uploadId !== uploadId; });
    localStorage.setItem(STORED_UPLOADS_KEY, JSON.stringify(next));
}

function trackServerJob(jobData, label) {
    const task = {
        id: `job:${jobData.id}`,
        jobId: jobData.id,
        type: 'job',
        label: label,
        state: jobData.state,
        processedBytes: 0,
        totalBytes: 0,
        currentPath: '',
    };
    tasks.set(task.id, task);
    renderTasks();
    pollServerJob(task);
}

async function pollServerJob(task) {
    try {
        const response = await apiGet(`/jobs/${task.jobId}`);
        const job = response.job;
        Object.assign(task, {
            state: job.state,
            processedBytes: job.processed_bytes,
            totalBytes: job.total_bytes,
            currentPath: job.current_path || '',
            error: job.error && job.error.message,
            result: job.result,
        });
        renderTasks();
        if (['queued', 'running', 'cancelling'].includes(job.state)) {
            setTimeout(function () { pollServerJob(task); }, 500);
        } else if (job.state === 'completed') {
            refreshWorkspace();
            showToast(`${task.label}完成`, 'success');
        }
    } catch (error) {
        task.state = 'failed';
        task.error = error.message;
        renderTasks();
    }
}

async function cancelServerJob(task) {
    try {
        const response = await apiJson(`/jobs/${task.jobId}`, { method: 'DELETE' });
        task.state = response.job.state;
        renderTasks();
    } catch (error) {
        showApiError(error, '取消任务失败');
    }
}

function renderTasks() {
    ensureGlobalUi();
    taskHost.innerHTML = '';
    const activeTasks = Array.from(tasks.values());
    if (!activeTasks.length) {
        taskHost.classList.remove('open');
        return;
    }
    const header = document.createElement('div');
    header.className = 'workspace-fm-task-header';
    header.innerHTML = '<strong>文件任务</strong>';
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.textContent = '清理已完成';
    clear.addEventListener('click', function () {
        tasks.forEach(function (task, id) {
            if (['completed', 'failed', 'cancelled'].includes(task.state)) tasks.delete(id);
        });
        renderTasks();
    });
    header.appendChild(clear);
    taskHost.appendChild(header);
    activeTasks.forEach(function (task) {
        const row = document.createElement('div');
        row.className = `workspace-fm-task ${task.state}`;
        const bytes = Number(task.processedBytes || 0) + Number(task.inflightBytes || 0);
        const percent = task.totalBytes ? Math.min(100, Math.round(bytes / task.totalBytes * 100)) : 0;
        const stateLabel = {
            queued: '等待中', running: '进行中', processing: '正在写入', paused: '已暂停',
            completed: '已完成', failed: '失败', cancelled: '已取消', cancelling: '正在取消',
            'waiting-source': '等待源文件',
        }[task.state] || task.state;
        row.innerHTML = `
            <div class="workspace-fm-task-title"><span>${escapeHtml(task.label)}</span><b>${stateLabel}</b></div>
            <div class="workspace-fm-task-progress"><span style="width:${percent}%"></span></div>
            <div class="workspace-fm-task-meta"><span>${percent}% · ${formatBytes(bytes)} / ${formatBytes(task.totalBytes)}</span><span>${escapeHtml(task.error || task.currentPath || '')}</span></div>`;
        const actions = document.createElement('div');
        actions.className = 'workspace-fm-task-actions';
        if (task.state === 'running' && task.type === 'upload') {
            actions.appendChild(taskButton('暂停', function () { task.paused = true; task.state = 'paused'; renderTasks(); }));
            actions.appendChild(taskButton('取消', function () { cancelUploadTask(task); }, true));
        } else if (task.state === 'paused') {
            actions.appendChild(taskButton('继续', function () { task.paused = false; task.state = 'running'; renderTasks(); }));
            actions.appendChild(taskButton('取消', function () { cancelUploadTask(task); }, true));
        } else if (task.state === 'waiting-source') {
            actions.appendChild(taskButton('重新选择源文件', function () {
                openUploadPicker(Boolean(task.folder), task.targetDir, task.uploadId);
            }));
            actions.appendChild(taskButton('取消', function () { cancelUploadTask(task); }, true));
        } else if (['queued', 'running'].includes(task.state) && task.type === 'job') {
            actions.appendChild(taskButton('取消', function () { cancelServerJob(task); }, true));
        }
        row.appendChild(actions);
        taskHost.appendChild(row);
    });
    taskHost.classList.add('open');
}

function toggleTaskPanel() {
    ensureGlobalUi();
    taskHost.classList.toggle('open');
}

// ---------------------------------------------------------------------------
// Drag-and-drop and downloads
// ---------------------------------------------------------------------------

function handleExternalDragEnter(event) {
    if (!hasExternalFiles(event.dataTransfer)) return;
    event.preventDefault();
    dragCounter += 1;
    explorer.classList.add('external-drag-active');
}

function handleExternalDragOver(event) {
    if (!hasExternalFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
}

function handleExternalDragLeave(event) {
    if (!hasExternalFiles(event.dataTransfer)) return;
    dragCounter = Math.max(0, dragCounter - 1);
    if (!dragCounter) explorer.classList.remove('external-drag-active');
}

async function handleExternalDrop(event) {
    if (!hasExternalFiles(event.dataTransfer)) return;
    event.preventDefault();
    dragCounter = 0;
    explorer.classList.remove('external-drag-active');
    const targetRow = event.target.closest('.workspace-fm-tree-row[data-kind="dir"]');
    const target = targetRow ? targetRow.dataset.path : getDefaultTargetDir();
    try {
        const entries = await collectDroppedFiles(event.dataTransfer);
        if (entries.length) startChunkedUpload(entries, target, 'fail');
    } catch (error) {
        showApiError(error, '读取拖拽文件失败');
    }
}

function hasExternalFiles(dataTransfer) {
    return dataTransfer && Array.from(dataTransfer.types || []).includes('Files');
}

async function collectDroppedFiles(dataTransfer) {
    const result = [];
    const items = Array.from(dataTransfer.items || []);
    const entryItems = items.map(function (item) {
        return item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    }).filter(Boolean);
    if (!entryItems.length) {
        return Array.from(dataTransfer.files || []).map(function (file) { return { file: file, path: file.name }; });
    }
    for (const entry of entryItems) await walkDroppedEntry(entry, '', result);
    return result;
}

async function walkDroppedEntry(entry, prefix, result) {
    const path = normalizeUploadPath(`${prefix}/${entry.name}`);
    if (entry.isFile) {
        const file = await new Promise(function (resolve, reject) { entry.file(resolve, reject); });
        result.push({ file: file, path: path });
        return;
    }
    if (!entry.isDirectory) return;
    const reader = entry.createReader();
    while (true) {
        const children = await new Promise(function (resolve, reject) { reader.readEntries(resolve, reject); });
        if (!children.length) break;
        for (const child of children) await walkDroppedEntry(child, path, result);
    }
}

async function downloadSelection() {
    const entries = selectedFiles();
    if (!entries.length) return;
    if (entries.length === 1 && !entries[0].is_dir) {
        downloadSingleFile(entries[0]);
        return;
    }
    try {
        const response = await apiJson('/downloads', {
            method: 'POST',
            body: { session_id: state.currentSessionId, paths: entries.map(function (item) { return item.path; }) },
        });
        const task = response.job;
        trackServerJob(task, '准备下载压缩包');
        waitAndDownloadJob(task.id);
    } catch (error) {
        showApiError(error, '创建下载任务失败');
    }
}

async function waitAndDownloadJob(jobId) {
    while (true) {
        await delay(600);
        try {
            const response = await apiGet(`/jobs/${jobId}`);
            if (response.job.state === 'completed') {
                await fetchDownload(`/api/workspace/files/jobs/${jobId}/result`, 'workspace-download.zip', `download:${jobId}`);
                return;
            }
            if (['failed', 'cancelled'].includes(response.job.state)) return;
        } catch (_error) { return; }
    }
}

async function downloadSingleFile(file) {
    const params = new URLSearchParams({ session_id: state.currentSessionId, path: file.path });
    await fetchDownload(`/api/workspace/files/download?${params}`, fileName(file.path), `download:${Date.now()}`);
}

async function fetchDownload(url, filename, taskId) {
    const task = {
        id: taskId,
        type: 'download',
        label: `下载 ${filename}`,
        state: 'running',
        processedBytes: 0,
        totalBytes: 0,
        currentPath: filename,
    };
    tasks.set(task.id, task);
    renderTasks();
    try {
        const response = await fetch(url, { headers: { Authorization: `Bearer ${getToken()}` } });
        if (!response.ok) throw await responseError(response);
        task.totalBytes = Number(response.headers.get('Content-Length') || 0);
        const chunks = [];
        if (response.body && response.body.getReader) {
            const reader = response.body.getReader();
            while (true) {
                const result = await reader.read();
                if (result.done) break;
                chunks.push(result.value);
                task.processedBytes += result.value.byteLength;
                renderTasks();
            }
        } else {
            const buffer = await response.arrayBuffer();
            chunks.push(new Uint8Array(buffer));
            task.processedBytes = buffer.byteLength;
        }
        const blob = new Blob(chunks, { type: response.headers.get('Content-Type') || 'application/octet-stream' });
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(objectUrl);
        task.state = 'completed';
        if (!task.totalBytes) task.totalBytes = task.processedBytes;
        renderTasks();
    } catch (error) {
        task.state = 'failed';
        task.error = error.message;
        renderTasks();
        showApiError(error, '下载失败');
    }
}

// ---------------------------------------------------------------------------
// Dialogs, notifications and common UI
// ---------------------------------------------------------------------------

function ensureGlobalUi() {
    if (!toastHost) {
        toastHost = document.createElement('div');
        toastHost.className = 'workspace-fm-toasts';
        toastHost.setAttribute('aria-live', 'polite');
        document.body.appendChild(toastHost);
    }
    if (!taskHost) {
        taskHost = document.createElement('aside');
        taskHost.className = 'workspace-fm-task-center';
        taskHost.setAttribute('aria-label', '文件任务');
        document.body.appendChild(taskHost);
    }
}

function showToast(message, type) {
    ensureGlobalUi();
    const toast = document.createElement('div');
    toast.className = `workspace-fm-toast ${type || 'info'}`;
    const icon = type === 'success' ? 'fa-circle-check' : (type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-info');
    toast.innerHTML = `<i class="fa-solid ${icon}"></i><span>${escapeHtml(message)}</span>`;
    toastHost.appendChild(toast);
    requestAnimationFrame(function () { toast.classList.add('visible'); });
    setTimeout(function () {
        toast.classList.remove('visible');
        setTimeout(function () { toast.remove(); }, 220);
    }, type === 'error' ? 5200 : 2800);
}

function showConfirmDialog(options) {
    return showFormDialog(Object.assign({}, options, { fields: [] })).then(function (result) { return Boolean(result); });
}

function showFormDialog(options) {
    closeDialog();
    return new Promise(function (resolve) {
        dialog = document.createElement('div');
        dialog.className = 'workspace-fm-dialog-backdrop';
        const card = document.createElement('form');
        card.className = 'workspace-fm-dialog';
        card.innerHTML = `
            <div class="workspace-fm-dialog-header"><h3>${escapeHtml(options.title || '文件操作')}</h3><button type="button" class="close" aria-label="关闭">×</button></div>
            ${options.message ? `<p class="workspace-fm-dialog-message">${escapeHtml(options.message)}</p>` : ''}
            <div class="workspace-fm-dialog-fields"></div>
            <div class="workspace-fm-dialog-actions"><button type="button" class="cancel">取消</button><button type="submit" class="confirm ${options.danger ? 'danger' : ''}">${escapeHtml(options.confirmLabel || '确定')}</button></div>`;
        const fields = card.querySelector('.workspace-fm-dialog-fields');
        (options.fields || []).forEach(function (field) {
            const label = document.createElement('label');
            label.textContent = field.label;
            let control;
            if (field.type === 'select') {
                control = document.createElement('select');
                (field.options || []).forEach(function (option) {
                    const element = document.createElement('option');
                    element.value = option.value;
                    element.textContent = option.label;
                    if (option.value === field.value) element.selected = true;
                    control.appendChild(element);
                });
            } else {
                control = document.createElement('input');
                control.type = field.type || 'text';
                control.value = field.value || '';
                control.required = Boolean(field.required);
                if (field.autofocus) control.autofocus = true;
            }
            control.name = field.id;
            label.appendChild(control);
            fields.appendChild(label);
        });
        function finish(value) {
            const current = dialog;
            dialog = null;
            if (current) current.remove();
            resolve(value);
        }
        card.querySelector('.close').addEventListener('click', function () { finish(null); });
        card.querySelector('.cancel').addEventListener('click', function () { finish(null); });
        card.addEventListener('submit', function (event) {
            event.preventDefault();
            const values = {};
            (options.fields || []).forEach(function (field) {
                values[field.id] = card.elements[field.id].value;
            });
            finish(values);
        });
        dialog.addEventListener('mousedown', function (event) {
            if (event.target === dialog) finish(null);
        });
        dialog.appendChild(card);
        document.body.appendChild(dialog);
        requestAnimationFrame(function () {
            const focus = card.querySelector('[autofocus], input, select, .confirm');
            if (focus) { focus.focus(); if (focus.select) focus.select(); }
        });
    });
}

function closeDialog() {
    if (dialog) dialog.remove();
    dialog = null;
}

async function chooseConflictPolicy(conflicts) {
    const preview = conflicts.slice(0, 6).map(function (item) { return item.target_path || item.path; }).join('\n');
    const result = await showFormDialog({
        title: '发现同名文件',
        message: `${preview}${conflicts.length > 6 ? `\n还有 ${conflicts.length - 6} 项` : ''}`,
        confirmLabel: '继续上传',
        fields: [{ id: 'policy', label: '处理方式', type: 'select', options: [
            { value: 'overwrite', label: '覆盖同名文件' },
            { value: 'keep_both', label: '保留两份' },
            { value: 'skip', label: '跳过同名文件' },
        ] }],
    });
    return result && result.policy;
}

async function runImmediate(message, operation) {
    const toast = document.createElement('div');
    ensureGlobalUi();
    toast.className = 'workspace-fm-toast info visible persistent';
    toast.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i><span>${escapeHtml(message)}</span>`;
    toastHost.appendChild(toast);
    try {
        await operation();
    } catch (error) {
        showApiError(error, '操作失败');
    } finally {
        toast.remove();
    }
}

function handleKeyboard(event) {
    if (!explorer || !explorer.offsetParent || dialog) return;
    const target = event.target;
    if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && viewMode !== 'tree') {
        event.preventDefault();
        selectedPaths = new Set(visiblePaths());
        renderExplorer();
    } else if (event.key === 'Escape' && selectedPaths.size) {
        selectedPaths.clear();
        renderExplorer();
    } else if (event.key === 'Delete' && selectedPaths.size) {
        event.preventDefault();
        if (viewMode === 'trash') purgeSelectedTrash();
        else deleteSelection();
    } else if (event.key === 'F2' && selectedPaths.size === 1 && viewMode !== 'trash') {
        event.preventDefault();
        renameFlow(selectedFiles()[0]);
    } else if (event.key === 'Enter' && selectedPaths.size === 1 && viewMode !== 'trash') {
        const file = selectedFiles()[0];
        if (file && file.is_dir) toggleDirectory(file.path);
        else if (file) openFile(file);
    }
}

function installLongPress(element, callback) {
    let timer = null;
    element.addEventListener('pointerdown', function (event) {
        if (event.pointerType === 'mouse') return;
        timer = setTimeout(function () {
            callback(event);
            timer = null;
        }, 560);
    });
    ['pointerup', 'pointercancel', 'pointermove'].forEach(function (type) {
        element.addEventListener(type, function () { if (timer) clearTimeout(timer); timer = null; });
    });
}

function openFile(file) {
    if (callbacks.openFile) callbacks.openFile(file.path);
}

function selectedFiles() {
    if (viewMode === 'search') {
        return searchResults.filter(function (item) { return selectedPaths.has(item.path); });
    }
    const map = new Map((workspaceState && workspaceState.files || []).map(function (file) { return [file.path, file]; }));
    return Array.from(selectedPaths).map(function (path) { return map.get(path); }).filter(Boolean);
}

function selectedTrashIds() {
    return Array.from(selectedPaths).filter(function (path) { return path.startsWith('trash:'); }).map(function (path) { return path.slice(6); });
}

function knownDirectories(area) {
    return (workspaceState && workspaceState.files || []).filter(function (file) {
        return file.is_dir && (!area || file.area === area);
    }).sort(function (a, b) { return a.path.localeCompare(b.path, 'zh-Hans-CN'); });
}

function getDefaultTargetDir() {
    const selected = selectedFiles()[0];
    if (selected && selected.is_dir) return selected.path;
    if (selected) return parentDir(selected.path);
    const privateRoot = knownDirectories('private').find(function (file) { return isRoot(file.path); });
    return privateRoot ? privateRoot.path : '';
}

function findFile(path) {
    return (workspaceState && workspaceState.files || []).find(function (file) { return file.path === path; }) || null;
}

function getCapabilities(file) {
    if (!file) return {};
    if (file.capabilities) {
        return Object.assign({
            replace: !file.is_dir && Boolean(file.capabilities.rename),
        }, file.capabilities);
    }
    const root = isRoot(file.path);
    return {
        read: file.can_read !== false,
        download: !root && file.can_read !== false,
        upload: Boolean(file.is_dir) && file.can_upload !== false,
        create_folder: Boolean(file.is_dir) && file.can_upload !== false && (
            file.area === 'private' || (file.can_rename !== false && file.can_delete !== false)
        ),
        rename: !root && file.can_rename !== false,
        copy: !root && file.can_read !== false,
        move: !root && file.can_delete !== false,
        delete: !root && file.can_delete !== false,
        replace: !root && !file.is_dir && file.can_rename !== false,
    };
}

function areaForPath(path) {
    const root = String(path || '').split('/')[0];
    const rootFile = findFile(root);
    return rootFile ? rootFile.area : 'private';
}

function permissionHint(allowed) {
    return allowed ? '' : '无权限';
}

function normalizeState(value) {
    return Object.assign({}, value || {}, {
        files: value && value.files || [],
        open_files: value && value.open_files || [],
        expanded_dirs: value && value.expanded_dirs || [],
    });
}

function parentDir(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    parts.pop();
    return parts.join('/');
}

function fileName(path) {
    return String(path || '').split('/').filter(Boolean).pop() || '';
}

function extension(path) {
    const name = fileName(path).toLowerCase();
    const dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(dot) : '';
}

function normalizeUploadPath(path) {
    return String(path || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
}

function hasArchiveSuffix(path) {
    const lower = String(path || '').toLowerCase();
    return ARCHIVE_SUFFIXES.some(function (suffix) { return lower.endsWith(suffix); });
}

function fileIcon(path, isDir, expanded) {
    if (isDir || path === 'folder') return `<i class="fa-solid ${expanded ? 'fa-folder-open' : 'fa-folder'} folder"></i>`;
    const ext = extension(path);
    if (['.doc', '.docx', '.odt', '.rtf'].includes(ext)) return '<i class="fa-solid fa-file-word word"></i>';
    if (['.xls', '.xlsx', '.ods', '.csv'].includes(ext)) return '<i class="fa-solid fa-file-excel excel"></i>';
    if (['.ppt', '.pptx', '.odp'].includes(ext)) return '<i class="fa-solid fa-file-powerpoint powerpoint"></i>';
    if (ext === '.pdf') return '<i class="fa-solid fa-file-pdf pdf"></i>';
    if (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].includes(ext)) return '<i class="fa-solid fa-file-image image"></i>';
    if (['.py', '.js', '.mjs', '.ts', '.css', '.html', '.json', '.yaml', '.yml', '.sql', '.sh'].includes(ext)) return '<i class="fa-solid fa-file-code code"></i>';
    if (['.md', '.markdown', '.txt'].includes(ext)) return '<i class="fa-solid fa-file-lines text"></i>';
    return '<i class="fa-regular fa-file"></i>';
}

function iconButton(title, icon, action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-fm-icon-button';
    button.title = title;
    button.setAttribute('aria-label', title);
    button.innerHTML = `<i class="${icon}"></i>`;
    button.addEventListener('click', function (event) { event.stopPropagation(); action(button, event); });
    return button;
}

function compactButton(label, icon, action, danger, disabled, disabledTitle) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `workspace-fm-compact-button ${danger ? 'danger' : ''}`;
    button.innerHTML = `<i class="${icon}"></i><span>${escapeHtml(label)}</span>`;
    button.disabled = Boolean(disabled);
    if (disabled && disabledTitle) button.title = disabledTitle;
    button.addEventListener('click', function () { if (!button.disabled) action(); });
    return button;
}

function taskButton(label, action, danger) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = danger ? 'danger' : '';
    button.textContent = label;
    button.addEventListener('click', action);
    return button;
}

function emptyState(title, hint) {
    const element = document.createElement('div');
    element.className = 'workspace-fm-empty';
    element.innerHTML = `<i class="fa-regular fa-folder-open"></i><strong>${escapeHtml(title)}</strong><span>${escapeHtml(hint || '')}</span>`;
    return element;
}

async function apiGet(route, params) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(function ([key, value]) {
        if (value !== undefined && value !== null) query.set(key, String(value));
    });
    return apiJson(`${route}${query.size ? `?${query}` : ''}`, { method: 'GET' });
}

async function apiJson(route, options) {
    const requestOptions = Object.assign({ method: 'GET' }, options || {});
    requestOptions.headers = Object.assign({ Authorization: `Bearer ${getToken()}` }, requestOptions.headers || {});
    if (requestOptions.body && !(requestOptions.body instanceof FormData) && typeof requestOptions.body !== 'string') {
        requestOptions.headers['Content-Type'] = 'application/json';
        requestOptions.body = JSON.stringify(requestOptions.body);
    }
    const response = await fetch(`/api/workspace/files${route}`, requestOptions);
    if (!response.ok) throw await responseError(response);
    if (response.status === 204) return {};
    return response.json();
}

async function responseError(response) {
    let data = null;
    try { data = await response.json(); } catch (_error) { data = null; }
    const detail = data && data.detail;
    const error = new Error(
        detail && typeof detail === 'object' ? (detail.message || '请求失败') :
            (detail || data && data.error || `请求失败 (${response.status})`)
    );
    if (detail && typeof detail === 'object') {
        error.code = detail.code;
        error.path = detail.path;
        error.details = detail.details;
        error.retryable = detail.retryable;
    }
    error.status = response.status;
    return error;
}

function parseXhrError(xhr) {
    try {
        const data = JSON.parse(xhr.responseText || '{}');
        const detail = data.detail || {};
        const error = new Error(detail.message || data.error || `上传失败 (${xhr.status})`);
        error.code = detail.code;
        error.details = detail.details;
        return error;
    } catch (_error) {
        return new Error(`上传失败 (${xhr.status})`);
    }
}

function showApiError(error, fallback) {
    const message = error && error.message ? error.message : fallback;
    showToast(`${fallback}：${message}`, 'error');
}

async function sha256(buffer) {
    if (!window.crypto || !window.crypto.subtle) return '';
    const hash = await crypto.subtle.digest('SHA-256', buffer);
    return Array.from(new Uint8Array(hash)).map(function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
}

function copyText(value) {
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).catch(function () {});
}

function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let amount = bytes / 1024;
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
}

function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, function (char) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
}

function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
}
