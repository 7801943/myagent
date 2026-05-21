/**
 * context-bar.js — 全局 Token 用量进度条
 * 依赖：state.js
 */

import { state } from './state.js';

let tokenBarFill;

export function initContextBar() {
    tokenBarFill = document.getElementById("globalTokenBarFill");
}

export function updateContextProgress(contextUsage) {
    if (!contextUsage) return;
    const used = contextUsage.used_tokens || 0;
    const total = contextUsage.context_window_size || state.contextWindowSize || 1;
    const pct = Math.min(used / total, 1);

    // 更新全局 token bar 填充宽度
    if (tokenBarFill) {
        tokenBarFill.style.width = (pct * 100).toFixed(1) + "%";
        // 颜色：绿色 (hsl 152) → 橙色 (hsl 30) 基于百分比
        const hue = Math.round(152 - pct * 122);
        tokenBarFill.style.backgroundColor = "hsl(" + hue + ", 72%, 48%)";
    }
}

/**
 * 根据 Agent 状态控制进度条动画
 */
export function setStateAnimation(agentState) {
    if (!tokenBarFill) return;
    if (agentState === "thinking" || agentState === "generating" || agentState === "waiting_tool") {
        tokenBarFill.classList.add("animating");
    } else {
        tokenBarFill.classList.remove("animating");
    }
}