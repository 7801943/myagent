/**
 * MyAgent WebSocket Client — 原生 JS 实现
 * V4: Session management, optimized thinking/tool, HITL in-chip approval, centered status
 */
(function () {
    "use strict";

    // ── DOM Elements ──
    const $ = (sel) => document.querySelector(sel);
    const statusIndicator = $("#statusIndicator");
    const chatContainer = $("#chatContainer");
    const messageList = $("#messageList");
    const welcomeMsg = $("#welcomeMsg");
    const userInput = $("#userInput");
    const sendBtn = $("#sendBtn");
    const stopBtn = $("#stopBtn");
    const clearBtn = $("#clearBtn");
    const themeToggle = $("#themeToggle");
    const hitlModal = $("#hitlModal");
    const hitlToolName = $("#hitlToolName");
    const hitlReason = $("#hitlReason");
    const hitlArgs = $("#hitlArgs");
    const hitlApprove = $("#hitlApprove");
    const hitlReject = $("#hitlReject");

    // Sidebar elements
    const sidebar = $(".sidebar");
    const sidebarToggle = $("#sidebarToggle");
    const sidebarOpenBtn = $("#sidebarOpenBtn");
    const newChatBtn = $("#newChatBtn");
    const sessionListEl = $("#sessionList");

    // Header connection status elements
    const headerConnDot = $("#headerConnDot");
    const headerConnText = $("#headerConnText");

    // ── State ──
    let ws = null;
    let isConnected = false;
    let isProcessing = false;
    let currentAssistantEl = null;
    let currentContentEl = null;
    let currentThinkingList = null;
    let currentToolCallMap = {};
    let thinkingCount = 0;
    let toolCount = 0;
    let currentThinkingItem = null;
    let pendingHitlCallId = null;
    let reconnectTimer = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT = 10;
    const WS_URL = `ws://${window.location.hostname || "localhost"}:8765`;

    // Session state
    let currentSessionId = null;
    let sessions = [];

    // ── AgentState Display Map ──
    const STATE_MAP = {
        idle: { text: "空闲", css: "status-connected", emoji: "" },
        thinking: { text: "思考中...", css: "status-thinking", emoji: "🧠" },
        generating: { text: "生成中...", css: "status-running", emoji: "⚡" },
        waiting_tool: { text: "等待工具...", css: "status-waiting-tool", emoji: "🔧" },
        waiting_hitl: { text: "等待审批...", css: "status-waiting-hitl", emoji: "⏳" },
        error: { text: "错误", css: "status-error", emoji: "❌" },
    };


    // ── Sidebar Toggle ──
    function initSidebar() {
        const saved = localStorage.getItem("myagent-sidebar");
        if (saved === "collapsed") {
            sidebar.classList.add("collapsed");
            sidebarOpenBtn.style.display = "flex";
        }
    }

    sidebarToggle.addEventListener("click", function () {
        sidebar.classList.add("collapsed");
        sidebarOpenBtn.style.display = "flex";
        localStorage.setItem("myagent-sidebar", "collapsed");
    });

    sidebarOpenBtn.addEventListener("click", function () {
        sidebar.classList.remove("collapsed");
        sidebarOpenBtn.style.display = "none";
        localStorage.setItem("myagent-sidebar", "expanded");
    });

    // ── Session Management ──
    function requestSessionList() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "session_list" }));
        }
    }

    function renderSessionList() {
        sessionListEl.innerHTML = "";
        sessions.forEach(function (s) {
            const item = document.createElement("div");
            item.className = "session-item" + (s.session_id === currentSessionId ? " active" : "");
            item.dataset.sessionId = s.session_id;
            item.innerHTML = `
                <span class="session-item-icon">💬</span>
                <span class="session-item-title">${escapeHtml(s.title || "新对话")}</span>
                <button class="session-item-delete" title="删除会话" data-id="${s.session_id}">✕</button>
            `;
            // Click to switch
            item.addEventListener("click", function (e) {
                if (e.target.classList.contains("session-item-delete")) return;
                if (s.session_id !== currentSessionId && !isProcessing) {
                    switchSession(s.session_id);
                }
            });
            // Delete button
            const delBtn = item.querySelector(".session-item-delete");
            delBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                deleteSession(s.session_id);
            });
            sessionListEl.appendChild(item);
        });
    }

    function createNewSession() {
        if (isProcessing) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "session_create" }));
        }
    }

    function switchSession(sessionId) {
        if (isProcessing) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "session_switch", session_id: sessionId }));
        }
    }

    function deleteSession(sessionId) {
        if (isProcessing) return;
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "session_delete", session_id: sessionId }));
        }
    }

    newChatBtn.addEventListener("click", createNewSession);

    function loadHistoryMessages(messages) {
        messageList.innerHTML = "";

        let hasActiveAssistant = false;

        messages.forEach(function (msg) {
            try {
                if (msg.role === "user") {
                    appendUserMessage(msg.content);
                    hasActiveAssistant = false;
                } else if (msg.role === "assistant") {
                    if (!hasActiveAssistant) {
                        currentAssistantEl = createAssistantMessage();
                        currentContentEl = currentAssistantEl.querySelector(".message-content");
                        currentThinkingList = currentAssistantEl.querySelector(".msg-thinking-list");
                        currentToolCallMap = {};
                        thinkingCount = 0;
                        toolCount = 0;
                        currentThinkingItem = null;
                        hasActiveAssistant = true;
                    }

                    if (msg.metadata && msg.metadata.thinking) {
                        appendThinkingDelta(msg.metadata.thinking);
                        currentThinkingItem = null;
                    }

                    if (msg.tool_calls && msg.tool_calls.length > 0) {
                        msg.tool_calls.forEach(function (tc) {
                            let args = tc.arguments;
                            if (typeof args === "string") {
                                try { args = JSON.parse(args); } catch (e) { }
                            }
                            appendToolStart(tc.name || "tool", args, tc.id || ("hist_" + toolCount));
                        });
                    }

                    if (msg.content) {
                        let rawText = currentContentEl._rawText || "";
                        rawText += msg.content;
                        currentContentEl._rawText = rawText;
                        currentContentEl.innerHTML = renderMarkdown(rawText);
                        const replySection = currentContentEl.closest(".msg-reply-section");
                        if (replySection) replySection.style.display = "";
                    }
                } else if (msg.role === "tool") {
                    if (hasActiveAssistant) {
                        let latency = 0;
                        if (msg.metadata && msg.metadata.latency_ms) latency = msg.metadata.latency_ms;
                        let toolName = msg.tool_name || "tool";
                        let callId = msg.tool_call_id || "";
                        appendToolEnd(toolName, msg.content, latency, callId);
                    }
                }
            } catch (e) {
                console.error("Error loading history message:", e, msg);
            }
        });

        resetProcessingState();
        scrollToEnd(true);
    }

    // ── Markdown Renderer (marked + KaTeX) ──

    // Configure marked
    if (typeof marked !== "undefined") {
        marked.setOptions({
            breaks: true,
            gfm: true,
        });
    }

    /**
     * Extract math expressions from text, replace with placeholders.
     * Returns { text: string, mathBlocks: string[] }
     * Handles $$...$$ (display) and $...$ (inline), including multiline.
     */
    function extractMath(text) {
        const mathBlocks = [];
        // Match $$...$$ first (display math, greedy for multiline)
        text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (match, formula) {
            mathBlocks.push({ display: true, formula: formula.trim() });
            return "%%MATH_" + (mathBlocks.length - 1) + "%%";
        });
        // Match $...$ (inline math) — not preceded/followed by $ or digit
        text = text.replace(/(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)/g, function (match, formula) {
            mathBlocks.push({ display: false, formula: formula.trim() });
            return "%%MATH_" + (mathBlocks.length - 1) + "%%";
        });
        return { text: text, mathBlocks: mathBlocks };
    }

    /**
     * Render math placeholders back to KaTeX HTML.
     */
    function renderMathPlaceholders(html, mathBlocks) {
        if (!mathBlocks || mathBlocks.length === 0) return html;
        mathBlocks.forEach(function (block, idx) {
            var placeholder = "%%MATH_" + idx + "%%";
            try {
                var rendered = katex.renderToString(block.formula, {
                    displayMode: block.display,
                    throwOnError: false,
                    strict: false,
                });
                html = html.replace(placeholder, rendered);
            } catch (e) {
                // Fallback: show raw LaTeX in a code span
                var fallback = block.display
                    ? '<pre class="katex-fallback">' + escapeHtml(block.formula) + '</pre>'
                    : '<code class="katex-fallback">' + escapeHtml(block.formula) + '</code>';
                html = html.replace(placeholder, fallback);
            }
        });
        return html;
    }

    /**
     * Full Markdown + KaTeX rendering (used on message completion).
     */
    function renderMarkdown(text) {
        if (!text) return "";
        // If marked or katex not loaded, fallback to simple renderer
        if (typeof marked === "undefined" || typeof katex === "undefined") {
            return renderMarkdownSimple(text);
        }
        var extracted = extractMath(text);
        var mdHtml = marked.parse(extracted.text);
        var finalHtml = renderMathPlaceholders(mdHtml, extracted.mathBlocks);
        return finalHtml;
    }

    /**
     * Streaming Markdown rendering (no KaTeX for performance).
     * Math formulas shown as raw LaTeX during streaming.
     */
    function renderMarkdownStream(text) {
        if (!text) return "";
        if (typeof marked === "undefined") {
            return renderMarkdownSimple(text);
        }
        return marked.parse(text);
    }

    /**
     * Legacy simple renderer — fallback when libraries not loaded.
     */
    function renderMarkdownSimple(text) {
        if (!text) return "";
        var html = escapeHtml(text);
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
            return '<pre><code class="lang-' + (lang || "text") + '">' + code.trim() + '</code></pre>';
        });
        html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
        html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
        html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
        html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
        html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
        html = html.replace(/^[\-\*] (.+)$/gm, "<li>$1</li>");
        html = html.replace(/(<li>.*<\/li>)/s, "<ul>$1</ul>");
        html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
        html = html.replace(/^> (.+)$/gm, "<blockquote>$1</blockquote>");
        html = html.replace(/\n\n/g, "</p><p>");
        html = html.replace(/\n/g, "<br>");
        if (!html.startsWith("<")) {
            html = "<p>" + html + "</p>";
        }
        return html;
    }

    // ── WebSocket 连接 ──
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        try {
            ws = new WebSocket(WS_URL);
        } catch (e) {
            console.error("WebSocket creation failed:", e);
            appendErrorMessage("无法创建 WebSocket 连接");
            scheduleReconnect();
            return;
        }

        ws.onopen = function () {
            isConnected = true;
            reconnectAttempts = 0;
            setStatus("connected", "空闲");

            sendBtn.disabled = false;
            // Request session list on connect
            requestSessionList();
        };

        ws.onclose = function () {
            isConnected = false;
            setStatus("disconnected", "未连接");
            sendBtn.disabled = true;
            scheduleReconnect();
        };

        ws.onerror = function (err) {
            console.error("WebSocket error:", err);
            setStatus("disconnected", "连接错误");
        };

        ws.onmessage = function (event) {
            try {
                handleMessage(JSON.parse(event.data));
            } catch (e) {
                console.error("Failed to parse WebSocket message:", e, event.data);
            }
        };
    }

    function scheduleReconnect() {
        if (reconnectAttempts >= MAX_RECONNECT) {

            return;
        }
        reconnectAttempts++;
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 10000);

        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, delay);
    }

    // ── 发送消息 ──
    function sendMessage(text) {
        if (!text || !text.trim()) return;
        if (!isConnected) {
            appendErrorMessage("未连接到服务器，请等待连接恢复");
            return;
        }
        if (isProcessing) return;

        text = text.trim();
        welcomeMsg.classList.add("hidden");
        appendUserMessage(text);

        isProcessing = true;
        setStatus("processing", "处理中...");
        showStopButton();

        currentAssistantEl = createAssistantMessage();
        currentContentEl = currentAssistantEl.querySelector(".message-content");
        currentThinkingList = currentAssistantEl.querySelector(".msg-thinking-list");
        currentContentEl.classList.add("streaming-cursor");
        currentToolCallMap = {};
        thinkingCount = 0;
        toolCount = 0;
        currentThinkingItem = null;

        ws.send(JSON.stringify({ type: "chat", text: text }));
        userInput.value = "";
        autoResizeInput();
    }

    // ── 停止/恢复 ──
    function showStopButton() {
        sendBtn.style.display = "none";
        stopBtn.style.display = "flex";
    }

    function showSendButton() {
        stopBtn.style.display = "none";
        sendBtn.style.display = "flex";
        sendBtn.disabled = !isConnected;
    }

    stopBtn.addEventListener("click", function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "cancel" }));
        }
    });

    // ── 处理服务端消息 ──
    function handleMessage(data) {
        const type = data.type;

        switch (type) {
            case "connected":
                currentSessionId = data.session_id;
                console.log("Session:", data.session_id);
                requestSessionList();
                break;

            case "session_list_result":
                sessions = data.sessions || [];
                renderSessionList();
                break;

            case "session_created":
                currentSessionId = data.session_id;
                messageList.innerHTML = "";
                welcomeMsg.classList.remove("hidden");
                resetProcessingState();
                requestSessionList();
                break;

            case "session_switched":
                currentSessionId = data.session_id;
                resetProcessingState();
                welcomeMsg.classList.add("hidden");
                loadHistoryMessages(data.messages || []);
                requestSessionList();
                break;

            case "session_deleted":
                sessions = sessions.filter(function (s) { return s.session_id !== data.session_id; });
                if (data.session_id === currentSessionId) {
                    // If deleted current session, create new
                    createNewSession();
                }
                renderSessionList();
                break;

            case "state_change":
                handleStateChange(data.state);
                break;

            case "text_delta":
                appendTextDelta(data.text || "");
                break;

            case "thinking_delta":
                appendThinkingDelta(data.text || "");
                break;

            case "stream_start":
                break;

            case "stream_end":
                if (data.resuming) {
                    currentThinkingItem = null;
                }
                break;

            case "tool_start":
                appendToolStart(data.tool_name, data.args, data.call_id);
                break;

            case "tool_end":
                appendToolEnd(data.tool_name, data.result, data.latency_ms, data.call_id);
                break;

            case "tool_error":
                appendToolError(data.tool_name, data.error, data.call_id);
                break;

            case "safety_blocked":
                appendSafetyBlocked(data.rule, data.reason, data.action, data.call_id, data.tool_name);
                break;

            case "hitl_request":
                showHitlApproval(data.tool_name, data.reason, data.args, data.call_id);
                break;

            case "message_end":
                finalizeAssistantMessage(data.stop_reason || "completed");
                break;

            case "error":
                handleError(data.message);
                break;

            case "pong":
                break;

            default:
                console.warn("Unknown message type:", type, data);
        }
    }

    function resetProcessingState() {
        currentAssistantEl = null;
        currentContentEl = null;
        currentThinkingList = null;
        currentToolCallMap = {};
        currentThinkingItem = null;
        thinkingCount = 0;
        toolCount = 0;
        isProcessing = false;
        showSendButton();
    }

    // ── AgentState Display ──
    function handleStateChange(state) {
        const info = STATE_MAP[state];
        if (!info) {
            console.warn("Unknown agent state:", state);
            return;
        }

        statusIndicator.className = "status-badge " + info.css;
        statusIndicator.textContent = info.text;
    }

    // ── DOM 操作函数 ──

    function scrollToEnd(force = false) {
        requestAnimationFrame(function () {
            const isNearBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight < 100;
            if (force || isNearBottom) {
                chatContainer.scrollTop = chatContainer.scrollHeight;
            }
        });
    }

    function setStatus(cls, text) {
        statusIndicator.className = "status-badge status-" + cls;
        statusIndicator.textContent = text;

        // Only update header for connection-level changes
        if (cls === "connected" || cls === "disconnected") {
            if (headerConnDot) {
                headerConnDot.className = "header-conn-dot" + (cls === "connected" ? " connected" : "");
            }
            if (headerConnText) {
                headerConnText.textContent = cls === "connected" ? "已连接" : "未连接";
            }
        }
    }

    function appendUserMessage(text) {
        const el = document.createElement("div");
        el.className = "message message-user";
        el.innerHTML = `
            <div class="message-avatar">U</div>
            <div class="message-body">
                <div class="message-content">${escapeHtml(text)}</div>
            </div>
        `;
        messageList.appendChild(el);
        scrollToEnd(true);
    }

    function createAssistantMessage() {
        const el = document.createElement("div");
        el.className = "message message-assistant";
        el.innerHTML = `
            <div class="message-avatar">AI</div>
            <div class="message-body">
                <div class="msg-section msg-thinking-section" style="display:none;">
                    <div class="msg-section-label">💭 Thinking</div>
                    <div class="msg-thinking-list"></div>
                </div>
                <div class="msg-section msg-tools-section" style="display:none;">
                    <div class="msg-tools-container"></div>
                </div>
                <div class="msg-section msg-reply-section">
                    <div class="message-content"></div>
                </div>
            </div>
        `;

        messageList.appendChild(el);
        scrollToEnd(true);
        return el;
    }



    function appendTextDelta(text) {
        if (!currentContentEl) return;
        currentContentEl.classList.remove("streaming-cursor");
        let rawText = currentContentEl._rawText || "";
        rawText += text;
        currentContentEl._rawText = rawText;
        // Use stream renderer (no KaTeX) during streaming for performance
        currentContentEl.innerHTML = renderMarkdownStream(rawText);
        const replySection = currentContentEl.closest(".msg-reply-section");
        if (replySection) replySection.style.display = "";

        if (isProcessing) {
            currentContentEl.classList.add("streaming-cursor");
        }
        scrollToEnd(false);
    }

    function forceNewLine() {
        if (currentContentEl && currentContentEl._rawText && !currentContentEl._rawText.endsWith('\n\n')) {
            if (currentContentEl._rawText.endsWith('\n')) {
                appendTextDelta('\n');
            } else {
                appendTextDelta('\n\n');
            }
        }
    }

    function appendThinkingDelta(text) {
        if (!currentThinkingList) return;
        forceNewLine();

        const thinkingSection = currentThinkingList.closest(".msg-thinking-section");
        if (thinkingSection) thinkingSection.style.display = "";

        if (!currentThinkingItem) {
            thinkingCount++;
            currentThinkingItem = document.createElement("div");
            currentThinkingItem.className = "msg-thinking-item";
            currentThinkingItem.innerHTML = `
                <span class="thinking-num">#${thinkingCount}</span>
                <span class="thinking-text"></span>
            `;
            currentThinkingList.appendChild(currentThinkingItem);
        }

        const textSpan = currentThinkingItem.querySelector(".thinking-text");
        textSpan.textContent += text;

        // Auto-scroll thinking list to bottom
        currentThinkingList.scrollTop = currentThinkingList.scrollHeight;
        scrollToEnd(false);
    }

    // ── Tool Chip ──

    function appendToolStart(toolName, args, callId) {
        if (!currentAssistantEl) return;
        forceNewLine();

        const toolsSection = currentAssistantEl.querySelector(".msg-tools-section");
        if (toolsSection) toolsSection.style.display = "";

        const container = currentAssistantEl.querySelector(".msg-tools-container");
        if (!container) return;

        toolCount++;
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
                    <div class="tool-chip-result">等待执行...</div>
                </div>
            </div>
        `;

        const header = chipEl.querySelector(".tool-chip-header");
        header.addEventListener("click", function () {
            chipEl.classList.toggle("expanded");
        });

        container.appendChild(chipEl);
        currentToolCallMap[callId] = chipEl;
        scrollToEnd(true);
    }

    function appendToolEnd(toolName, result, latencyMs, callId) {
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

    function appendToolError(toolName, error, callId) {
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

    function appendSafetyBlocked(rule, reason, action, callId, toolName) {
        if (!currentAssistantEl) return;

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
            const bodyEl = currentAssistantEl.querySelector(".message-body");
            const el = document.createElement("div");
            el.className = "safety-blocked";
            el.innerHTML = html;
            bodyEl.appendChild(el);
        }
        scrollToEnd(true);
    }

    // ── HITL Approval — In Tool Chip ──
    function showHitlApproval(toolName, reason, args, callId) {
        pendingHitlCallId = callId;

        // Try to find an existing tool chip for this call_id
        let chipEl = currentToolCallMap[callId];

        // Create chip if not exists
        if (!chipEl) {
            const toolsSection = currentAssistantEl ? currentAssistantEl.querySelector(".msg-tools-section") : null;
            if (toolsSection) {
                toolsSection.style.display = "";
                const container = currentAssistantEl.querySelector(".msg-tools-container");
                if (container) {
                    toolCount++;
                    chipEl = document.createElement("div");
                    chipEl.className = "tool-chip";
                    chipEl.dataset.callId = callId;
                    currentToolCallMap[callId] = chipEl;

                    chipEl.innerHTML = `
                        <div class="tool-chip-header">
                            <span class="tool-chip-dot running"></span>
                            <span class="tool-chip-name">${escapeHtml(getToolLabel(toolName))}</span>
                            <span class="tool-chip-summary">${escapeHtml(getCommandSummary(toolName, args))}</span>
                            <span class="tool-chip-spinner"></span>
                        </div>
                        <div class="tool-chip-body">
                            <div class="tool-chip-body-inner">
                                <div class="tool-chip-params-title">调用参数</div>
                                <div class="tool-chip-params">${escapeHtml(args ? JSON.stringify(args, null, 2) : "{}")}</div>
                                <div class="tool-chip-result-title">执行结果</div>
                                <div class="tool-chip-result">等待审批...</div>
                            </div>
                        </div>
                    `;

                    const header = chipEl.querySelector(".tool-chip-header");
                    header.addEventListener("click", function (e) {
                        if (e.target.tagName !== "BUTTON") {
                            chipEl.classList.toggle("expanded");
                        }
                    });

                    container.appendChild(chipEl);
                }
            }
        }

        if (chipEl) {
            // Update existing chip with HITL status
            const statusEl = chipEl.querySelector(".tool-chip-status");
            if (statusEl) {
                statusEl.className = "tool-chip-status hitl";
                statusEl.textContent = "等待审批";
                statusEl.title = reason; // Show reason on hover
            }

            // Show reason in the body as well
            const resultEl = chipEl.querySelector(".tool-chip-result");
            if (resultEl) {
                resultEl.innerHTML = `<span style="color: var(--warning);">⚠️ 触发安全拦截: ${escapeHtml(reason)}</span>`;
            }

            // Inject buttons into the header
            const header = chipEl.querySelector(".tool-chip-header");
            const summary = header.querySelector(".tool-chip-summary");

            // Remove spinner
            const spinner = header.querySelector(".tool-chip-spinner");
            if (spinner) spinner.style.display = "none";

            // Create action container
            const actionContainer = document.createElement("div");
            actionContainer.className = "hitl-header-actions";
            actionContainer.innerHTML = `
                <button class="hitl-header-btn approve" data-call-id="${escapeHtml(callId)}">批准</button>
                <button class="hitl-header-btn reject" data-call-id="${escapeHtml(callId)}">拒绝(120s)</button>
            `;

            // Insert after summary
            header.insertBefore(actionContainer, summary.nextSibling);

            let timeLeft = 120;
            let timerInterval;

            const btnApprove = actionContainer.querySelector(".approve");
            const btnReject = actionContainer.querySelector(".reject");

            function handleResponse(approved) {
                clearInterval(timerInterval);
                if (ws) {
                    ws.send(JSON.stringify({
                        type: "hitl_response",
                        call_id: callId,
                        approved: approved,
                    }));
                }
                actionContainer.innerHTML = approved ?
                    '<span style="color: var(--success); font-size: 12px; margin-right: 8px;">✅ 已批准</span>' :
                    '<span style="color: var(--danger); font-size: 12px; margin-right: 8px;">❌ 已拒绝</span>';
            }

            btnApprove.addEventListener("click", function (e) {
                e.stopPropagation();
                handleResponse(true);
            });

            btnReject.addEventListener("click", function (e) {
                e.stopPropagation();
                handleResponse(false);
            });

            // Start countdown
            timerInterval = setInterval(() => {
                timeLeft--;
                if (timeLeft <= 0) {
                    handleResponse(false); // Auto reject
                } else {
                    btnReject.textContent = `拒绝(${timeLeft}s)`;
                }
            }, 1000);

            chipEl.classList.add("expanded");
        }
    }

    // ── HITL Global Modal (fallback) ──
    function showHitlModal(toolName, reason, args, callId) {
        hitlToolName.textContent = toolName;
        hitlReason.textContent = reason;
        hitlArgs.textContent = args ? JSON.stringify(args, null, 2) : "{}";
        hitlModal.style.display = "";
    }

    function hideHitlModal() {
        hitlModal.style.display = "none";
        pendingHitlCallId = null;
    }

    hitlApprove.addEventListener("click", function () {
        if (pendingHitlCallId && ws) {
            ws.send(JSON.stringify({
                type: "hitl_response",
                call_id: pendingHitlCallId,
                approved: true,
            }));
        }
        hideHitlModal();
    });

    hitlReject.addEventListener("click", function () {
        if (pendingHitlCallId && ws) {
            ws.send(JSON.stringify({
                type: "hitl_response",
                call_id: pendingHitlCallId,
                approved: false,
            }));
        }
        hideHitlModal();
    });

    function finalizeAssistantMessage(stopReason) {
        if (currentContentEl) {
            currentContentEl.classList.remove("streaming-cursor");
            const rawText = currentContentEl._rawText || "";
            if (rawText) {
                currentContentEl.innerHTML = renderMarkdown(rawText);
            }
        }

        if (stopReason === "cancelled") {
            if (currentContentEl && !currentContentEl._rawText) {
                currentContentEl.innerHTML = '<p style="color: var(--text-tertiary); font-style: italic;">⏹ 已停止</p>';
            }
        }

        if (stopReason !== "cancelled") {
            // Refresh session list to update titles
            requestSessionList();
        }

        currentAssistantEl = null;
        currentContentEl = null;
        currentThinkingList = null;
        currentToolCallMap = {};
        currentThinkingItem = null;
        thinkingCount = 0;
        toolCount = 0;
        isProcessing = false;

        showSendButton();

        setStatus("connected", "空闲");

        scrollToEnd(true);
    }

    function handleError(message) {
        if (currentContentEl) {
            currentContentEl.classList.remove("streaming-cursor");
        }

        if (currentAssistantEl && currentContentEl) {
            const rawText = currentContentEl._rawText || "";
            if (rawText) {
                currentContentEl.innerHTML = renderMarkdown(rawText);
            }
        }

        appendErrorMessage(message);
        currentAssistantEl = null;
        currentContentEl = null;
        currentThinkingList = null;
        currentToolCallMap = {};
        currentThinkingItem = null;
        thinkingCount = 0;
        toolCount = 0;
        isProcessing = false;
        showSendButton();
        setStatus("connected", "空闲");
    }

    function appendErrorMessage(message) {
        welcomeMsg.classList.add("hidden");
        const el = document.createElement("div");
        el.className = "message-error";
        el.innerHTML = `
            <div class="message-content">❌ ${escapeHtml(message)}</div>
        `;
        messageList.appendChild(el);
        scrollToEnd(true);
    }

    // ── Theme Toggle ──
    function initTheme() {
        const saved = localStorage.getItem("myagent-theme");
        if (saved) {
            document.documentElement.dataset.theme = saved;
        }
    }

    themeToggle.addEventListener("click", function () {
        const current = document.documentElement.dataset.theme;
        const next = current === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = next;
        localStorage.setItem("myagent-theme", next);
    });

    // ── Input Handling ──
    function autoResizeInput() {
        userInput.style.height = "auto";
        userInput.style.height = Math.min(userInput.scrollHeight, 120) + "px";
    }

    userInput.addEventListener("input", autoResizeInput);

    userInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage(userInput.value);
        }
    });

    sendBtn.addEventListener("click", function () {
        sendMessage(userInput.value);
    });

    // ── Clear Chat ──
    clearBtn.addEventListener("click", function () {
        messageList.innerHTML = "";
        currentAssistantEl = null;
        currentContentEl = null;
        currentThinkingList = null;
        currentToolCallMap = {};
        currentThinkingItem = null;
        thinkingCount = 0;
        toolCount = 0;
        isProcessing = false;
        showSendButton();
        welcomeMsg.classList.remove("hidden");
    });

    // ── Quick Prompts ──
    document.querySelectorAll(".quick-prompt").forEach(function (btn) {
        btn.addEventListener("click", function () {
            const text = this.dataset.text;
            if (text) sendMessage(text);
        });
    });

    // ── Tool Label / Summary helpers ──
    function getToolLabel(toolName) {
        const labels = {
            "cli_execute": "CLI",
            "file_read": "读取文件",
            "file_write": "写入文件",
        };
        return labels[toolName] || toolName;
    }

    function getCommandSummary(toolName, args) {
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

    // ── Utility ──
    function escapeHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    // ── Heartbeat ──
    setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
        }
    }, 30000);

    // ── Init ──
    initTheme();
    initSidebar();
    connect();
})();