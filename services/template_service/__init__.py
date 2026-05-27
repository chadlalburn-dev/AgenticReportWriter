"""Template authoring service.

Four entry adapters converge on a single Template Builder workflow that
produces a draft ReportTemplate (status=DRAFT) for human review. Once
the author approves, the template moves to status=APPROVED and is
locked under the change-control process described in the architecture
plan (Validated mode).

Currently implemented:
- DocxAdapter: parses a .docx with heading hierarchy + content into a
  section tree, with draft prompt_template + tag suggestions per section.

Not yet implemented (architecture plan calls for these):
- SampleReportsAdapter: derive a template by LLM analysis of historical
  reports.
- LibraryAdapter: pick a shipped industry-standard template
  (ICH E6 IB, ICH E3 CSR, CONSORT) and customize.
- FromScratchAdapter: agent asks scoping questions and proposes a
  skeleton.
"""

from services.template_service.adapters.docx_adapter import (
    DocxAdapter,
    DocxAdapterOptions,
)
from services.template_service.adapters.from_scratch_adapter import (
    FromScratchAdapter,
    FromScratchAdapterOptions,
    ScopingSpec,
)
from services.template_service.adapters.library_adapter import (
    LibraryAdapter,
    LibraryAdapterOptions,
    LibraryNotFound,
)
from services.template_service.adapters.sample_reports_adapter import (
    SampleReportsAdapter,
    SampleReportsAdapterOptions,
)
from services.template_service.builder import (
    TemplateBuildResult,
    TemplateBuilder,
)

__all__ = [
    "DocxAdapter",
    "DocxAdapterOptions",
    "FromScratchAdapter",
    "FromScratchAdapterOptions",
    "LibraryAdapter",
    "LibraryAdapterOptions",
    "LibraryNotFound",
    "SampleReportsAdapter",
    "SampleReportsAdapterOptions",
    "ScopingSpec",
    "TemplateBuildResult",
    "TemplateBuilder",
]
