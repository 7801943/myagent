/**
 * onlyoffice-editor.js — OnlyOffice DocsAPI 封装
 * 职责：加载 DocumentServer api.js、拉取 editor config、创建/销毁编辑器。
 */

import { getToken } from './auth.js';

let apiPromise = null;
let editor = null;
let currentPath = '';
let openSequence = 0;
let containerCounter = 0;

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

    const force = Boolean(options && options.force);
    console.info('[OnlyOffice] open requested', { path: relativePath, mode: mode || 'edit', force: force });
    if (!force && currentPath === relativePath && editor) {
        console.info('[OnlyOffice] open skipped; document already active', { path: relativePath });
        return;
    }

    const sequence = ++openSequence;
    showStatus('正在打开文档...');
    closeDocument();

    try {
        const data = await fetchEditorConfig(relativePath, mode || 'edit');
        if (sequence !== openSequence) {
            console.info('[OnlyOffice] stale editor config ignored', { path: relativePath });
            return;
        }
        logEditorConfig(relativePath, data);
        await loadOnlyOfficeApi(data.onlyoffice_url);
        if (sequence !== openSequence) {
            console.info('[OnlyOffice] stale api load ignored', { path: relativePath });
            return;
        }

        const containerId = `onlyofficeEditorInstance${++containerCounter}`;
        host.innerHTML = `<div id="${containerId}" class="onlyoffice-editor-instance"></div>`;
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
        editor = new window.DocsAPI.DocEditor(containerId, data.config);
        currentPath = relativePath;
        showStatus('');
    } catch (err) {
        currentPath = '';
        host.innerHTML = '';
        console.error('[OnlyOffice] open document failed', { path: relativePath, error: err });
        showStatus(err.message || '文档打开失败');
    }
}

export function closeDocument() {
    if (editor && typeof editor.destroyEditor === 'function') {
        try {
            console.info('[OnlyOffice] destroying editor', { path: currentPath });
            editor.destroyEditor();
        } catch (e) {
            // 销毁失败不影响下一次重新创建编辑器。
        }
    }
    editor = null;
    currentPath = '';
}
