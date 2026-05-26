"""GoogleDocsRenderer — live Docs API renderer using Application Default Credentials.

Auth setup: run `gcloud auth application-default login` once on the local
machine and grant Drive + Docs scopes. In Cloud Run, workload identity
handles auth automatically — no keys, no JSON files.

Scopes required:
  - https://www.googleapis.com/auth/documents
  - https://www.googleapis.com/auth/drive.file  (so we can also export)

This renderer is intentionally simple for the PoC:
  - Documents created via Docs API
  - Content inserted as one big batchUpdate per section group
  - Headings styled by level
  - Footnote anchors rendered inline as bracketed superscript-style markers
    `[1]`, `[2]`, ... with a Citations Appendix at the end of the doc
    (full native footnotes are a follow-up — they need the
    CreateFootnote request and have indexing quirks worth handling
    carefully later)

Tables are not yet supported by this renderer — placeholder text is
emitted in their place, so the layout stays valid. Tables are a follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.document_renderer.dryrun import RenderArtifact, _citation_to_dict
from services.document_renderer.renderer import (
    InsertCitationsAppendix,
    InsertHeading,
    InsertPageBreak,
    InsertParagraph,
    InsertTable,
    RenderSpec,
)

# Docs API uses these style names for headings.
_HEADING_STYLES = {
    1: "HEADING_1",
    2: "HEADING_2",
    3: "HEADING_3",
    4: "HEADING_4",
    5: "HEADING_5",
    6: "HEADING_6",
}

_DOCS_SCOPES = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
)


@dataclass(frozen=True)
class GoogleDocsConfig:
    """Optional knobs for the renderer.

    target_drive_folder_id: if set, the new Doc is moved to this folder
    after creation (requires the drive.file scope and ADC user to have
    access to the folder).
    """

    target_drive_folder_id: str | None = None


class GoogleDocsRenderer:
    def __init__(self, config: GoogleDocsConfig | None = None) -> None:
        try:
            from google.auth import default  # type: ignore[import-not-found]
            from googleapiclient.discovery import build  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "google-api-python-client / google-auth not installed. "
                "Run `pip install google-api-python-client google-auth` in the project venv."
            ) from exc

        try:
            credentials, _project = default(scopes=list(_DOCS_SCOPES))
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "Application Default Credentials are not configured. "
                "Run `gcloud auth application-default login` (and grant the "
                "Docs + Drive scopes) before using GoogleDocsRenderer."
            ) from exc

        self._docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
        self._drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._config = config or GoogleDocsConfig()

    def render(self, spec: RenderSpec) -> RenderArtifact:
        document = self._docs.documents().create(body={"title": spec.title}).execute()
        document_id = document["documentId"]

        # Build the document content + indexed style/footnote requests in a
        # single forward pass.
        requests: list[dict[str, Any]] = []
        # The Docs API auto-creates the first paragraph at index 1; we insert
        # all our text starting there.
        cursor = 1
        # Renumber citations globally for the appendix.
        citation_number_by_id: dict[str, int] = {}

        # We need the citations appendix's content rendered LAST but the
        # numbering established as we walk anchors. So: do a first pass to
        # number citation_ids in encounter order, then a second pass to emit
        # the requests.
        for op in spec.operations:
            if isinstance(op, InsertParagraph):
                for _, cid in op.footnote_anchors:
                    if cid not in citation_number_by_id:
                        citation_number_by_id[cid] = len(citation_number_by_id) + 1
            elif isinstance(op, InsertTable) and op.citation_id:
                if op.citation_id not in citation_number_by_id:
                    citation_number_by_id[op.citation_id] = len(citation_number_by_id) + 1

        for op in spec.operations:
            cursor, ops_for_op = self._operation_to_requests(
                op, cursor, citation_number_by_id
            )
            requests.extend(ops_for_op)

        if requests:
            self._docs.documents().batchUpdate(
                documentId=document_id, body={"requests": requests}
            ).execute()

        # Optional: move to a target folder.
        if self._config.target_drive_folder_id:
            self._drive.files().update(
                fileId=document_id,
                addParents=self._config.target_drive_folder_id,
                fields="id, parents",
            ).execute()

        url = f"https://docs.google.com/document/d/{document_id}/edit"
        n_ops = len(requests)
        return RenderArtifact(
            backend="google-docs",
            document_id=document_id,
            document_url=url,
            n_operations=n_ops,
        )

    def export_docx(self, document_id: str, dest_path: str) -> str:
        """Export a Doc to .docx via the Drive API."""
        from io import FileIO
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-not-found]

        request = self._drive.files().export_media(
            fileId=document_id,
            mimeType=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
        with FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return dest_path

    def export_pdf(self, document_id: str, dest_path: str) -> str:
        from io import FileIO
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-not-found]

        request = self._drive.files().export_media(
            fileId=document_id, mimeType="application/pdf"
        )
        with FileIO(dest_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return dest_path

    # -- per-operation translation ------------------------------------------

    def _operation_to_requests(
        self, op: object, cursor: int, citation_numbers: dict[str, int]
    ) -> tuple[int, list[dict[str, Any]]]:
        requests: list[dict[str, Any]] = []
        if isinstance(op, InsertHeading):
            text = op.text + "\n"
            requests.append(
                {
                    "insertText": {"location": {"index": cursor}, "text": text},
                }
            )
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": cursor,
                            "endIndex": cursor + len(text),
                        },
                        "paragraphStyle": {
                            "namedStyleType": _HEADING_STYLES.get(op.level, "HEADING_6")
                        },
                        "fields": "namedStyleType",
                    }
                }
            )
            return cursor + len(text), requests

        if isinstance(op, InsertParagraph):
            # Splice [N] markers into the text at the anchor positions, from
            # the end backwards so earlier positions don't shift.
            text = op.text
            for idx, cid in sorted(op.footnote_anchors, key=lambda a: a[0], reverse=True):
                num = citation_numbers.get(cid)
                if num is None:
                    continue
                marker = f" [{num}]"
                text = text[:idx] + marker + text[idx:]
            text += "\n"
            requests.append(
                {"insertText": {"location": {"index": cursor}, "text": text}}
            )
            return cursor + len(text), requests

        if isinstance(op, InsertTable):
            # Placeholder until full table support is added.
            placeholder = (
                f"[Table: {op.caption} — content rendered in a follow-up "
                f"({len(op.rows)} rows × {len(op.columns)} columns)]\n"
            )
            requests.append(
                {"insertText": {"location": {"index": cursor}, "text": placeholder}}
            )
            return cursor + len(placeholder), requests

        if isinstance(op, InsertPageBreak):
            requests.append(
                {"insertPageBreak": {"location": {"index": cursor}}}
            )
            return cursor + 1, requests

        if isinstance(op, InsertCitationsAppendix):
            header = "References\n"
            requests.append(
                {"insertText": {"location": {"index": cursor}, "text": header}}
            )
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": cursor,
                            "endIndex": cursor + len(header),
                        },
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "fields": "namedStyleType",
                    }
                }
            )
            cursor += len(header)
            for citation in op.citations:
                num = citation_numbers.get(citation.citation_id)
                if num is None:
                    continue
                d = _citation_to_dict(citation)
                line = (
                    f"[{num}] {d['source_doc_id']} — {d['source_type']} — "
                    f"{d['locator']} — \"{d['snippet']}\"\n"
                )
                requests.append(
                    {"insertText": {"location": {"index": cursor}, "text": line}}
                )
                cursor += len(line)
            return cursor, requests

        raise TypeError(f"unsupported render op: {type(op)!r}")
