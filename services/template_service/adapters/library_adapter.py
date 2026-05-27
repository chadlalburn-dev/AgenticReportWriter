"""LibraryAdapter — pick a shipped industry-standard template by id.

Second of four template-authoring entry points. The library lives at
`templates/library/*.json` (relative to the repo root) and ships with a
small set of standards:

  - ich_e6_ib       — ICH E6 Investigator's Brochure
  - ich_e3_csr      — ICH E3 Clinical Study Report
  - consort_rct     — CONSORT 2010 RCT report

Authors pick a template by id, optionally clone it under a new id to
customize (the LibraryAdapter only loads + validates; subsequent
edits go through the TemplateBuilder façade).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shared.schemas import ReportTemplate
from shared.schemas.template import TemplateStatus


def _default_library_root() -> Path:
    """Locate templates/library/ relative to the repo root.

    Repo layout: <root>/services/template_service/adapters/library_adapter.py
    so the library is at <root>/templates/library/. Walk up four parents.
    """
    return Path(__file__).resolve().parents[3] / "templates" / "library"


@dataclass(frozen=True)
class LibraryAdapterOptions:
    """Cloning options when loading a template from the library.

    `clone_as_template_id` lets the caller load `ich_e6_ib` and emit a
    template with a new id (e.g., `gsk_ich_e6_ib_v2026`) so the original
    stays untouched.
    """

    clone_as_template_id: str | None = None
    clone_as_title: str | None = None
    new_authored_by: str | None = None


class LibraryNotFound(KeyError):
    """Raised when a template_id isn't present in the library."""


class LibraryAdapter:
    def __init__(self, library_root: str | Path | None = None) -> None:
        self._library_root = Path(library_root or _default_library_root())

    def list_ids(self) -> list[str]:
        if not self._library_root.exists():
            return []
        return sorted(p.stem for p in self._library_root.glob("*.json"))

    def load(
        self,
        template_id: str,
        options: LibraryAdapterOptions | None = None,
    ) -> ReportTemplate:
        path = self._library_root / f"{template_id}.json"
        if not path.exists():
            available = self.list_ids()
            raise LibraryNotFound(
                f"no library template with id={template_id!r}; "
                f"available: {available!r}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        template = ReportTemplate.model_validate(payload)

        if options is None:
            return template

        # Apply clone-time overrides — produces a fresh draft so the caller
        # can edit without mutating the library copy.
        updates: dict[str, object] = {}
        if options.clone_as_template_id:
            updates["template_id"] = options.clone_as_template_id
        if options.clone_as_title:
            updates["title"] = options.clone_as_title
        # Reset to DRAFT on clone — the clone hasn't been re-approved.
        updates["status"] = TemplateStatus.DRAFT

        # Metadata gets a fresh authored_at + source_origin breadcrumb
        # pointing back to where the clone came from.
        meta = template.metadata.model_copy(
            update={
                "authored_by": options.new_authored_by or template.metadata.authored_by,
                "authored_at": datetime.now(timezone.utc),
                "parent_template_id": template.template_id,
                "source_origin": "from_library",
            }
        )
        updates["metadata"] = meta

        return template.model_copy(update=updates)
