/**
 * markdown.js — Markdown + KaTeX 渲染模块
 * 依赖外部库：marked, katex（通过 CDN 全局加载）
 * 不依赖其他内部模块
 */

import { escapeHtml } from './utils.js';

// ── 配置 marked ──
export function initMarkdown() {
    if (typeof marked !== "undefined") {
        marked.setOptions({
            breaks: true,
            gfm: true,
        });
    }
}

/**
 * 从文本中提取数学公式，替换为占位符
 * 处理 $$...$$（块级）和 $...$（行内），支持多行
 */
export function extractMath(text) {
    const mathBlocks = [];
    // 先匹配 $$...$$（块级数学公式，贪婪匹配多行）
    text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (match, formula) {
        mathBlocks.push({ display: true, formula: formula.trim() });
        return "%%MATH_" + (mathBlocks.length - 1) + "%%";
    });
    // 匹配 $...$（行内数学公式）
    text = text.replace(/(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\$)\$(?!\$)/g, function (match, formula) {
        mathBlocks.push({ display: false, formula: formula.trim() });
        return "%%MATH_" + (mathBlocks.length - 1) + "%%";
    });
    return { text: text, mathBlocks: mathBlocks };
}

/**
 * 将数学占位符渲染回 KaTeX HTML
 */
export function renderMathPlaceholders(html, mathBlocks) {
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
            var fallback = block.display
                ? '<pre class="katex-fallback">' + escapeHtml(block.formula) + '</pre>'
                : '<code class="katex-fallback">' + escapeHtml(block.formula) + '</code>';
            html = html.replace(placeholder, fallback);
        }
    });
    return html;
}

/**
 * 完整 Markdown + KaTeX 渲染（消息完成时使用）
 */
export function renderMarkdown(text) {
    if (!text) return "";
    if (typeof marked === "undefined" || typeof katex === "undefined") {
        return renderMarkdownSimple(text);
    }
    var extracted = extractMath(text);
    var mdHtml = marked.parse(extracted.text);
    var finalHtml = renderMathPlaceholders(mdHtml, extracted.mathBlocks);
    return finalHtml;
}

/**
 * 流式 Markdown 渲染（不使用 KaTeX，提升性能）
 * 数学公式在流式过程中显示为原始 LaTeX
 */
export function renderMarkdownStream(text) {
    if (!text) return "";
    if (typeof marked === "undefined") {
        return renderMarkdownSimple(text);
    }
    return marked.parse(text);
}

/**
 * 简易渲染器 — 外部库未加载时的降级方案
 */
export function renderMarkdownSimple(text) {
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