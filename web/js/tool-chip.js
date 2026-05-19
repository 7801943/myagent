/**
 * tool-chip.js — Tool Chip 组件 + HITL 审批
 * 依赖：state.js, utils.js, connection.js
 * 不依赖 chat.js（通过参数接收 assistantEl）
 */

import { state } from './state.js';
import { escapeHtml, getToolLabel, getCommandSummary, scrollToEnd, HITL_TIMEOUT_SECONDS } from './utils.js';
import { send } from './connection.js';

// ── Tool Chip 内部状态 ──
let currentToolCallMap = {};
let toolCount = 0;
let pendingHitlCallId = null;

// ── HITL Modal DOM ──
let hitlModal;
let hitlToolName;
let hitlReason;
let hitlArgs;
let hitlApprove;
let hitlReject;

export function initToolChip() {
    hitlModal = document.getElementById("hitlModal");
    hitlToolName = document.getElementById("hitlToolName");
    hitlReason = document.getElementById("hitlReason");
    hitlArgs = document.getElementById("hitlArgs");
    hitlApprove = document.getElementById("hitlApprove");
    hitlReject = document.getElementById("hitlReject");

    // HITL Modal 事件
    if (hitlApprove) {
        hitlApprove.addEventListener("click", function () {
            if (pendingHitlCallId) {
                send({ type: "hitl_response", call_id: pendingHitlCallId, approved: true });
            }
            hideHitlModal();
        });
    }
    if (hitlReject) {
        hitlReject.addEventListener("click", function () {
            if (pendingHitlCallId) {
                send({ type: "hitl_response", call_id: pendingHitlCallId, approved: false });
            }
            hideHitlModal();
        });
    }
}

export function resetToolState() {
    currentToolCallMap = {};
    toolCount = 0;
    pendingHitlCallId = null;
}

// ── 创建 Tool Chip 的 HTML 模板（统一模板，消除重复） ──
function createChipEl(toolName, args, callId, resultText) {
    const argsStr = args ? JSON.stringify(args, null, 2) : "{}";

    const chipEl = document.createElement("div");
    chipEl.className = "tool-chip";
    chipEl.dataset.callId = callId;
    chipEl.dataset.toolIndex = toolCount;

    chipEl.innerHTML = `
        <div class="tool-chip-header">
            <span class="tool-chip-dot running"></span>
            <span class="tool-chip-name">${escapeHtml(getToolLabel(toolName))}</span>
            <span class="tool-chip-summary">${escapeHtml(getCommandSummary(toolName, args))}</span>
            <span class="tool-chip-spinner"></span>
        </div>
        <div class="tool-chip-body">
            <div class="tool-chip-body-inner">
                <div class="tool-chip-params-title">输入参数</div>
                <div class="tool-chip-params">${escapeHtml(argsStr)}</div>
                <div class="tool-chip-result-title">执行结果</div>
                <div class="tool-chip-result">${resultText || "等待执行..."}</div>
            </div>
        </div>
    `;

    const header = chipEl.querySelector(".tool-chip-header");
    header.addEventListener("click", function (e) {
        if (e.target.tagName !== "BUTTON") {
            chipEl.classList.toggle("expanded");
        }
    });

    return chipEl;
}

// ── Tool Chip 操作 ──

export function appendToolStart(assistantEl, toolName, args, callId) {
    if (!assistantEl) return;

    const toolsSection = assistantEl.querySelector(".msg-tools-section");
    if (toolsSection) toolsSection.style.display = "";

    const container = assistantEl.querySelector(".msg-tools-container");
    if (!container) return;

    toolCount++;
    const chipEl = createChipEl(toolName, args, callId);
    container.appendChild(chipEl);
    currentToolCallMap[callId] = chipEl;
    scrollToEnd(true);
}

export function appendToolEnd(toolName, result, latencyMs, callId) {
    const chipEl = currentToolCallMap[callId];
    if (!chipEl) return;

    const dot = chipEl.querySelector(".tool-chip-dot");
    if (dot) dot.classList.remove("running");

    const spinner = chipEl.querySelector(".tool-chip-spinner");
    if (spinner) {
        const latencyEl = document.createElement("span");
        latencyEl.className = "tool-chip-latency";
        latencyEl.textContent = latencyMs + "ms";
        spinner.replaceWith(latencyEl);
    }

    const resultEl = chipEl.querySelector(".tool-chip-result");
    if (resultEl) {
        resultEl.textContent = result || "(无输出)";
    }

    scrollToEnd(false);
}

export function appendToolError(toolName, error, callId) {
    const chipEl = currentToolCallMap[callId];
    if (!chipEl) return;

    const dot = chipEl.querySelector(".tool-chip-dot");
    if (dot) {
        dot.classList.remove("running");
        dot.classList.add("error");
    }

    const spinner = chipEl.querySelector(".tool-chip-spinner");
    if (spinner) spinner.remove();

    const resultEl = chipEl.querySelector(".tool-chip-result");
    if (resultEl) {
        resultEl.textContent = "Error: " + (error || "未知错误");
        resultEl.style.color = "var(--danger)";
    }

    chipEl.classList.add("expanded");
    scrollToEnd(false);
}

export function appendSafetyBlocked(assistantEl, rule, reason, action, callId, toolName) {
    if (!assistantEl) return;

    const html = `🛡️ <strong>安全拦截</strong>: ${escapeHtml(reason)} <br><small>规则: ${escapeHtml(rule)} | 动作: ${escapeHtml(action)}</small>`;

    if (callId && currentToolCallMap[callId]) {
        const chipEl = currentToolCallMap[callId];
        const inner = chipEl.querySelector(".tool-chip-body-inner");
        if (inner) {
            const el = document.createElement("div");
            el.className = "safety-blocked in-chip";
            el.innerHTML = html;
            inner.appendChild(el);
        }
        chipEl.classList.add("expanded");
        const dot = chipEl.querySelector(".tool-chip-dot");
        if (dot) {
            dot.classList.remove("running");
            dot.classList.add("error");
        }
    } else {
        const bodyEl = assistantEl.querySelector(".message-body");
        const el = document.createElement("div");
        el.className = "safety-blocked";
        el.innerHTML = html;
        bodyEl.appendChild(el);
    }
    scrollToEnd(true);
}

// ── HITL In-Chip 审批 ──

export function showHitlApproval(assistantEl, toolName, reason, args, callId) {
    pendingHitlCallId = callId;

    // 尝试找到已有的 tool chip
    let chipEl = currentToolCallMap[callId];

    // 如果没有对应 chip，创建一个
    if (!chipEl) {
        const toolsSection = assistantEl ? assistantEl.querySelector(".msg-tools-section") : null;
        if (toolsSection) {
            toolsSection.style.display = "";
            const container = assistantEl.querySelector(".msg-tools-container");
            if (container) {
                toolCount++;
                chipEl = createChipEl(toolName, args, callId, "等待审批...");
                container.appendChild(chipEl);
                currentToolCallMap[callId] = chipEl;
            }
        }
    }

    if (chipEl) {
        // 更新已有 chip 的 HITL 状态
        const statusEl = chipEl.querySelector(".tool-chip-status");
        if (statusEl) {
            statusEl.className = "tool-chip-status hitl";
            statusEl.textContent = "等待审批";
            statusEl.title = reason;
        }

        // 在 body 中显示原因
        const resultEl = chipEl.querySelector(".tool-chip-result");
        if (resultEl) {
            resultEl.innerHTML = `<span style="color: var(--warning);">⚠️ 触发安全拦截: ${escapeHtml(reason)}</span>`;
        }

        // 在 header 中注入审批按钮
        const header = chipEl.querySelector(".tool-chip-header");
        const summary = header.querySelector(".tool-chip-summary");

        // 隐藏 spinner
        const spinner = header.querySelector(".tool-chip-spinner");
        if (spinner) spinner.style.display = "none";

        // 创建操作按钮容器
        const actionContainer = document.createElement("div");
        actionContainer.className = "hitl-header-actions";
        actionContainer.innerHTML = `
            <button class="hitl-header-btn approve" data-call-id="${escapeHtml(callId)}">批准</button>
            <button class="hitl-header-btn reject" data-call-id="${escapeHtml(callId)}">拒绝(${HITL_TIMEOUT_SECONDS}s)</button>
        `;

        header.insertBefore(actionContainer, summary.nextSibling);

        let timeLeft = HITL_TIMEOUT_SECONDS;
        let timerInterval;

        const btnApprove = actionContainer.querySelector(".approve");
        const btnReject = actionContainer.querySelector(".reject");

        function handleResponse(approved) {
            clearInterval(timerInterval);
            send({ type: "hitl_response", call_id: callId, approved: approved });
            actionContainer.innerHTML = approved
                ? '<span style="color: var(--success); font-size: 12px; margin-right: 8px;">✅ 已批准</span>'
                : '<span style="color: var(--danger); font-size: 12px; margin-right: 8px;">❌ 已拒绝</span>';
        }

        btnApprove.addEventListener("click", function (e) {
            e.stopPropagation();
            handleResponse(true);
        });

        btnReject.addEventListener("click", function (e) {
            e.stopPropagation();
            handleResponse(false);
        });

        // 开始倒计时
        timerInterval = setInterval(() => {
            timeLeft--;
            if (timeLeft <= 0) {
                handleResponse(false); // 超时自动拒绝
            } else {
                btnReject.textContent = `拒绝(${timeLeft}s)`;
            }
        }, 1000);

        chipEl.classList.add("expanded");
    } else {
        // 无法创建 in-chip 审批时，降级到全局 modal
        showHitlModal(toolName, reason, args, callId);
    }
}

// ── HITL 全局 Modal（降级方案） ──

export function showHitlModal(toolName, reason, args, callId) {
    pendingHitlCallId = callId;
    if (hitlToolName) hitlToolName.textContent = toolName;
    if (hitlReason) hitlReason.textContent = reason;
    if (hitlArgs) hitlArgs.textContent = args ? JSON.stringify(args, null, 2) : "{}";
    if (hitlModal) hitlModal.style.display = "";
}

function hideHitlModal() {
    if (hitlModal) hitlModal.style.display = "none";
    pendingHitlCallId = null;
}