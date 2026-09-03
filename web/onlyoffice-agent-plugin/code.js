(() => {
    "use strict";

    const CHANNEL = "myagent-onlyoffice-bridge";
    let token = "";
    const pendingDiagnostics = [];
    const state = {
        runtime: null,
        runtimePromise: null,
        acknowledged: false,
        completed: new Map(),
        readyTimer: null,
        started: false,
        readyLogged: false,
    };

    function sendDiagnostic(phase, message) {
        fetch("/api/documents/plugin-diagnostic", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-OnlyOffice-Plugin-Token": token,
            },
            body: JSON.stringify({
                phase: String(phase || "").slice(0, 80),
                message: String(message || "").slice(0, 500),
            }),
        }).catch(() => {});
    }

    function diagnostic(phase, message = "") {
        const safePhase = String(phase || "").slice(0, 80);
        const safeMessage = String(message || "").slice(0, 500);
        if (!token) {
            if (pendingDiagnostics.length < 20) pendingDiagnostics.push([safePhase, safeMessage]);
            return;
        }
        sendDiagnostic(safePhase, safeMessage);
    }

    function pluginToken() {
        const value = Asc.plugin.info?.options?.token;
        return typeof value === "string" ? value : "";
    }

    function setPluginToken(value) {
        token = String(value || "");
        if (!token) return;
        while (pendingDiagnostics.length) sendDiagnostic(...pendingDiagnostics.shift());
    }

    diagnostic("script_loaded", "automation plugin code executing");
    window.addEventListener("error", (event) => diagnostic("window_error", event.message || "unknown error"));
    window.addEventListener("unhandledrejection", (event) => {
        const reason = event.reason;
        diagnostic("unhandled_rejection", reason && reason.message ? reason.message : String(reason || "unknown rejection"));
    });

    function ensureRuntime() {
        if (state.runtime) return Promise.resolve(state.runtime);
        if (state.runtimePromise) return state.runtimePromise;
        const optionToken = pluginToken();
        if (!optionToken) return Promise.reject(new Error("ONLYOFFICE plugin options token is missing"));
        setPluginToken(optionToken);
        state.runtimePromise = fetch("/api/documents/plugin-runtime", {
            method: "POST",
            headers: { "X-OnlyOffice-Plugin-Token": token },
        })
            .then((response) => {
                if (!response.ok) throw new Error(`plugin runtime HTTP ${response.status}`);
                return response.json();
            })
            .then((runtime) => {
                state.runtime = runtime;
                diagnostic("runtime_loaded", `host_origin=${runtime.host_origin}`);
                return runtime;
            })
            .catch((error) => {
                diagnostic("runtime_failed", error.message || String(error));
                throw error;
            });
        return state.runtimePromise;
    }

    function normalizeText(value) {
        return String(value == null ? "" : value).replace(/\r\n?/g, "\n").replace(/\u0007/g, "\t");
    }

    function cleanHtml(value) {
        const doc = new DOMParser().parseFromString(String(value || ""), "text/html");
        doc.querySelectorAll("script,style,iframe,object,embed,link,meta,base,form").forEach((node) => node.remove());
        doc.querySelectorAll("*").forEach((node) => {
            [...node.attributes].forEach((attribute) => {
                const name = attribute.name.toLowerCase();
                const val = attribute.value.trim().toLowerCase();
                if (name.startsWith("on") || name === "srcdoc" || (["href", "src"].includes(name) && val.startsWith("javascript:"))) {
                    node.removeAttribute(attribute.name);
                }
            });
        });
        return doc.body.innerHTML.slice(0, 200000);
    }

    function post(type, payload = {}) {
        const runtime = state.runtime;
        if (!runtime) return;
        window.top.postMessage({
            channel: CHANNEL,
            type,
            version: runtime.protocol_version,
            nonce: runtime.nonce,
            editor_session_id: runtime.editor_session_id,
            document: { path: runtime.path, document_key: runtime.document_key },
            ...payload,
        }, runtime.host_origin);
    }

    function ready() {
        if (!state.readyLogged) {
            state.readyLogged = true;
            diagnostic("ready_sending", `editor_type=${Asc.plugin.info?.editorType || "unknown"}`);
        }
        post("ready", {
            plugin_guid: state.runtime.plugin_guid,
            writable: state.runtime.writable,
            editor_type: Asc.plugin.info?.editorType || "unknown",
            office_api_ready: typeof Asc.plugin.executeMethod === "function",
        });
    }

    async function start() {
        if (state.started) return;
        await ensureRuntime();
        if (typeof Asc.plugin.executeMethod !== "function") {
            diagnostic("bridge_start_deferred", "executeMethod unavailable");
            return;
        }
        state.started = true;
        diagnostic("bridge_started", `editor_type=${Asc.plugin.info?.editorType || "unknown"}`);
        ready();
        state.readyTimer = setInterval(() => {
            if (state.acknowledged) clearInterval(state.readyTimer);
            else ready();
        }, 1000);
    }

    function executeMethod(name, args = []) {
        return new Promise((resolve, reject) => {
            try {
                const accepted = Asc.plugin.executeMethod(name, args, resolve);
                if (accepted === false) reject(new Error(`ONLYOFFICE rejected ${name}`));
            } catch (error) { reject(error); }
        });
    }

    function callCommand(command, recalculate = false) {
        return new Promise((resolve, reject) => {
            try { Asc.plugin.callCommand(command, false, recalculate, resolve); }
            catch (error) { reject(error); }
        });
    }

    function failure(code, message, metadata) {
        const error = new Error(message);
        error.code = code;
        error.metadata = metadata || {};
        throw error;
    }

    function validateRequest(request) {
        const runtime = state.runtime;
        if (!request || request.version !== runtime.protocol_version) failure("PROTOCOL_INVALID", "协议版本不匹配");
        if (request.editor_session_id !== runtime.editor_session_id) failure("DOCUMENT_MISMATCH", "编辑器实例已变化");
        if (request.document?.path !== runtime.path || request.document?.document_key !== runtime.document_key) {
            failure("DOCUMENT_MISMATCH", "目标文档与当前编辑器不匹配");
        }
        if (!Number.isInteger(request.deadline_ms) || Date.now() >= request.deadline_ms) failure("COMMAND_TIMEOUT", "命令已超时");
        if (!["document_read", "document_navigate", "document_edit", "document_snapshot", "document_select", "document_get_selection", "document_insert_or_replace", "document_format", "document_add_comment", "spreadsheet_read", "spreadsheet_edit", "template_fill"].includes(request.command)) {
            failure("PROTOCOL_INVALID", "命令不在白名单中");
        }
    }

    async function readSelection(format) {
        const text = normalizeText(await executeMethod("GetSelectedText", [{
            NewLineSeparator: "\n", TableCellSeparator: "\t", TableRowSeparator: "\n", ParaSeparator: "\n", TabSymbol: "\t",
        }]));
        if (format === "html") {
            return { scope: "selection", format: "html", text, html: cleanHtml(await executeMethod("GetSelectedContent", [{ type: "html" }])) };
        }
        return { scope: "selection", format: "text", text };
    }

    async function documentRead(args) {
        if (args.scope === "selection") return readSelection(args.format || "text");
        if (args.format === "html") failure("PROTOCOL_INVALID", "全文读取只支持 text 格式");
        Asc.scope.readOffset = Math.max(0, Number(args.offset) || 0);
        Asc.scope.readLimit = Math.max(1, Number(args.limit) || 100000);
        return callCommand(function () {
            const doc = Api.GetDocument();
            const parts = [];
            const count = doc.GetElementsCount();
            for (let index = 0; index < count; index += 1) {
                const element = doc.GetElement(index);
                try {
                    const text = element.GetText({ ParaSeparator: "\n", TableCellSeparator: "\t", TableRowSeparator: "\n" });
                    if (text !== undefined && text !== null) parts.push(String(text));
                } catch (_) {}
            }
            const full = parts.join("\n");
            const text = full.slice(Asc.scope.readOffset, Asc.scope.readOffset + Asc.scope.readLimit);
            const next = Asc.scope.readOffset + text.length;
            return { scope: "document", format: "text", text, offset: Asc.scope.readOffset, next_offset: next < full.length ? next : null, truncated: next < full.length, total_chars: full.length };
        });
    }

    async function documentSnapshot() {
        if (Asc.plugin.info?.editorType !== "word") failure("PROTOCOL_INVALID", "逻辑行快照只支持 DOCX");
        return callCommand(function () {
            function collectParagraphs(doc) {
                const paragraphs = [];
                const count = doc.GetElementsCount();
                for (let index = 0; index < count; index += 1) {
                    const element = doc.GetElement(index);
                    let type = "";
                    try { type = element.GetClassType(); } catch (_) {}
                    if (type === "paragraph") {
                        paragraphs.push(element);
                    } else if (type === "table") {
                        const rows = element.GetRowsCount();
                        for (let rowIndex = 0; rowIndex < rows; rowIndex += 1) {
                            const row = element.GetRow(rowIndex);
                            const cells = row.GetCellsCount();
                            for (let cellIndex = 0; cellIndex < cells; cellIndex += 1) {
                                const content = row.GetCell(cellIndex).GetContent();
                                const nested = content.GetAllParagraphs() || [];
                                for (let paragraphIndex = 0; paragraphIndex < nested.length; paragraphIndex += 1) {
                                    paragraphs.push(nested[paragraphIndex]);
                                }
                            }
                        }
                    } else if (typeof element.GetAllParagraphs === "function") {
                        const nested = element.GetAllParagraphs() || [];
                        for (let paragraphIndex = 0; paragraphIndex < nested.length; paragraphIndex += 1) {
                            paragraphs.push(nested[paragraphIndex]);
                        }
                    }
                }
                return paragraphs;
            }
            function checksum(value) {
                let hash = 2166136261;
                for (let index = 0; index < value.length; index += 1) {
                    hash ^= value.charCodeAt(index);
                    hash = Math.imul(hash, 16777619);
                }
                return `fnv1a:${(hash >>> 0).toString(16).padStart(8, "0")}`;
            }
            const doc = Api.GetDocument();
            const paragraphs = collectParagraphs(doc);
            const lines = [];
            for (let paragraphIndex = 0; paragraphIndex < paragraphs.length; paragraphIndex += 1) {
                const paragraph = paragraphs[paragraphIndex];
                let paragraphText = "";
                try {
                    paragraphText = String(paragraph.GetText({
                        NewLineSeparator: "\n", ParaSeparator: "", TabSymbol: "\t",
                    }) || "").replace(/\r\n?/g, "\n");
                } catch (_) {}
                const parts = paragraphText.split("\n");
                let localStart = 0;
                for (let partIndex = 0; partIndex < parts.length; partIndex += 1) {
                    const text = parts[partIndex];
                    let range = null, fallback = null;
                    try { fallback = paragraph.GetRange(); } catch (_) {}
                    try { range = paragraph.GetRange(localStart, localStart + text.length); } catch (_) { range = fallback; }
                    let start = null, end = null, page = null;
                    try { start = range.GetStartPos(); } catch (_) {}
                    try { end = range.GetEndPos(); } catch (_) {}
                    try { page = Number(range.GetStartPage()) + 1; } catch (_) {}
                    lines.push({ text, paragraph_text: paragraphText, start, end, page });
                    localStart += text.length + 1;
                }
            }
            if (!lines.length) lines.push({ text: "", paragraph_text: "", start: 0, end: 0, page: 1 });
            const signature = lines.map((line) => `${line.text}\u001e${line.start}:${line.end}:${line.page}`).join("\u001f");
            return { snapshot_id: checksum(signature), lines };
        });
    }

    async function documentGetSelection() {
        const selected = await readSelection("text");
        return { selected_text: selected.text };
    }

    async function selectPdf(args) {
        const query = args.anchor || args.select_text || "";
        if (!query) {
            if (!Number.isInteger(args.page)) failure("PROTOCOL_INVALID", "PDF 定位需要页码或文本");
            const moved = await executeMethod("GoToPage", [args.page - 1]);
            if (moved === false) failure("TARGET_NOT_FOUND", `页码 ${args.page} 不存在`);
            return { selected_text: "", page: args.page };
        }
        Asc.scope.pdfSelectArgs = { query, page: Number.isInteger(args.page) ? args.page : 0 };
        const result = await callCommand(function () {
            function firstPoint(quad) {
                const value = Array.isArray(quad) && Array.isArray(quad[0]) ? quad[0] : quad;
                if (Array.isArray(value)) {
                    if (value.length && typeof value[0] === "object") return { x: value[0].x, y: value[0].y };
                    return { x: value[0], y: value[1] };
                }
                return { x: value.x1 ?? value.x ?? value.X, y: value.y1 ?? value.y ?? value.Y };
            }
            function lastPoint(quad) {
                const value = Array.isArray(quad) && Array.isArray(quad[0]) ? quad[quad.length - 1] : quad;
                if (Array.isArray(value)) {
                    if (value.length && typeof value[value.length - 1] === "object") {
                        const point = value[value.length - 1]; return { x: point.x, y: point.y };
                    }
                    return { x: value[value.length - 2], y: value[value.length - 1] };
                }
                return { x: value.x3 ?? value.x2 ?? value.x ?? value.X, y: value.y3 ?? value.y2 ?? value.y ?? value.Y };
            }
            const doc = Api.GetDocument();
            const args = Asc.scope.pdfSelectArgs;
            const startPage = args.page > 0 ? args.page - 1 : 0;
            const endPage = args.page > 0 ? args.page : doc.GetPagesCount();
            const matches = [];
            for (let pageIndex = startPage; pageIndex < endPage; pageIndex += 1) {
                const page = doc.GetPage(pageIndex);
                const quads = page.Search({ text: args.query, matchCase: true, wholeWords: false }) || [];
                for (let index = 0; index < quads.length; index += 1) matches.push({ pageIndex, quad: quads[index] });
            }
            if (matches.length !== 1) return { count: matches.length };
            const match = matches[0];
            const page = doc.GetPage(match.pageIndex);
            const selected = page.SetSelection(firstPoint(match.quad), lastPoint(match.quad));
            return { count: 1, selected: selected !== false, page: match.pageIndex + 1 };
        });
        if (!result || result.count === 0) failure("TARGET_NOT_FOUND", "PDF 中未找到目标文本");
        if (result.count !== 1) failure("TARGET_NOT_UNIQUE", `PDF 目标文本匹配 ${result.count} 次`);
        if (!result.selected) failure("SELECTION_CHANGED", "PDF 文本层无法建立可靠选区");
        const selected = normalizeText(await executeMethod("GetSelectedText", []));
        if (selected && selected !== query) failure("SELECTION_CHANGED", "PDF 实际选中文本与目标不一致");
        return { selected_text: selected || query, page: result.page };
    }

    async function documentSelect(args) {
        if (Asc.plugin.info?.editorType === "pdf") return selectPdf(args);
        Asc.scope.logicalSelectArgs = args;
        const result = await callCommand(function () {
            function collectParagraphs(doc) {
                const paragraphs = [];
                const count = doc.GetElementsCount();
                for (let index = 0; index < count; index += 1) {
                    const element = doc.GetElement(index);
                    let type = "";
                    try { type = element.GetClassType(); } catch (_) {}
                    if (type === "paragraph") paragraphs.push(element);
                    else if (type === "table") {
                        for (let rowIndex = 0; rowIndex < element.GetRowsCount(); rowIndex += 1) {
                            const row = element.GetRow(rowIndex);
                            for (let cellIndex = 0; cellIndex < row.GetCellsCount(); cellIndex += 1) {
                                const nested = row.GetCell(cellIndex).GetContent().GetAllParagraphs() || [];
                                for (let paragraphIndex = 0; paragraphIndex < nested.length; paragraphIndex += 1) paragraphs.push(nested[paragraphIndex]);
                            }
                        }
                    } else if (typeof element.GetAllParagraphs === "function") {
                        const nested = element.GetAllParagraphs() || [];
                        for (let paragraphIndex = 0; paragraphIndex < nested.length; paragraphIndex += 1) paragraphs.push(nested[paragraphIndex]);
                    }
                }
                return paragraphs;
            }
            function checksum(value) {
                let hash = 2166136261;
                for (let index = 0; index < value.length; index += 1) { hash ^= value.charCodeAt(index); hash = Math.imul(hash, 16777619); }
                return `fnv1a:${(hash >>> 0).toString(16).padStart(8, "0")}`;
            }
            const doc = Api.GetDocument(), paragraphs = collectParagraphs(doc), lines = [];
            for (let paragraphIndex = 0; paragraphIndex < paragraphs.length; paragraphIndex += 1) {
                const paragraph = paragraphs[paragraphIndex];
                let paragraphText = "";
                try { paragraphText = String(paragraph.GetText({ NewLineSeparator: "\n", ParaSeparator: "", TabSymbol: "\t" }) || "").replace(/\r\n?/g, "\n"); } catch (_) {}
                const parts = paragraphText.split("\n");
                let localStart = 0;
                for (let partIndex = 0; partIndex < parts.length; partIndex += 1) {
                    const text = parts[partIndex];
                    let range = null;
                    try { range = paragraph.GetRange(localStart, localStart + text.length); } catch (_) { try { range = paragraph.GetRange(); } catch (_) {} }
                    let start = null, end = null, page = null;
                    try { start = range.GetStartPos(); } catch (_) {}
                    try { end = range.GetEndPos(); } catch (_) {}
                    try { page = Number(range.GetStartPage()) + 1; } catch (_) {}
                    lines.push({ text, start, end, page });
                    localStart += text.length + 1;
                }
            }
            if (!lines.length) lines.push({ text: "", start: 0, end: 0, page: 1 });
            const signature = lines.map((line) => `${line.text}\u001e${line.start}:${line.end}:${line.page}`).join("\u001f");
            const snapshotId = checksum(signature), args = Asc.scope.logicalSelectArgs;
            if (args.snapshot_id && args.snapshot_id !== snapshotId) return { error: "stale" };
            const line = Number.isInteger(args.line_no) ? lines[args.line_no - 1] : null;
            if (Number.isInteger(args.line_no) && !line) return { error: "line" };
            if (!args.anchor && !args.select_text) {
                if (line) {
                    const cursor = doc.GetRange(line.start, line.start);
                    cursor.Select();
                    return { selected_text: "", page: line.page };
                }
                if (Number.isInteger(args.page)) {
                    const moved = doc.GoToPage(args.page - 1);
                    return moved === false ? { error: "page" } : { selected_text: "", page: args.page };
                }
            }
            const query = args.anchor || args.select_text;
            const ranges = doc.Search(query, true) || [], matches = [];
            for (let index = 0; index < ranges.length; index += 1) {
                const range = ranges[index];
                const start = range.GetStartPos(), end = range.GetEndPos();
                let rangePage = null;
                try { rangePage = Number(range.GetStartPage()) + 1; } catch (_) {}
                let startLine = null;
                for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
                    const candidate = lines[lineIndex];
                    if (candidate.start !== null && candidate.end !== null && start >= candidate.start
                        && (start < candidate.end || (candidate.start === candidate.end && start === candidate.start))) {
                        startLine = lineIndex + 1; break;
                    }
                }
                if (Number.isInteger(args.line_no) && startLine !== args.line_no) continue;
                if (Number.isInteger(args.page) && rangePage !== args.page) continue;
                if (args.select_text && line && !(start >= line.start && end <= line.end)) continue;
                matches.push(range);
            }
            if (matches.length !== 1) return { error: matches.length ? "unique" : "not_found", count: matches.length };
            matches[0].Select();
            return { selected_text: query, page: Number(matches[0].GetStartPage()) + 1 };
        });
        if (result?.error === "stale") failure("STALE_LINE_INDEX", "文档逻辑行已变化，请重新读取或搜索");
        if (result?.error === "line") failure("LINE_OUT_OF_RANGE", "逻辑行不存在");
        if (result?.error === "page") failure("TARGET_NOT_FOUND", "页码不存在");
        if (result?.error === "not_found") failure("TARGET_NOT_FOUND", "未找到目标文本");
        if (result?.error === "unique") failure("TARGET_NOT_UNIQUE", `目标文本匹配 ${result.count} 次`);
        if (!result.selected_text) return { selected_text: "", page: result.page };
        const selected = normalizeText(await executeMethod("GetSelectedText", []));
        if (result.selected_text && selected !== normalizeText(result.selected_text)) failure("SELECTION_CHANGED", "实际选中文本与目标不一致");
        return { selected_text: selected, page: result.page };
    }

    async function insertOrReplace(args) {
        if (!state.runtime.writable || Asc.plugin.info?.editorType !== "word") failure("READ_ONLY", "当前文档只读");
        const selected = normalizeText(await executeMethod("GetSelectedText", []));
        if (!selected || selected !== normalizeText(args.expected_text)) failure("SELECTION_CHANGED", "当前选区内容已变化");
        if (args.operation === "replace") {
            await executeMethod("ReplaceTextSmart", [[String(args.text || "")], "\t", "\n"]);
        } else {
            Asc.scope.insertRelativeArgs = { text: String(args.text || ""), position: args.operation === "insert_before" ? "before" : "after" };
            const inserted = await callCommand(function () {
                const range = Api.GetDocument().GetRangeBySelect();
                return range ? range.AddText(Asc.scope.insertRelativeArgs.text, Asc.scope.insertRelativeArgs.position) : false;
            }, true);
            if (inserted === false) failure("SELECTION_CHANGED", "当前选区不能插入文本");
        }
        return { changed: true, previous_text: selected };
    }

    async function prepareSelectedRange(args) {
        if (!state.runtime.writable || Asc.plugin.info?.editorType !== "word") failure("READ_ONLY", "当前文档只读");
        const selected = normalizeText(await executeMethod("GetSelectedText", []));
        if (!selected || selected !== normalizeText(args.expected_text)) failure("SELECTION_CHANGED", "当前选区内容已变化");
        return selected;
    }

    async function formatDocumentSelection(args) {
        const selected = await prepareSelectedRange(args);
        Asc.scope.formatProperties = args.properties || {};
        const result = await callCommand(function () {
            const range = Api.GetDocument().GetRangeBySelect();
            if (!range) return { applied: false };
            const properties = Asc.scope.formatProperties, applied = [];
            if (properties.font_family !== undefined) { range.SetFontFamily(properties.font_family); applied.push("font_family"); }
            if (properties.font_size !== undefined) { range.SetFontSize(Math.round(properties.font_size * 2)); applied.push("font_size"); }
            if (properties.bold !== undefined) { range.SetBold(properties.bold); applied.push("bold"); }
            if (properties.italic !== undefined) { range.SetItalic(properties.italic); applied.push("italic"); }
            if (properties.underline !== undefined) { range.SetUnderline(properties.underline ? "single" : "none"); applied.push("underline"); }
            if (properties.strikeout !== undefined) { range.SetStrikeout(properties.strikeout); applied.push("strikeout"); }
            if (properties.font_color !== undefined) { range.SetColor(Api.HexColor(properties.font_color)); applied.push("font_color"); }
            if (properties.background_color !== undefined) { range.SetShd("clear", Api.HexColor(properties.background_color)); applied.push("background_color"); }
            const paragraphs = range.GetAllParagraphs() || [];
            if (properties.alignment !== undefined) {
                const alignment = properties.alignment === "justify" ? "both" : properties.alignment;
                for (let index = 0; index < paragraphs.length; index += 1) paragraphs[index].SetJc(alignment);
                applied.push("alignment");
            }
            if (properties.line_spacing !== undefined) {
                const spacing = properties.line_spacing;
                const rule = spacing.rule === "multiple" ? "auto" : spacing.rule === "at_least" ? "atLeast" : "exact";
                const value = spacing.rule === "multiple" ? Math.round(spacing.value * 240) : Math.round(spacing.value * 20);
                for (let index = 0; index < paragraphs.length; index += 1) paragraphs[index].SetSpacingLine(value, rule);
                applied.push("line_spacing");
            }
            return { applied: applied.length > 0, properties: applied, paragraph_count: paragraphs.length };
        }, true);
        if (!result?.applied) failure("VERIFY_FAILED", "当前选区无法应用格式");
        return { ...result, selected_text: selected };
    }

    async function addDocumentComment(args) {
        const selected = await prepareSelectedRange(args);
        Asc.scope.commentArgs = { text: args.text, author: args.author || "Agent", author_user_id: args.author_user_id || "agent" };
        const result = await callCommand(function () {
            const range = Api.GetDocument().GetRangeBySelect();
            if (!range) return { added: false };
            const args = Asc.scope.commentArgs;
            return { added: Boolean(range.AddComment(args.text, args.author, args.author_user_id)) };
        }, true);
        if (!result?.added) failure("VERIFY_FAILED", "ONLYOFFICE 未能增加批注");
        return { added: true, selected_text: selected };
    }

    async function navigate(target) {
        if (!target || typeof target !== "object") failure("PROTOCOL_INVALID", "target 无效");
        if (Number.isInteger(target.page)) {
            Asc.scope.pageIndex = target.page - 1;
            const moved = await callCommand(function () { return Api.GetDocument().GoToPage(Asc.scope.pageIndex); });
            if (moved === false) failure("TARGET_NOT_FOUND", `页码 ${target.page} 不存在`);
            if (!target.anchor) return { page: target.page };
        }
        if (typeof target.anchor !== "string" || !target.anchor) failure("PROTOCOL_INVALID", "anchor 无效");
        Asc.scope.anchor = target.anchor;
        Asc.scope.matchCase = target.match_case !== false;
        Asc.scope.occurrence = Number.isInteger(target.occurrence) ? target.occurrence : 0;
        Asc.scope.contextBefore = typeof target.context_before === "string" ? target.context_before : "";
        Asc.scope.contextAfter = typeof target.context_after === "string" ? target.context_after : "";
        const result = await callCommand(function () {
            const doc = Api.GetDocument();
            const ranges = doc.Search(Asc.scope.anchor, Asc.scope.matchCase) || [];
            const matching = [];
            for (let index = 0; index < ranges.length; index += 1) {
                const range = ranges[index];
                let before = "", after = "";
                try {
                    const start = range.GetStartPos(), end = range.GetEndPos();
                    if (Asc.scope.contextBefore) before = doc.GetRange(Math.max(0, start - Asc.scope.contextBefore.length - 64), start).GetText({ ParaSeparator: "\n" });
                    if (Asc.scope.contextAfter) after = doc.GetRange(end, end + Asc.scope.contextAfter.length + 64).GetText({ ParaSeparator: "\n" });
                } catch (_) {}
                if (Asc.scope.contextBefore && !before.endsWith(Asc.scope.contextBefore)) continue;
                if (Asc.scope.contextAfter && !after.startsWith(Asc.scope.contextAfter)) continue;
                matching.push(index);
            }
            const selectedIndex = Asc.scope.occurrence > 0 ? (matching[Asc.scope.occurrence - 1] ?? -1) : (matching.length === 1 ? matching[0] : -1);
            if (selectedIndex >= 0) ranges[selectedIndex].Select();
            return { total: ranges.length, matching: matching.length, selectedIndex };
        });
        if (!result || result.matching === 0) failure("TARGET_NOT_FOUND", "未找到文本锚点");
        if (result.selectedIndex < 0) failure("TARGET_NOT_UNIQUE", `文本锚点匹配 ${result.matching} 次`);
        return { anchor: target.anchor, occurrence: result.selectedIndex + 1, match_count: result.total };
    }

    async function documentEdit(args) {
        if (!state.runtime.writable) failure("READ_ONLY", "当前文档只读");
        const operation = args.operation;
        if (operation === "replace_match") {
            await navigate(args.target);
            const selected = normalizeText(await executeMethod("GetSelectedText", []));
            const expected = normalizeText(args.expected_text === undefined ? args.target.anchor : args.expected_text);
            if (selected !== expected) failure("STALE_SELECTION", "定位后的文本与 expected_text 不一致");
            await executeMethod("ReplaceTextSmart", [[String(args.text || "")], "\t", "\n"]);
            return { operation, replaced_text: selected, replacement_text: String(args.text || "") };
        }
        if (operation === "insert_at_cursor") {
            await executeMethod("InputText", [String(args.text || "")]);
            return { operation, inserted_length: String(args.text || "").length };
        }
        if (!["replace_selection", "delete_selection"].includes(operation) || typeof args.expected_text !== "string") {
            failure("PROTOCOL_INVALID", "选区编辑操作或 expected_text 无效");
        }
        const selected = normalizeText(await executeMethod("GetSelectedText", []));
        if (selected !== normalizeText(args.expected_text)) failure("STALE_SELECTION", "选区内容已变化");
        if (operation === "delete_selection") await executeMethod("RemoveSelectedContent", []);
        else await executeMethod("ReplaceTextSmart", [[String(args.text || "")], "\t", "\n"]);
        return { operation, previous_text: selected, replacement_text: operation === "delete_selection" ? "" : String(args.text || "") };
    }

    async function spreadsheetRead(args) {
        Asc.scope.sheetName = args.sheet_name || "";
        Asc.scope.cellRange = args.cell_range || "";
        Asc.scope.readScope = args.scope || "used_range";
        Asc.scope.valueMode = args.value_mode || "values";
        Asc.scope.maxCells = Number(args.max_cells) || 10000;
        return callCommand(function () {
            const sheet = Asc.scope.sheetName ? Api.GetSheet(Asc.scope.sheetName) : Api.GetActiveSheet();
            if (!sheet) return { error: "sheet_not_found" };
            let range;
            if (Asc.scope.readScope === "selection") range = Api.GetSelection();
            else if (Asc.scope.readScope === "range") range = sheet.GetRange(Asc.scope.cellRange);
            else range = sheet.GetUsedRange();
            const rows = range.GetRowsCount(), columns = range.GetColumnsCount();
            if (rows * columns > Asc.scope.maxCells) return { error: "too_many_cells", rows, columns };
            const result = { sheet_name: sheet.GetName(), address: range.GetAddress(true, true, "xlA1", false), rows, columns };
            if (Asc.scope.valueMode !== "formulas") result.values = range.GetValue();
            if (Asc.scope.valueMode !== "values") result.formulas = range.GetFormula();
            return result;
        }).then((result) => {
            if (result?.error === "sheet_not_found") failure("TARGET_NOT_FOUND", "工作表不存在");
            if (result?.error === "too_many_cells") failure("PROTOCOL_INVALID", "读取区域超过单元格上限", result);
            return result;
        });
    }

    function equalValue(left, right) { return JSON.stringify(left) === JSON.stringify(right); }

    async function spreadsheetEdit(args) {
        if (!state.runtime.writable) failure("READ_ONLY", "当前工作簿只读");
        Asc.scope.editArgs = args;
        const result = await callCommand(function () {
            const args = Asc.scope.editArgs, payload = args.payload || {};
            const sheet = args.sheet_name ? Api.GetSheet(args.sheet_name) : Api.GetActiveSheet();
            if (!sheet) return { error: "sheet_not_found" };
            if (args.operation === "set_range" || args.operation === "clear_range") {
                const range = sheet.GetRange(payload.range);
                const actual = range.GetValue();
                if (!Object.prototype.hasOwnProperty.call(payload, "expected_values")) return { error: "expected_required" };
                if (JSON.stringify(actual) !== JSON.stringify(payload.expected_values)) return { error: "stale", actual };
                if (args.operation === "clear_range") range.ClearContents(); else range.SetValue(payload.values);
                return { operation: args.operation, sheet_name: sheet.GetName(), range: payload.range };
            }
            if (args.operation === "update_cells") {
                const cells = payload.cells || [];
                for (const item of cells) {
                    if (!Object.prototype.hasOwnProperty.call(item, "expected_value")) return { error: "expected_required" };
                    const actual = sheet.GetRange(item.cell).GetValue();
                    if (JSON.stringify(actual) !== JSON.stringify(item.expected_value)) return { error: "stale", cell: item.cell, actual };
                }
                cells.forEach((item) => sheet.GetRange(item.cell).SetValue(item.value));
                return { operation: args.operation, sheet_name: sheet.GetName(), updated_cells: cells.length };
            }
            if (args.operation === "append_rows") {
                const rows = payload.rows || [], used = sheet.GetUsedRange();
                const startRow = used.GetRow() + used.GetRowsCount() + 1;
                const startColumn = used.GetCol() + 1;
                rows.forEach((row, rowOffset) => row.forEach((value, colOffset) => {
                    sheet.GetRangeByNumber(startRow + rowOffset - 1, startColumn + colOffset - 1).SetValue(value);
                }));
                return { operation: args.operation, sheet_name: sheet.GetName(), appended_rows: rows.length };
            }
            return { error: "operation_invalid" };
        }, true);
        if (result?.error === "stale") failure("STALE_RANGE", "单元格区域内容已变化", result);
        if (result?.error === "expected_required") failure("PROTOCOL_INVALID", "覆盖操作必须提供 expected_values/expected_value");
        if (result?.error) failure(result.error === "sheet_not_found" ? "TARGET_NOT_FOUND" : "PROTOCOL_INVALID", result.error);
        return result;
    }

    async function templateFill(args) {
        const variables = args.variables || {};
        if (Asc.plugin.info?.editorType === "word") {
            Asc.scope.templateVariables = variables;
            const result = await callCommand(function () {
                const doc = Api.GetDocument(), missing = [], counts = {};
                for (const key of Object.keys(Asc.scope.templateVariables)) {
                    const marker = `{{${key}}}`, ranges = doc.Search(marker, true) || [];
                    counts[key] = ranges.length;
                    if (!ranges.length) missing.push(key);
                }
                if (missing.length) return { error: "missing", missing };
                for (const key of Object.keys(Asc.scope.templateVariables)) {
                    const marker = `{{${key}}}`, value = Asc.scope.templateVariables[key];
                    const ranges = doc.Search(marker, true) || [];
                    for (let index = ranges.length - 1; index >= 0; index -= 1) {
                        ranges[index].Select();
                        Api.ReplaceTextSmart([String(value == null ? "" : value)], "\t", "\n");
                    }
                }
                return { replaced: counts };
            }, true);
            if (result?.error === "missing") failure("PLACEHOLDER_NOT_FOUND", "模板中未找到部分占位符", result);
            return result;
        }
        Asc.scope.templateVariables = variables;
        const result = await callCommand(function () {
            const sheets = typeof Api.GetSheets === "function" ? Api.GetSheets() : [Api.GetActiveSheet()];
            const matches = {}, operations = [];
            Object.keys(Asc.scope.templateVariables).forEach((key) => { matches[key] = 0; });
            for (const sheet of sheets) {
                const used = sheet.GetUsedRange(), rows = used.GetRowsCount(), cols = used.GetColumnsCount();
                for (let row = 0; row < rows; row += 1) for (let col = 0; col < cols; col += 1) {
                    const cell = used.GetCells(row + 1, col + 1);
                    const formula = cell.GetFormula();
                    if (formula && String(formula).startsWith("=")) continue;
                    const raw = cell.GetValue();
                    if (typeof raw !== "string") continue;
                    let next = raw, exactValue, exact = false;
                    for (const key of Object.keys(Asc.scope.templateVariables)) {
                        const marker = `{{${key}}}`;
                        if (!next.includes(marker)) continue;
                        matches[key] += 1;
                        if (next === marker) { exact = true; exactValue = Asc.scope.templateVariables[key]; }
                        else next = next.split(marker).join(String(Asc.scope.templateVariables[key] ?? ""));
                    }
                    if (exact || next !== raw) operations.push({ cell, value: exact ? exactValue : next });
                }
            }
            const missing = Object.keys(matches).filter((key) => matches[key] === 0);
            if (missing.length) return { error: "missing", missing };
            operations.forEach((item) => item.cell.SetValue(item.value));
            return { replaced: matches };
        }, true);
        if (result?.error === "missing") failure("PLACEHOLDER_NOT_FOUND", "模板中未找到部分占位符", result);
        return result;
    }

    async function run(request) {
        validateRequest(request);
        if (state.completed.has(request.request_id)) return state.completed.get(request.request_id);
        let result;
        if (request.command === "document_read") result = await documentRead(request.args);
        if (request.command === "document_navigate") result = await navigate(request.args.target);
        if (request.command === "document_edit") result = await documentEdit(request.args);
        if (request.command === "document_snapshot") result = await documentSnapshot();
        if (request.command === "document_select") result = await documentSelect(request.args);
        if (request.command === "document_get_selection") result = await documentGetSelection();
        if (request.command === "document_insert_or_replace") result = await insertOrReplace(request.args);
        if (request.command === "document_format") result = await formatDocumentSelection(request.args);
        if (request.command === "document_add_comment") result = await addDocumentComment(request.args);
        if (request.command === "spreadsheet_read") result = await spreadsheetRead(request.args);
        if (request.command === "spreadsheet_edit") result = await spreadsheetEdit(request.args);
        if (request.command === "template_fill") result = await templateFill(request.args);
        const response = { request_id: request.request_id, ok: true, result, error: null };
        state.completed.set(request.request_id, response);
        if (state.completed.size > 200) state.completed.delete(state.completed.keys().next().value);
        return response;
    }

    window.addEventListener("message", async (event) => {
        const message = event.data;
        if (!message || message.channel !== CHANNEL) return;
        const runtime = state.runtime || await ensureRuntime().catch(() => null);
        if (!runtime || event.source !== window.top || event.origin !== runtime.host_origin) return;
        if (!message || message.channel !== CHANNEL || message.nonce !== runtime.nonce) return;
        if (message.type === "ack" && message.editor_session_id === runtime.editor_session_id) {
            state.acknowledged = true;
            diagnostic("ack_received", "host accepted plugin ready");
            return;
        }
        if (message.type !== "command") return;
        diagnostic("command_received", `${message.request?.command || "unknown"} request=${String(message.request?.request_id || "").slice(0, 12)}`);
        let response;
        try {
            response = await run(message.request);
            diagnostic("command_completed", `${message.request?.command || "unknown"} request=${String(message.request?.request_id || "").slice(0, 12)}`);
        }
        catch (error) {
            diagnostic("command_failed", `${error.code || "INTERNAL_ERROR"}: ${error.message || "unknown error"}`);
            response = { request_id: message.request?.request_id || "", ok: false, result: null, error: { code: error.code || "INTERNAL_ERROR", message: error.message || "插件执行失败", metadata: error.metadata || {} } };
        }
        post("response", { response });
    });

    Asc.plugin.init = function () {
        setPluginToken(pluginToken());
        diagnostic("init_called", "Asc.plugin.init invoked");
        start().catch((error) => {
            diagnostic("bridge_start_failed", error.message || String(error));
            post("error", { message: error.message });
        });
    };

    // Document Server 9.4 CE 在缓存恢复等路径上可能遗漏 init 回调。
    // SDK 真正注入 executeMethod 后再启动，避免把脚本加载误报为插件可用。
    let sdkPollCount = 0;
    const sdkPoll = setInterval(() => {
        sdkPollCount += 1;
        if (typeof Asc.plugin.executeMethod === "function" && pluginToken()) {
            clearInterval(sdkPoll);
            start().catch((error) => post("error", { message: error.message }));
        } else if (sdkPollCount >= 100) {
            clearInterval(sdkPoll);
            diagnostic("sdk_timeout", "executeMethod unavailable after 10 seconds");
            ensureRuntime().then(() => post("error", { message: "OnlyOffice Plugin SDK 初始化超时" })).catch(() => {});
        }
    }, 100);
})();
