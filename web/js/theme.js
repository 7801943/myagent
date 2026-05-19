/**
 * theme.js — 主题切换
 * 只操作 localStorage 和 data-theme 属性
 */

export function initTheme() {
    const saved = localStorage.getItem("myagent-theme");
    if (saved) {
        document.documentElement.dataset.theme = saved;
    }
}

export function initThemeToggle() {
    const themeToggle = document.getElementById("themeToggle");
    if (!themeToggle) return;

    themeToggle.addEventListener("click", function () {
        const current = document.documentElement.dataset.theme;
        const next = current === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        localStorage.setItem("myagent-theme", next);
    });
}