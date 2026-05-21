/**
 * app.js — 入口文件
 * 初始化各模块，绑定全局事件
 */

import { initMarkdown } from './markdown.js';
import { initTheme, initThemeToggle } from './theme.js';
import { initHeader } from './header.js';
import { initChat } from './chat.js';
import { initToolChip } from './tool-chip.js';
import { initContextBar } from './context-bar.js';
import { initRouter } from './router.js';
import { initWorkspace } from './workspace.js';
import { connect } from './connection.js';
import { on } from './state.js';

// ── 初始化 ──
// <script type="module"> 天然 defer，DOM 已就绪，无需 DOMContentLoaded

// 基础模块
initMarkdown();
initTheme();
initThemeToggle();

// UI 模块
initHeader();
initChat();
initToolChip();
initContextBar();
initWorkspace();

// 消息路由
initRouter();

// WebSocket 连接
connect();

// ── 全局快捷键注册 ──
window.addEventListener("keydown", function (e) {
    const isMeta = e.metaKey || e.ctrlKey;
    if (isMeta) {
        if (e.key.toLowerCase() === "e") {
            e.preventDefault();
            import('./header.js').then(function (m) {
                m.toggleSidebar();
            });
        } else if (e.key.toLowerCase() === "i") {
            e.preventDefault();
            import('./workspace.js').then(function (m) {
                m.toggleWorkspace();
            });
        } else if (e.key.toLowerCase() === "l") {
            e.preventDefault();
            const input = document.getElementById("userInput");
            if (input) input.focus();
        }
    }
});

