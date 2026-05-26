"""Shared Pydantic schemas used across all services.

These are the integration contracts between ingestion, parsing, template,
generation, citation, and rendering services. Changes here are breaking
changes — bump the schema version and migrate consumers.
"""

from shared.schemas.canonical_document import CanonicalDocument, SourceSystem
from shared.schemas.citation import Citation, CitationLocator, SourceType
from shared.schemas.template import (
    DataBinding,
    DataBindingType,
    GenerationMode,
    ReportTemplate,
    TemplateSection,
    TemplateStatus,
    ValidationRule,
)

__all__ = [
    "CanonicalDocument",
    "Citation",
    "CitationLocator",
    "DataBinding",
    "DataBindingType",
    "GenerationMode",
    "ReportTemplate",
    "SourceSystem",
    "SourceType",
    "TemplateSection",
    "TemplateStatus",
    "ValidationRule",
]
