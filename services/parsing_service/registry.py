"""Default parser registry — convenience for callers that just want
'parse this document'."""

from __future__ import annotations

from .docx_parser import DocxParser
from .parser import ParserRegistry
from .pdf_parser import PdfParser
from .xlsx_parser import XlsxParser


def default_registry() -> ParserRegistry:
    registry = ParserRegistry()
    registry.register(PdfParser())
    registry.register(DocxParser())
    registry.register(XlsxParser())
    return registry
