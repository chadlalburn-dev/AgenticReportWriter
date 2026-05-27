"""TemplateBuilder — orchestrates the four authoring adapters.

For now wraps DocxAdapter. The other adapters (sample reports, library,
from-scratch) plug in here as they're built.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared.schemas import ReportTemplate

from services.template_service.adapters.docx_adapter import (
    DocxAdapter,
    DocxAdapterOptions,
)
from services.template_service.adapters.library_adapter import (
    LibraryAdapter,
    LibraryAdapterOptions,
)


@dataclass(frozen=True)
class TemplateBuildResult:
    template: ReportTemplate
    warnings: tuple[str, ...] = ()


class TemplateBuilder:
    """Façade exposing one method per authoring entry point.

    The shape is intentionally narrow so callers don't depend on internal
    adapter classes — the builder is the only public surface.
    """

    def from_docx(
        self,
        path: str | Path,
        *,
        template_id: str,
        title: str | None = None,
        report_type: str = "imported_from_docx",
        authored_by: str = "template-builder:docx-adapter",
    ) -> TemplateBuildResult:
        adapter = DocxAdapter(
            DocxAdapterOptions(
                template_id=template_id,
                title=title,
                report_type=report_type,
                authored_by=authored_by,
            )
        )
        template = adapter.from_file(path)
        warnings = self._sanity_check(template)
        return TemplateBuildResult(template=template, warnings=tuple(warnings))

    def from_library(
        self,
        template_id: str,
        *,
        clone_as_template_id: str | None = None,
        clone_as_title: str | None = None,
        authored_by: str | None = None,
        library_root: str | Path | None = None,
    ) -> TemplateBuildResult:
        """Load a shipped library template. If clone_as_template_id is set,
        the result is a fresh DRAFT clone the author can customize without
        touching the library copy."""
        adapter = LibraryAdapter(library_root=library_root)
        options: LibraryAdapterOptions | None = None
        if clone_as_template_id or clone_as_title or authored_by:
            options = LibraryAdapterOptions(
                clone_as_template_id=clone_as_template_id,
                clone_as_title=clone_as_title,
                new_authored_by=authored_by,
            )
        template = adapter.load(template_id, options=options)
        warnings = self._sanity_check(template)
        return TemplateBuildResult(template=template, warnings=tuple(warnings))

    @staticmethod
    def _sanity_check(template: ReportTemplate) -> list[str]:
        """Cheap heuristics — surface things a human reviewer should look at."""
        warnings: list[str] = []
        n_sections = len(template.all_sections())
        if n_sections < 3:
            warnings.append(
                f"only {n_sections} section(s) detected — verify the source "
                "document uses Word Heading styles, not bold formatting"
            )
        elif n_sections > 200:
            warnings.append(
                f"{n_sections} sections detected — the source may have stray "
                "headings or use Heading styles for non-section text"
            )
        # Sections with empty prompts after the heuristic indicate the source
        # had no content under that heading — the human may want to add hints.
        thin_sections = [
            s.section_id
            for s in template.all_sections()
            if not s.generation.prompt_template
            or "content hint" not in (s.generation.prompt_template or "").lower()
        ]
        if len(thin_sections) > 0:
            warnings.append(
                f"{len(thin_sections)} section(s) have no content hint from the "
                "source — the human author will need to flesh out prompts. "
                f"First few: {thin_sections[:5]}"
            )
        return warnings
