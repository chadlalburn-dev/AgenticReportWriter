"""Binding resolution + chunk retrieval.

Given a TemplateSection's data_bindings and the available pool of
CanonicalDocuments + ParsedChunks, this module produces a
`ResolvedSectionContext`: the inputs the section filler needs to prompt
the LLM.

PoC behaviour:
- file_set: tag-based filtering of the chunk pool
- file_ref: chunks whose source_doc_id matches
- free_text_input: pulled from the ReportInstance.free_text_inputs map
- named_query / sql_query / api_call: not executed in the PoC — placeholder
  text noting "data binding deferred" is emitted so the LLM is aware. The
  production path runs queries through the named-query registry + the SQL
  safety gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.schemas import (
    CanonicalDocument,
    DataBindingType,
    ParsedChunk,
    TemplateSection,
)
from shared.schemas.template import (
    ApiCallBinding,
    ComputedMetricBinding,
    FileRefBinding,
    FileSetBinding,
    FreeTextInputBinding,
    NamedQueryBinding,
    SqlQueryBinding,
)


@dataclass(frozen=True)
class ResolvedBinding:
    binding_id: str
    binding_type: DataBindingType
    chunks: tuple[ParsedChunk, ...] = ()
    text_value: str | None = None
    deferred_note: str | None = None


@dataclass(frozen=True)
class ResolvedSectionContext:
    section: TemplateSection
    bindings: tuple[ResolvedBinding, ...]
    # All chunks across resolved file_set / file_ref bindings, in retrieval
    # order. citation_id ↔ chunk_id mapping is built one layer up.
    all_chunks: tuple[ParsedChunk, ...] = field(default_factory=tuple)


class BindingResolver:
    """Resolves data bindings to chunks/text. PoC simple-tag-filter retrieval."""

    def __init__(
        self,
        *,
        chunks_by_doc: dict[str, list[ParsedChunk]],
        docs_by_id: dict[str, CanonicalDocument],
        free_text_inputs: dict[str, str],
        max_chunks_per_binding: int = 40,
    ) -> None:
        self._chunks_by_doc = chunks_by_doc
        self._docs_by_id = docs_by_id
        self._free_text_inputs = free_text_inputs
        self._max_chunks_per_binding = max_chunks_per_binding

    def resolve(self, section: TemplateSection) -> ResolvedSectionContext:
        resolved: list[ResolvedBinding] = []
        all_chunks: list[ParsedChunk] = []
        seen_chunk_ids: set[str] = set()

        for binding in section.data_bindings:
            rb: ResolvedBinding
            if isinstance(binding, FileSetBinding):
                chunks = self._chunks_for_tags(binding.filter_tags)[
                    : self._max_chunks_per_binding
                ]
                rb = ResolvedBinding(
                    binding_id=binding.binding_id,
                    binding_type=binding.type,
                    chunks=tuple(chunks),
                )
            elif isinstance(binding, FileRefBinding):
                chunks = self._chunks_by_doc.get(binding.doc_id, [])[
                    : self._max_chunks_per_binding
                ]
                rb = ResolvedBinding(
                    binding_id=binding.binding_id,
                    binding_type=binding.type,
                    chunks=tuple(chunks),
                )
            elif isinstance(binding, FreeTextInputBinding):
                value = self._free_text_inputs.get(binding.binding_id)
                if value is None and binding.required:
                    raise KeyError(
                        f"missing required free_text_input: {binding.binding_id!r}"
                    )
                rb = ResolvedBinding(
                    binding_id=binding.binding_id,
                    binding_type=binding.type,
                    text_value=value or "",
                )
            elif isinstance(
                binding,
                (NamedQueryBinding, SqlQueryBinding, ComputedMetricBinding, ApiCallBinding),
            ):
                rb = ResolvedBinding(
                    binding_id=binding.binding_id,
                    binding_type=binding.type,
                    deferred_note=(
                        f"[PoC: {binding.type.value} binding {binding.binding_id!r} "
                        "is not executed in the local PoC. The production pipeline "
                        "would execute it through the named-query registry + SQL "
                        "safety gate and inject the resulting table here.]"
                    ),
                )
            else:  # pragma: no cover - exhaustiveness guard
                raise TypeError(f"unknown binding type: {type(binding)!r}")

            resolved.append(rb)
            for chunk in rb.chunks:
                if chunk.chunk_id not in seen_chunk_ids:
                    seen_chunk_ids.add(chunk.chunk_id)
                    all_chunks.append(chunk)

        return ResolvedSectionContext(
            section=section, bindings=tuple(resolved), all_chunks=tuple(all_chunks)
        )

    def _chunks_for_tags(self, filter_tags: list[str]) -> list[ParsedChunk]:
        if not filter_tags:
            return []
        target = set(t.lower() for t in filter_tags)
        results: list[ParsedChunk] = []
        for doc_id, chunks in self._chunks_by_doc.items():
            doc_tags = (
                set(self._docs_by_id[doc_id].tags) if doc_id in self._docs_by_id else set()
            )
            doc_tags = {t.lower() for t in doc_tags}
            chunk_tags_match = any(
                target & {t.lower() for t in c.tags} for c in chunks
            )
            if target & doc_tags or chunk_tags_match:
                results.extend(chunks)
        return results
