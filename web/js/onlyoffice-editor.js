/**
 * onlyoffice-editor.js — OnlyOffice DocsAPI 封装
 * 职责：加载 DocumentServer api.js、拉取 editor config、创建/缓存/销毁编辑器。
 */

import { getToken } from './auth.js';
import { state } from './state.js';

let apiPromise = null;
let currentPath = '';
let openSequence = 0;
let pendingPath = '';
let containerCounter = 0;
const MAX_EDITOR_CACHE = 3;
const editorCache = new Map();

function showStatus(message) {
    const statusEl = document.getElementById('onlyofficeStatus');
    if (statusEl) statusEl.textContent = message || '';
}

function getContainer() {
    return document.getElementById('onlyofficeEditor');
}


function resolveBrowserOnlyOfficeUrl(onlyofficeUrl) {
    const url = new URL(onlyofficeUrl, window.location.href);
    const localHosts = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

    // 后端/容器可以用 localhost，但远程浏览器里的 localhost 是客户端自己。
    // 远程访问时改用当前页面的主机名，并保留 OnlyOffice 端口。
    if (localHosts.has(url.hostname)) {
        url.hostname = window.location.hostname;
        if (!url.port) url.port = '8081';
    }

    return url.origin;
}

function sanitizeUrl(url) {
    const parsed = new URL(url, window.location.href);
    if (parsed.searchParams.has('token')) {
        parsed.searchParams.set('token', '<redacted>');
    }
    return parsed.toString();
}

function logEditorConfig(relativePath, data) {
    const config = data.config || {};
    const document = config.document || {};
    const editorConfig = config.editorConfig || {};
    console.info('[OnlyOffice] editor config', {
        path: relativePath,
        onlyofficeUrl: resolveBrowserOnlyOfficeUrl(data.onlyoffice_url),
        documentType: config.documentType,
        fileType: document.fileType,
        key: document.key,
        documentUrl: document.url ? sanitizeUrl(document.url) : '',
        callbackUrl: editorConfig.callbackUrl ? sanitizeUrl(editorConfig.callbackUrl) : '',
        mode: editorConfig.mode,
        hasJwt: Boolean(config.token),
    });
}

function loadOnlyOfficeApi(onlyofficeUrl) {
    if (window.DocsAPI && window.DocsAPI.DocEditor) {
        return Promise.resolve();
    }
    if (apiPromise) return apiPromise;

    apiPromise = new Promise(function (resolve, reject) {
        const script = document.createElement('script');
        const browserOnlyOfficeUrl = resolveBrowserOnlyOfficeUrl(onlyofficeUrl);
        script.src = `${browserOnlyOfficeUrl}/web-apps/apps/api/documents/api.js`;
        console.info('[OnlyOffice] loading api.js', script.src);
        script.async = true;
        script.onload = function () { resolve(); };
        script.onerror = function () {
            console.error('[OnlyOffice] api.js load failed', script.src);
            reject(new Error('OnlyOffice API 加载失败'));
        };
        document.head.appendChild(script);
    });

    return apiPromise;
}

async function fetchEditorConfig(relativePath, mode) {
    const token = getToken();
    const params = new URLSearchParams({ path: relativePath, mode: mode || 'edit' });
    if (state.currentSessionId) params.set('session_id', state.currentSessionId);
    const response = await fetch(`/api/documents/editor-config?${params.toString()}`, {
        headers: { Authorization: 'Bearer ' + token },
    });

    if (!response.ok) {
        let message = '无法获取文档配置';
        try {
            const data = await response.json();
            message = data.detail || data.error || message;
        } catch (e) {
            // 保持默认错误文案。
        }
        throw new Error(message);
    }

    return response.json();
}

export async function openDocument(relativePath, mode, options) {
    const host = getContainer();
    if (!host || !relativePath) return;

    const sequence = ++openSequence;
    pendingPath = relativePath;
    const signature = options && options.signature ? String(options.signature) : '';
    const normalizedMode = mode || 'edit';
    console.info('[OnlyOffice] open requested', {
        path: relativePath,
        mode: normalizedMode,
        signature: signature,
    });

    const cached = editorCache.get(relativePath);
    if (cached) {
        pendingPath = '';
        activateCachedEditor(relativePath);
        console.info('[OnlyOffice] editor cache activated', {
            path: relativePath,
            containerId: cached.containerId,
            cachedSignature: cached.signature,
            requestedSignature: signature,
        });
        showStatus('');
        return;
    }

    showStatus('正在打开文档...');
    deactivateEditors();

    try {
        const data = await fetchEditorConfig(relativePath, normalizedMode);
        if (sequence !== openSequence) {
            if (pendingPath === relativePath) pendingPath = '';
            console.info('[OnlyOffice] stale editor config ignored', { path: relativePath });
            return;
        }
        logEditorConfig(relativePath, data);
        await loadOnlyOfficeApi(data.onlyoffice_url);
        if (sequence !== openSequence) {
            if (pendingPath === relativePath) pendingPath = '';
            console.info('[OnlyOffice] stale api load ignored', { path: relativePath });
            return;
        }

        const containerId = `onlyofficeEditorInstance${++containerCounter}`;
        const slotEl = document.createElement('div');
        slotEl.className = 'onlyoffice-editor-instance';
        slotEl.style.display = 'none';
        slotEl.style.visibility = 'hidden';
        slotEl.style.pointerEvents = 'none';
        slotEl.setAttribute('aria-hidden', 'true');

        const instanceEl = document.createElement('div');
        instanceEl.id = containerId;
        instanceEl.className = 'onlyoffice-editor-host';
        slotEl.appendChild(instanceEl);
        host.appendChild(slotEl);
        data.config.events = Object.assign({}, data.config.events || {}, {
            onAppReady: function () {
                console.info('[OnlyOffice] app ready', { path: relativePath });
            },
            onDocumentReady: function () {
                console.info('[OnlyOffice] document ready', { path: relativePath });
            },
            onDocumentStateChange: function (event) {
                console.info('[OnlyOffice] document state changed', {
                    path: relativePath,
                    isChanged: event && event.data,
                });
            },
            onError: function (event) {
                console.error('[OnlyOffice] editor error', { path: relativePath, event: event });
            },
        });
        console.info('[OnlyOffice] creating editor', { path: relativePath, containerId: containerId });
        const editor = new window.DocsAPI.DocEditor(containerId, data.config);
        editorCache.set(relativePath, {
            path: relativePath,
            mode: normalizedMode,
            signature: signature,
            editor: editor,
            containerId: containerId,
            element: slotEl,
            lastUsed: Date.now(),
        });
        pendingPath = '';
        activateCachedEditor(relativePath);
        evictOldEditors();
        showStatus('');
    } catch (err) {
        if (pendingPath === relativePath) pendingPath = '';
        currentPath = '';
        destroyCachedEditor(relativePath, 'open-failed');
        console.error('[OnlyOffice] open document failed', { path: relativePath, error: err });
        showStatus(err.message || '文档打开失败');
    }
}

export function activateDocument(relativePath) {
    if (!relativePath || !editorCache.has(relativePath)) return false;
    openSequence += 1;
    pendingPath = '';
    activateCachedEditor(relativePath);
    showStatus('');
    return true;
}

export function closeDocument(relativePath) {
    if (relativePath) {
        if (relativePath === currentPath || relativePath === pendingPath) {
            openSequence += 1;
            if (relativePath === pendingPath) pendingPath = '';
        }
        destroyCachedEditor(relativePath, 'document-closed');
        return;
    }
    openSequence += 1;
    pendingPath = '';
    currentPath = '';
    deactivateEditors();
}

export function closeAllDocuments() {
    openSequence += 1;
    pendingPath = '';
    Array.from(editorCache.keys()).forEach(function (path) {
        destroyCachedEditor(path, 'close-all');
    });
    currentPath = '';
    showStatus('');
}

function activateCachedEditor(relativePath) {
    const cached = editorCache.get(relativePath);
    if (!cached) return;
    deactivateEditors();
    setEditorVisibility(cached, true);
    cached.lastUsed = Date.now();
    currentPath = relativePath;
    requestAnimationFrame(function () {
        window.dispatchEvent(new Event('resize'));
        if (cached.editor && typeof cached.editor.resize === 'function') {
            try {
                cached.editor.resize();
            } catch (e) {
                console.warn('[OnlyOffice] resize editor failed', { path: relativePath, error: e });
            }
        }
    });
}

function deactivateEditors() {
    editorCache.forEach(function (cached) {
        setEditorVisibility(cached, false);
    });
}

function setEditorVisibility(cached, isActive) {
    if (!cached || !cached.element) return;
    cached.element.classList.toggle('active', isActive);
    cached.element.style.display = isActive ? 'block' : 'none';
    cached.element.style.visibility = isActive ? 'visible' : 'hidden';
    cached.element.style.pointerEvents = isActive ? 'auto' : 'none';
    cached.element.setAttribute('aria-hidden', isActive ? 'false' : 'true');
}

function evictOldEditors() {
    while (editorCache.size > MAX_EDITOR_CACHE) {
        const oldest = Array.from(editorCache.values())
            .filter(function (cached) { return cached.path !== currentPath; })
            .sort(function (a, b) { return a.lastUsed - b.lastUsed; })[0];
        if (!oldest) return;
        destroyCachedEditor(oldest.path, 'cache-limit');
    }
}

function destroyCachedEditor(relativePath, reason) {
    const cached = editorCache.get(relativePath);
    if (!cached) return;

    if (cached.editor && typeof cached.editor.destroyEditor === 'function') {
        try {
            console.info('[OnlyOffice] destroying editor', { path: relativePath, reason: reason });
            cached.editor.destroyEditor();
        } catch (e) {
            console.warn('[OnlyOffice] destroy editor failed', { path: relativePath, reason: reason, error: e });
        }
    }
    if (cached.element && cached.element.parentNode) {
        cached.element.parentNode.removeChild(cached.element);
    }
    editorCache.delete(relativePath);
    if (currentPath === relativePath) currentPath = '';
}
