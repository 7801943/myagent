import { send } from './connection.js';

const CHANNEL = 'myagent-onlyoffice-bridge';
const BRIDGE_REVISION = 2;
const editors = new Map();
let activePath = '';
let heartbeatTimer = null;

export function reportOnlyOfficeDiagnostic(phase, entryOrPath, message, requestId) {
    const path = typeof entryOrPath === 'string'
        ? entryOrPath
        : (entryOrPath && entryOrPath.path) || '';
    send({
        type: 'onlyoffice_bridge_diagnostic',
        phase: String(phase || '').slice(0, 80),
        path: String(path || '').slice(0, 1000),
        request_id: String(requestId || '').slice(0, 128),
        message: String(message || '').slice(0, 500),
    });
}

const diagnostic = reportOnlyOfficeDiagnostic;

function snapshot() {
    return {
        type: 'onlyoffice_client_state',
        visible: document.visibilityState === 'visible',
        last_active_ms: Date.now(),
        bridge_revision: BRIDGE_REVISION,
        editors: Array.from(editors.values()).map(function (entry) {
            return {
                path: entry.path,
                document_key: entry.documentKey,
                editor_session_id: entry.editorSessionId,
                ready: entry.ready,
                active: entry.path === activePath,
                writable: entry.writable,
                error: entry.error || '',
            };
        }),
    };
}

export function reportOnlyOfficeState() { send(snapshot()); }

export function registerOnlyOfficeAutomation(path, automation) {
    if (!automation) return;
    editors.set(path, {
        path,
        documentKey: automation.document_key,
        editorSessionId: automation.editor_session_id,
        nonce: automation.nonce,
        pluginGuid: automation.plugin_guid,
        writable: automation.writable === true,
        ready: false,
        error: '',
        pluginWindow: null,
    });
    diagnostic('editor_registered', path, `editor=${String(automation.editor_session_id || '').slice(0, 12)} writable=${automation.writable === true}`);
    reportOnlyOfficeState();
}

export function unregisterOnlyOfficeAutomation(path, editorSessionId) {
    const entry = editors.get(path);
    if (editorSessionId && entry && entry.editorSessionId !== editorSessionId) return;
    diagnostic('editor_unregistered', entry || path, `editor=${String((entry && entry.editorSessionId) || '').slice(0, 12)}`);
    editors.delete(path);
    if (activePath === path) activePath = '';
    reportOnlyOfficeState();
}

export function activateOnlyOfficeAutomation(path) {
    activePath = path || '';
    reportOnlyOfficeState();
}

export function handleOnlyOfficeServerMessage(data) {
    if (data.type === 'onlyoffice_state_request') {
        diagnostic('state_request_received', activePath, `editors=${editors.size}`);
        reportOnlyOfficeState();
        return;
    }
    if (data.type === 'onlyoffice_open_request') {
        diagnostic('open_request_received', data.path, `existing=${editors.has(data.path)}`);
        send({ type: 'workspace_open_file', path: data.path, open_with: 'onlyoffice' });
        const entry = editors.get(data.path);
        if (entry && !entry.ready) {
            diagnostic('existing_editor_not_ready', entry, entry.error || 'waiting for automation plugin');
        }
        return;
    }
    if (data.type !== 'onlyoffice_bridge_command') return;
    const request = data.request || {};
    const entry = editors.get(request.document && request.document.path);
    diagnostic('bridge_command_received', entry || (request.document && request.document.path) || '', request.command || '', request.request_id);
    if (!entry || !entry.ready || !entry.pluginWindow || entry.documentKey !== request.document.document_key || entry.editorSessionId !== request.editor_session_id) {
        diagnostic(
            'bridge_command_rejected',
            entry || (request.document && request.document.path) || '',
            `entry=${Boolean(entry)} ready=${Boolean(entry && entry.ready)} pluginWindow=${Boolean(entry && entry.pluginWindow)} keyMatch=${Boolean(entry && entry.documentKey === request.document.document_key)} editorMatch=${Boolean(entry && entry.editorSessionId === request.editor_session_id)}`,
            request.request_id,
        );
        send({ type: 'onlyoffice_bridge_response', response: {
            request_id: request.request_id || '', ok: false, result: null,
            error: { code: entry ? 'PLUGIN_NOT_READY' : 'DOCUMENT_MISMATCH', message: '目标 OnlyOffice 编辑器未就绪或实例已变化' },
        }});
        return;
    }
    entry.pluginWindow.postMessage({ channel: CHANNEL, type: 'command', nonce: entry.nonce, request }, window.location.origin);
    diagnostic('bridge_command_posted_to_plugin', entry, request.command || '', request.request_id);
}

window.addEventListener('message', function (event) {
    const message = event.data;
    if (!message || message.channel !== CHANNEL) return;
    if (event.origin !== window.location.origin) {
        diagnostic('plugin_message_rejected_origin', message.document && message.document.path, `actual=${event.origin} expected=${window.location.origin}`);
        return;
    }
    const path = message.document && message.document.path;
    const entry = editors.get(path);
    if (!entry) {
        diagnostic('plugin_message_rejected_missing_editor', path, message.type || 'unknown');
        return;
    }
    if (message.nonce !== entry.nonce || message.editor_session_id !== entry.editorSessionId || message.document.document_key !== entry.documentKey) {
        diagnostic('plugin_message_rejected_identity', entry, message.type || 'unknown');
        return;
    }
    if (message.type === 'ready') {
        if (message.plugin_guid !== entry.pluginGuid || message.office_api_ready !== true || !event.source) {
            diagnostic('plugin_ready_rejected', entry, `guidMatch=${message.plugin_guid === entry.pluginGuid} apiReady=${message.office_api_ready === true} source=${Boolean(event.source)}`);
            return;
        }
        entry.pluginWindow = event.source;
        entry.ready = true;
        entry.error = '';
        entry.writable = message.writable === true;
        entry.pluginWindow.postMessage({ channel: CHANNEL, type: 'ack', nonce: entry.nonce, editor_session_id: entry.editorSessionId }, window.location.origin);
        diagnostic('plugin_ready_accepted', entry, `editorType=${message.editor_type || 'unknown'} writable=${entry.writable}`);
        reportOnlyOfficeState();
        return;
    }
    if (message.type === 'error') {
        if (!event.source) return;
        entry.pluginWindow = event.source;
        entry.ready = false;
        entry.error = String(message.message || 'OnlyOffice 插件初始化失败');
        diagnostic('plugin_error', entry, entry.error);
        reportOnlyOfficeState();
        return;
    }
    if (message.type === 'diagnostic') {
        diagnostic(`plugin_${message.phase || 'diagnostic'}`, entry, message.message || '');
        return;
    }
    if (event.source !== entry.pluginWindow) return;
    if (message.type === 'response') {
        diagnostic('plugin_response_received', entry, `ok=${Boolean(message.response && message.response.ok)}`, message.response && message.response.request_id);
        send({ type: 'onlyoffice_bridge_response', response: message.response });
    }
});

document.addEventListener('visibilitychange', reportOnlyOfficeState);
['pointerdown', 'keydown'].forEach(function (eventName) {
    document.addEventListener(eventName, reportOnlyOfficeState, { passive: true });
});

export function initOnlyOfficeAutomation() {
    if (heartbeatTimer) return;
    heartbeatTimer = setInterval(reportOnlyOfficeState, 15000);
}
