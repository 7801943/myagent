/**
 * workspace.js — 工作区面板
 * 职责：折叠/展开、Tab 切换
 */

let panel;
let toggleBtn;
let closeBtn;

export function initWorkspace() {
    panel = document.getElementById("workspacePanel");
    toggleBtn = document.getElementById("workspaceToggle");
    closeBtn = document.getElementById("workspaceClose");

    // 折叠/展开
    if (toggleBtn) {
        toggleBtn.addEventListener("click", function () {
            if (panel) panel.classList.toggle("open");
            toggleBtn.classList.toggle("active", panel && panel.classList.contains("open"));
            // 持久化状态
            localStorage.setItem("myagent-workspace", panel && panel.classList.contains("open") ? "open" : "closed");
        });
    }

    if (closeBtn) {
        closeBtn.addEventListener("click", function () {
            if (panel) panel.classList.remove("open");
            if (toggleBtn) toggleBtn.classList.remove("active");
            localStorage.setItem("myagent-workspace", "closed");
        });
    }

    // Tab 切换
    const tabs = document.querySelectorAll(".workspace-tab");
    const contents = document.querySelectorAll(".workspace-tab-content");

    tabs.forEach(function (tab) {
        tab.addEventListener("click", function () {
            const target = this.dataset.tab;
            tabs.forEach(function (t) { t.classList.toggle("active", t.dataset.tab === target); });
            contents.forEach(function (c) { c.classList.toggle("active", c.dataset.tab === target); });
        });
    });

    // 恢复上次状态
    const saved = localStorage.getItem("myagent-workspace");
    if (saved === "open" && panel) {
        panel.classList.add("open");
        if (toggleBtn) toggleBtn.classList.add("active");
    }
}