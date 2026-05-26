"""Binding resolution + chunk retrieval.

Given a TemplateSection's data_bindings and the available pool of
CanonicalDocuments + ParsedChunks (plus, optionally, a SqlSafetyGate
into the data warehouse), this module produces a `ResolvedSectionContext`
that the section filler turns into an LLM prompt.

Per-binding behaviour:
- file_set      → tag-based filtering over the chunk pool
- file_ref      → chunks whose source_doc_id matches
- free_text_input → pulled from the ReportInstance.free_text_inputs map
- named_query   → executed through the SqlSafetyGate (registry + dry-run),
                  result attached to the binding as a ResolvedQueryResult
- sql_query     → LLM-drafted SQL, runs through the gate's lint + dry-run
                  + approval pipeline before execution
- api_call / computed_metric → not yet executed; emit a deferred_note

If no safety gate is provided, query-typed bindings emit deferred_notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.data_integration import (
    ResolvedQueryResult,
    SafetyVerdict,
    SqlSafetyGate,
    SqlSafetyViolation,
)
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


_REPORT_PARAM_RE = re.compile(r"\{\{\s*report\.([\w]+)\s*\}\}")


@dataclass(frozen=True)
class ResolvedBinding:
    binding_id: str
    binding_type: DataBindingType
    chunks: tuple[ParsedChunk, ...] = ()
    text_value: str | None = None
    query_result: ResolvedQueryResult | None = None
    query_verdict: SafetyVerdict | None = None
    deferred_note: str | None = None


@dataclass(frozen=True)
class ResolvedSectionContext:
    section: TemplateSection
    bindings: tuple[ResolvedBinding, ...]
    all_chunks: tuple[ParsedChunk, ...] = field(default_factory=tuple)


class BindingResolver:
    """Resolves data bindings to chunks/text/tables.

    The `safety_gate`, when provided, executes NAMED_QUERY and SQL_QUERY
    bindings. {{report.X}} placeholders in binding parameters are
    substituted from `free_text_inputs`. No gate → those bindings emit
    deferred_notes so the demo still runs without an EDC backend.
    """

    def __init__(
        self,
        *,
        chunks_by_doc: dict[str, list[ParsedChunk]],
        docs_by_id: dict[str, CanonicalDocument],
        free_text_inputs: dict[str, str],
        max_chunks_per_binding: int = 40,
        safety_gate: SqlSafetyGate | None = None,
    ) -> None:
        self._chunks_by_doc = chunks_by_doc
        self._docs_by_id = docs_by_id
        self._free_text_inputs = free_text_inputs
        self._max_chunks_per_binding = max_chunks_per_binding
        self._safety_gate = safety_gate

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
            elif isinstance(binding, NamedQueryBinding):
                rb = self._resolve_named_query(binding)
            elif isinstance(binding, SqlQueryBinding):
                rb = self._resolve_sql_query(binding)
            elif isinstance(binding, (ComputedMetricBinding, ApiCallBinding)):
                rb = ResolvedBinding(
                    binding_id=binding.binding_id,
                    binding_type=binding.type,
                    deferred_note=(
                        f"[PoC: {binding.type.value} binding {binding.binding_id!r} "
                        "is not executed in the local PoC.]"
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

    # -- Query bindings -----------------------------------------------------

    def _resolve_named_query(self, binding: NamedQueryBinding) -> ResolvedBinding:
        if self._safety_gate is None:
            return ResolvedBinding(
                binding_id=binding.binding_id,
                binding_type=binding.type,
                deferred_note=(
                    f"[PoC: named_query binding {binding.binding_id!r} "
                    f"(query_id={binding.query_id!r}) was not executed because no "
                    "SqlSafetyGate was provided to the orchestrator. Pass a gate "
                    "configured against your data warehouse to enable this binding.]"
                ),
            )
        params = self._substitute_report_params(binding.parameters)
        try:
            result, verdict = self._safety_gate.run_named_query(
                binding.query_id, params
            )
        except SqlSafetyViolation as exc:
            return ResolvedBinding(
                binding_id=binding.binding_id,
                binding_type=binding.type,
                deferred_note=(
                    f"[named_query binding {binding.binding_id!r} "
                    f"(query_id={binding.query_id!r}) failed the safety gate: "
                    f"{exc.code} — {exc.message}]"
                ),
            )
        return ResolvedBinding(
            binding_id=binding.binding_id,
            binding_type=binding.type,
            query_result=result,
            query_verdict=verdict,
        )

    def _resolve_sql_query(self, binding: SqlQueryBinding) -> ResolvedBinding:
        if self._safety_gate is None:
            return ResolvedBinding(
                binding_id=binding.binding_id,
                binding_type=binding.type,
                deferred_note=(
                    f"[PoC: sql_query binding {binding.binding_id!r} requires "
                    "a SqlSafetyGate + approver. Not configured.]"
                ),
            )
        params = self._substitute_report_params(binding.parameters)
        result, verdict = self._safety_gate.run_llm_drafted(
            binding.sql,
            params,
            intent=f"sql_query binding {binding.binding_id!r}",
        )
        if result is None:
            return ResolvedBinding(
                binding_id=binding.binding_id,
                binding_type=binding.type,
                query_verdict=verdict,
                deferred_note=(
                    f"[sql_query binding {binding.binding_id!r} blocked by the "
                    f"safety gate: {verdict.failure_code} — {verdict.failure_message}]"
                ),
            )
        return ResolvedBinding(
            binding_id=binding.binding_id,
            binding_type=binding.type,
            query_result=result,
            query_verdict=verdict,
        )

    def _substitute_report_params(
        self, parameters: dict[str, str]
    ) -> dict[str, object]:
        """Replace `{{report.X}}` placeholders with values from free_text_inputs."""
        out: dict[str, object] = {}
        for name, expr in parameters.items():
            substituted = _REPORT_PARAM_RE.sub(
                lambda m: self._free_text_inputs.get(m.group(1), m.group(0)), expr
            )
            out[name] = substituted
        return out

    # -- File-set retrieval -------------------------------------------------

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
