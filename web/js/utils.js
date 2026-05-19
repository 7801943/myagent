/**
 * utils.js — 工具函数（零依赖）
 */

export const STATE_MAP = {
    idle: { text: "空闲", css: "status-connected", emoji: "" },
    thinking: { text: "思考中...", css: "status-thinking", emoji: "🧠" },
    generating: { text: "生成中...", css: "status-running", emoji: "⚡" },
    waiting_tool: { text: "等待工具...", css: "status-waiting-tool", emoji: "🔧" },
    waiting_hitl: { text: "等待审批...", css: "status-waiting-hitl", emoji: "⏳" },
    error: { text: "错误", css: "status-error", emoji: "❌" },
};

export const HITL_TIMEOUT_SECONDS = 120;

export function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

export function getToolLabel(toolName) {
    const labels = {
        "cli_execute": "CLI",
        "file_read": "读取文件",
        "file_write": "写入文件",
    };
    return labels[toolName] || toolName;
}

export function getCommandSummary(toolName, args) {
    if (!args) return "";
    if (typeof args === "string") {
        try { args = JSON.parse(args); } catch (e) { return args.substring(0, 50); }
    }
    if (toolName === "cli_execute" && args.command) {
        return args.command.substring(0, 60);
    }
    if (toolName === "file_read" && args.path) {
        return args.path;
    }
    if (toolName === "file_write" && args.path) {
        return args.path;
    }
    try {
        return JSON.stringify(args).substring(0, 60);
    } catch (e) {
        return "";
    }
}

export function scrollToEnd(force = false) {
    const chatContainer = document.getElementById("chatContainer");
    if (!chatContainer) return;
    requestAnimationFrame(function () {
        const isNearBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight < 100;
        if (force || isNearBottom) {
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }
    });
}