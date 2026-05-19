/**
 * context-bar.js — 上下文进度条组件
 * 依赖：state.js
 */

import { state } from './state.js';

let contextProgressContainer;
let contextProgressFill;
let contextProgressLeft;
let contextProgressRight;

export function initContextBar() {
    contextProgressContainer = document.getElementById("contextProgressContainer");
    contextProgressFill = document.getElementById("contextProgressFill");
    contextProgressLeft = document.getElementById("contextProgressLeft");
    contextProgressRight = document.getElementById("contextProgressRight");
}

export function updateContextProgress(contextUsage) {
    if (!contextUsage) return;
    const used = contextUsage.used_tokens || 0;
    const total = contextUsage.context_window_size || state.contextWindowSize || 1;
    const pct = Math.min(used / total, 1);

    // 显示容器
    if (contextProgressContainer) {
        contextProgressContainer.classList.add("visible");
    }

    // 更新填充宽度
    if (contextProgressFill) {
        contextProgressFill.style.width = (pct * 100).toFixed(1) + "%";
        // 颜色：绿色 (hsl 152) → 橙色 (hsl 30) 基于百分比
        const hue = Math.round(152 - pct * 122);
        contextProgressFill.style.backgroundColor = "hsl(" + hue + ", 72%, 48%)";
    }

    // 更新标签
    if (contextProgressLeft) {
        const usedK = used >= 1000 ? (used / 1000).toFixed(1) + "K" : used;
        contextProgressLeft.textContent = usedK;
    }
    if (contextProgressRight) {
        const totalK = total >= 1000 ? (total / 1000).toFixed(0) + "K" : total;
        contextProgressRight.textContent = totalK;
    }
}

/**
 * 根据 Agent 状态控制进度条动画
 */
export function setStateAnimation(agentState) {
    if (!contextProgressFill) return;
    if (agentState === "thinking" || agentState === "generating" || agentState === "waiting_tool") {
        contextProgressFill.classList.add("animating");
    } else {
        contextProgressFill.classList.remove("animating");
    }
}