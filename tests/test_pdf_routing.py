import asyncio
import sys
from types import SimpleNamespace

import pytest

from myagent.tools.builtin.file_diff import file_diff
from myagent.tools.builtin.file_read import file_read
from myagent.tools.builtin.pdf_router import parse_pdf_with_routing


def run_tool(coro):
    return asyncio.run(coro)


def _fake_pdf_inspector(monkeypatch, *, pdf_type, confidence, pages):
    module = SimpleNamespace()
    module.detect_pdf = lambda path: SimpleNamespace(
        pdf_type=pdf_type,
        confidence=confidence,
        page_count=len(pages),
        pages_needing_ocr=[],
        has_encoding_issues=False,
        processing_time_ms=1,
    )

    def extract_pages_markdown(path, pages=None):
        selected = list(range(len(pages_data))) if pages is None else pages
        return SimpleNamespace(pages=[pages_data[index] for index in selected])

    pages_data = [
        SimpleNamespace(
            page=index,
            markdown=markdown,
            needs_ocr=needs_ocr,
            ocr_reason="page_needs_ocr" if needs_ocr else None,
        )
        for index, (markdown, needs_ocr) in enumerate(pages)
    ]
    module.extract_pages_markdown = extract_pages_markdown
    monkeypatch.setitem(sys.modules, "pdf_inspector", module)


def test_mixed_pdf_at_threshold_uses_hybrid_route(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"fake")
    monkeypatch.setenv("MYAGENT_PDF_TEXT_CONFIDENCE_THRESHOLD", "0.85")
    _fake_pdf_inspector(
        monkeypatch,
        pdf_type="mixed",
        confidence=0.85,
        pages=[("native text", False), ("", True)],
    )

    result = parse_pdf_with_routing(path)

    assert result.route == "hybrid"
    assert result.fallback_pages == [2]
    assert result.threshold == 0.85


def test_low_confidence_mixed_pdf_uses_image_route(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"fake")
    monkeypatch.setenv("MYAGENT_PDF_TEXT_CONFIDENCE_THRESHOLD", "0.85")
    _fake_pdf_inspector(
        monkeypatch,
        pdf_type="mixed",
        confidence=0.849,
        pages=[("native text", False), ("", True)],
    )

    result = parse_pdf_with_routing(path)

    assert result.route == "image"
    assert result.fallback_pages == [1, 2]
    assert result.reason == "mixed_pdf_confidence_below_threshold"


def test_short_native_text_pdf_is_not_rejected_only_for_low_confidence(
    tmp_path, monkeypatch
):
    path = tmp_path / "short.pdf"
    path.write_bytes(b"fake")
    _fake_pdf_inspector(
        monkeypatch,
        pdf_type="text_based",
        confidence=0.5,
        pages=[("one native text line", False)],
    )

    result = parse_pdf_with_routing(path)

    assert result.route == "text"
    assert result.pages[0].markdown == "one native text line"


def test_native_text_route_falls_back_when_selected_pages_are_empty(
    tmp_path, monkeypatch
):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"fake")
    _fake_pdf_inspector(
        monkeypatch,
        pdf_type="text_based",
        confidence=1.0,
        pages=[("", True)],
    )

    result = parse_pdf_with_routing(path)

    assert result.route == "image"
    assert result.fallback_pages == [1]
    assert result.reason == "selected_pages_have_no_extractable_text"


def _make_text_pdf(path, text):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_image_pdf(path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, (0, 0, 200, 200), False)
    pix.clear_with(255)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()


def test_file_read_uses_pdf_inspector_for_native_text_pdf(tmp_path):
    path = tmp_path / "text.pdf"
    _make_text_pdf(path, "Native PDF route answer")

    result = run_tool(file_read(str(path)))

    assert not result.is_error, result.content
    assert result.metadata["pdf_parser"] == "pdf-inspector"
    assert result.metadata["pdf_route"] == "text"
    assert "Native PDF route answer" in result.content


def test_file_diff_uses_pdf_inspector_for_text_pdfs(tmp_path):
    path_a = tmp_path / "a.pdf"
    path_b = tmp_path / "b.pdf"
    _make_text_pdf(path_a, "Version A text")
    _make_text_pdf(path_b, "Version B text")

    result = run_tool(file_diff(str(path_a), str(path_b)))

    assert not result.is_error, result.content
    assert result.metadata["pdf_a"]["pdf_parser"] == "pdf-inspector"
    assert result.metadata["pdf_b"]["pdf_parser"] == "pdf-inspector"
    assert result.metadata["hunk_count"] > 0


def test_file_diff_image_pdf_returns_visual_fallback(tmp_path):
    path_a = tmp_path / "a.pdf"
    path_b = tmp_path / "b.pdf"
    _make_image_pdf(path_a)
    _make_image_pdf(path_b)

    result = run_tool(file_diff(str(path_a), str(path_b)))

    assert not result.is_error, result.content
    assert result.metadata["mode"] == "visual_fallback"
    assert result.content_blocks
    assert {block["source"] for block in result.content_blocks} == {"A", "B"}
