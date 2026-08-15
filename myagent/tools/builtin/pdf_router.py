"""Shared PDF inspection, text extraction, and image-fallback routing.

All native-text PDF consumers use pdf-inspector.  PyMuPDF remains the image
renderer and is invoked by callers when this module selects an image route.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_PDF_TEXT_CONFIDENCE_THRESHOLD = 0.85
_PDF_TEXT_TYPES = {"text_based"}
_PDF_IMAGE_TYPES = {"scanned", "image_based"}


def get_pdf_text_confidence_threshold() -> float:
    """Return the configurable threshold used to trust native text extraction."""
    raw = os.environ.get(
        "MYAGENT_PDF_TEXT_CONFIDENCE_THRESHOLD",
        str(_PDF_TEXT_CONFIDENCE_THRESHOLD),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _PDF_TEXT_CONFIDENCE_THRESHOLD
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class PdfPageText:
    page_number: int
    markdown: str
    needs_image: bool = False
    fallback_reason: str | None = None


@dataclass
class PdfRoutingResult:
    route: str
    pdf_type: str
    confidence: float
    threshold: float
    page_count: int
    selected_pages: list[int]
    pages: list[PdfPageText] = field(default_factory=list)
    fallback_pages: list[int] = field(default_factory=list)
    has_encoding_issues: bool = False
    reason: str = ""
    processing_time_ms: int | None = None

    @property
    def uses_text(self) -> bool:
        return self.route in {"text", "hybrid"}

    @property
    def needs_images(self) -> bool:
        return self.route in {"image", "hybrid"}

    def metadata(self) -> dict[str, Any]:
        return {
            "pdf_parser": "pdf-inspector",
            "pdf_route": self.route,
            "pdf_type": self.pdf_type,
            "pdf_confidence": self.confidence,
            "pdf_confidence_threshold": self.threshold,
            "pdf_has_encoding_issues": self.has_encoding_issues,
            "pdf_route_reason": self.reason,
            "page_count": self.page_count,
            "selected_pages": list(self.selected_pages),
            "fallback_pages": list(self.fallback_pages),
            "pdf_inspector_processing_time_ms": self.processing_time_ms,
        }


def parse_pdf_with_routing(
    path: Path,
    start_page: int | None = None,
    end_page: int | None = None,
) -> PdfRoutingResult:
    """Inspect a PDF and extract per-page Markdown when native text is trusted.

    Routing rules:
    - text_based + valid encoding -> text, then verify extraction is non-empty
    - mixed + confidence >= threshold -> hybrid (only OCR-marked pages use images)
    - scanned/image_based, low-confidence mixed/unknown, or encoding issues -> image
    - parser/import failures -> image so callers can still render with PyMuPDF
    """
    threshold = get_pdf_text_confidence_threshold()
    try:
        import pdf_inspector
    except ImportError as exc:
        return PdfRoutingResult(
            route="image",
            pdf_type="unknown",
            confidence=0.0,
            threshold=threshold,
            page_count=0,
            selected_pages=[],
            reason=f"pdf_inspector_unavailable: {exc}",
        )

    try:
        detected = pdf_inspector.detect_pdf(str(path))
    except Exception as exc:
        return PdfRoutingResult(
            route="image",
            pdf_type="unknown",
            confidence=0.0,
            threshold=threshold,
            page_count=0,
            selected_pages=[],
            reason=f"pdf_inspector_detection_failed: {type(exc).__name__}: {exc}",
        )

    page_count = max(0, int(getattr(detected, "page_count", 0) or 0))
    selected_pages = _select_pages(page_count, start_page, end_page)
    pdf_type = str(getattr(detected, "pdf_type", "unknown") or "unknown").lower()
    confidence = float(getattr(detected, "confidence", 0.0) or 0.0)
    encoding_issues = bool(getattr(detected, "has_encoding_issues", False))
    processing_time_ms = getattr(detected, "processing_time_ms", None)

    route, reason = _decide_route(pdf_type, confidence, threshold, encoding_issues)
    if route == "image":
        return PdfRoutingResult(
            route=route,
            pdf_type=pdf_type,
            confidence=confidence,
            threshold=threshold,
            page_count=page_count,
            selected_pages=selected_pages,
            fallback_pages=list(selected_pages),
            has_encoding_issues=encoding_issues,
            reason=reason,
            processing_time_ms=processing_time_ms,
        )

    try:
        page_indexes = [page - 1 for page in selected_pages]
        extracted = pdf_inspector.extract_pages_markdown(
            str(path), pages=page_indexes
        )
    except Exception as exc:
        return PdfRoutingResult(
            route="image",
            pdf_type=pdf_type,
            confidence=confidence,
            threshold=threshold,
            page_count=page_count,
            selected_pages=selected_pages,
            fallback_pages=list(selected_pages),
            has_encoding_issues=encoding_issues,
            reason=f"pdf_inspector_extraction_failed: {type(exc).__name__}: {exc}",
            processing_time_ms=processing_time_ms,
        )

    page_map = {
        int(page.page) + 1: page
        for page in getattr(extracted, "pages", [])
    }
    pages: list[PdfPageText] = []
    fallback_pages: list[int] = []
    has_any_text = False

    for page_number in selected_pages:
        page = page_map.get(page_number)
        markdown = str(getattr(page, "markdown", "") or "").strip()
        has_any_text = has_any_text or bool(markdown)
        page_needs_ocr = bool(getattr(page, "needs_ocr", False)) if page else True
        fallback_reason = getattr(page, "ocr_reason", None) if page else "missing_page_result"

        # A confident text_based document can contain intentionally blank or
        # watermark-only pages. Trust document classification unless the whole
        # selected range has no extractable text. Mixed documents route the
        # per-page OCR flags to image rendering.
        needs_image = route == "hybrid" and (page_needs_ocr or not markdown)
        if needs_image:
            fallback_pages.append(page_number)
        pages.append(
            PdfPageText(
                page_number=page_number,
                markdown=markdown,
                needs_image=needs_image,
                fallback_reason=str(fallback_reason) if fallback_reason else None,
            )
        )

    if selected_pages and not has_any_text:
        route = "image"
        reason = "selected_pages_have_no_extractable_text"
        fallback_pages = list(selected_pages)
        pages = [
            PdfPageText(
                page_number=page.page_number,
                markdown=page.markdown,
                needs_image=True,
                fallback_reason=page.fallback_reason or reason,
            )
            for page in pages
        ]
    elif route == "hybrid" and not fallback_pages:
        route = "text"
        reason = "mixed_document_selected_pages_are_text_extractable"

    return PdfRoutingResult(
        route=route,
        pdf_type=pdf_type,
        confidence=confidence,
        threshold=threshold,
        page_count=page_count,
        selected_pages=selected_pages,
        pages=pages,
        fallback_pages=fallback_pages,
        has_encoding_issues=encoding_issues,
        reason=reason,
        processing_time_ms=processing_time_ms,
    )


def _select_pages(
    page_count: int,
    start_page: int | None,
    end_page: int | None,
) -> list[int]:
    if page_count <= 0:
        return []
    start = max(1, int(start_page or 1))
    end = min(int(end_page or page_count), page_count)
    if start > page_count:
        raise ValueError(f"PDF 共 {page_count} 页，start_line={start} 超出范围。")
    if end < start:
        raise ValueError("end_line 不能小于 start_line。")
    return list(range(start, end + 1))


def _decide_route(
    pdf_type: str,
    confidence: float,
    threshold: float,
    has_encoding_issues: bool,
) -> tuple[str, str]:
    if has_encoding_issues:
        return "image", "pdf_inspector_detected_encoding_issues"
    if pdf_type in _PDF_IMAGE_TYPES:
        return "image", f"pdf_type_is_{pdf_type}"
    if pdf_type in _PDF_TEXT_TYPES:
        return "text", "native_text_pdf"
    if pdf_type == "mixed":
        if confidence < threshold:
            return "image", "mixed_pdf_confidence_below_threshold"
        return "hybrid", "high_confidence_mixed_pdf"
    if confidence < threshold:
        return "image", "pdf_confidence_below_threshold"
    return "image", f"unsupported_pdf_type_{pdf_type}"
