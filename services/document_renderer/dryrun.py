"""DryRunRenderer — emits the RenderSpec as a JSON file with no network calls.

Two uses:
1. Tests assert on the structure of operations without standing up Docs API
2. Local dev: inspect what would land in the Doc before authenticating
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from services.document_renderer.renderer import (
    InsertCitationsAppendix,
    InsertHeading,
    InsertPageBreak,
    InsertParagraph,
    InsertTable,
    RenderSpec,
)
from shared.schemas import Citation


@dataclass(frozen=True)
class RenderArtifact:
    """What a renderer returns to the caller."""

    backend: str
    json_path: Path | None = None
    document_id: str | None = None
    document_url: str | None = None
    n_operations: int = 0


class DryRunRenderer:
    def __init__(self, output_path: Path) -> None:
        self._output_path = Path(output_path)

    def render(self, spec: RenderSpec) -> RenderArtifact:
        operations = [self._op_to_dict(op) for op in spec.operations]
        payload = {"title": spec.title, "operations": operations}
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        return RenderArtifact(
            backend="dry-run",
            json_path=self._output_path,
            n_operations=len(operations),
        )

    @staticmethod
    def _op_to_dict(op: object) -> dict[str, object]:
        # Citations need explicit handling — pydantic models, not dataclasses.
        if isinstance(op, InsertCitationsAppendix):
            return {
                "kind": "insert_citations_appendix",
                "citations": [
                    _citation_to_dict(c) for c in op.citations
                ],
            }
        if isinstance(op, InsertHeading):
            return {"kind": "insert_heading", "text": op.text, "level": op.level}
        if isinstance(op, InsertParagraph):
            return {
                "kind": "insert_paragraph",
                "text": op.text,
                "footnote_anchors": [
                    {"index": idx, "citation_id": cid} for idx, cid in op.footnote_anchors
                ],
            }
        if isinstance(op, InsertTable):
            return {
                "kind": "insert_table",
                "caption": op.caption,
                "columns": list(op.columns),
                "rows": [list(r) for r in op.rows],
                "citation_id": op.citation_id,
            }
        if isinstance(op, InsertPageBreak):
            return {"kind": "insert_page_break"}
        if is_dataclass(op):
            return {"kind": type(op).__name__.lower(), **asdict(op)}
        raise TypeError(f"unknown render op: {type(op)!r}")


def _citation_to_dict(citation: Citation) -> dict[str, object]:
    """Compact summary for the appendix — full record is in the audit log."""
    return {
        "citation_id": citation.citation_id,
        "source_type": citation.source_type.value,
        "source_doc_id": citation.source_doc_id,
        "source_uri": citation.source_uri,
        "snippet": citation.snippet[:200],
        "locator": {
            k: v
            for k, v in citation.locator.model_dump(exclude_none=True).items()
        },
    }
