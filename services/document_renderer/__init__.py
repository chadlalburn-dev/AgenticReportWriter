"""Document renderer.

Translates a ReportInstance + Citations into:
  - A Google Doc (working copy for collaborative review)
  - .docx export (via Drive API)
  - .pdf export (via Drive API)

Two renderer modes:
  - GoogleDocsRenderer: live calls to the Docs/Drive APIs using ADC
    (gcloud auth application-default login, or workload identity in Cloud
    Run). No API keys, no service-account JSON files.
  - DryRunRenderer: emits the batchUpdate spec as JSON without making any
    network calls. Useful for tests and for inspecting what would happen
    before actually creating a Doc.

Both renderers share the same `RenderSpec` intermediate representation —
the spec is produced once from the generated content + citations, then
applied to either backend.
"""

from services.document_renderer.dryrun import DryRunRenderer, RenderArtifact
from services.document_renderer.renderer import (
    DocumentRenderer,
    RenderSpec,
    spec_from_report,
)

# GoogleDocsRenderer is exported only if the Google API client is importable
# at module-load time; otherwise the user can still build a RenderSpec and
# use DryRunRenderer without any cloud dependency.
try:
    from services.document_renderer.gdocs import GoogleDocsConfig, GoogleDocsRenderer
except ImportError:  # pragma: no cover - depends on local install
    GoogleDocsConfig = None  # type: ignore[assignment,misc]
    GoogleDocsRenderer = None  # type: ignore[assignment,misc]


__all__ = [
    "DocumentRenderer",
    "DryRunRenderer",
    "GoogleDocsConfig",
    "GoogleDocsRenderer",
    "RenderArtifact",
    "RenderSpec",
    "spec_from_report",
]
