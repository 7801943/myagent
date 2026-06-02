/**
 * workspace.js — 工作区面板
 * 职责：折叠/展开、Tab 切换（通过 activity-bar 按钮）、布局模式切换
 */

let panel;
let chatPanel;
let layoutContainer;

export function initWorkspace() {
    panel = document.getElementById("workspacePanel");
    chatPanel = document.getElementById("chatPanel");
    layoutContainer = document.querySelector(".main-layout-container");

    // Tab 切换 — 绑定到 activity-bar 中的 ws-tab-btn 按钮
    const abTabBtns = document.querySelectorAll(".ab-item.ws-tab-btn");
    const contents = document.querySelectorAll(".workspace-tab-content");

    abTabBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
            const target = this.dataset.tab;
            // 更新 activity-bar 按钮状态
            abTabBtns.forEach(function (b) {
                b.classList.toggle("active", b.dataset.tab === target);
            });
            // 更新 workspace 面板内容
            contents.forEach(function (c) {
                c.classList.toggle("active", c.dataset.tab === target);
            });
        });
    });

    // ── Layout Mode Toggle ──
    initLayoutToggle();

    // 恢复上次布局状态
    const savedLayout = localStorage.getItem("myagent-layout-mode") || "split";
    applyLayoutMode(savedLayout);
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