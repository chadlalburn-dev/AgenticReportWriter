"""End-to-end: an authored Markdown report-template runs through the real
generation engine, pulling from BigQuery (SQLite stand-in), Confluence (mock),
and files (synthetic corpus) in one run — with citations tracing to each.

This proves the whole 'template doc -> multi-source -> cited draft' chain.
Live GSK BigQuery/Confluence swap in behind the same interfaces; here we use
the local stand-ins so it runs offline.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from services.api_integration import (
    ApiCallGate,
    ApiConnectorRegistry,
    MockConfluenceConnector,
)
from services.data_integration import (
    NamedQueryRegistry,
    SqlSafetyGate,
    SqliteQueryExecutor,
)
from services.generation_orchestrator.orchestrator import ReportGenerator
from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector
from services.parsing_service.registry import default_registry
from services.template_service import load_report_doc
from shared.llm import LlmRequest, LlmResponse, StubLlmClient
from shared.schemas import SourceType

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "samples" / "synthetic_compound" / "sources"
QUERIES = REPO_ROOT / "samples" / "synthetic_compound" / "queries"
EDC = REPO_ROOT / "samples" / "synthetic_compound" / "edc.sqlite"

# An authored report-template doc that pulls all three source kinds.
DOC = """---
report_type: e2e_multi_source
title: End-to-end multi-source report
version: 0.1.0
inputs:
  - id: compound_id
    prompt: Compound id
    required: true
sources:
  - id: ae
    type: bigquery
    dataset: edc_warehouse
    query_id: ae_summary_by_soc_v3
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: context
    type: confluence
    space: PSS
    cql: 'Kinase Z target rationale'
  - id: prior
    type: file
    filter_tags: [nonclinical]
citation:
  required: true
  granularity: claim
  min_per_paragraph: 1
output:
  formats: [html]
---

# End-to-end multi-source report

## 1. Safety and context

> Instruction: Summarise the safety signal and the target context.
> Sources: ae, context, prior
> Table: ae
"""


@pytest.fixture(scope="module", autouse=True)
def _fixtures() -> None:
    # Seed the EDC SQLite + synthetic corpus if not already present.
    if not EDC.exists():
        spec = importlib.util.spec_from_file_location(
            "_seed", REPO_ROOT / "samples" / "synthetic_compound" / "seed_db.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        mod.seed()
    if not CORPUS.exists() or not any(CORPUS.rglob("*.docx")):
        spec = importlib.util.spec_from_file_location(
            "_corpus", REPO_ROOT / "samples" / "synthetic_compound" / "generate_corpus.py"
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        mod.main()


_CITE_RE = re.compile(r"\[citation_id=([0-9a-f-]+)\]")


def _cite_all_stub() -> StubLlmClient:
    """A stub that cites EVERY citation_id it is shown — so the resulting
    citations span all source types present (chunks, DB tables, API tables)."""
    stub = StubLlmClient(strict=True)
    stub.register_handler(
        lambda r: r.response_schema_name == "PlanOutput",
        lambda r: stub.make_response(parsed_json={"overall_summary": "x", "section_plans": []}),
    )

    def fill(r: LlmRequest) -> LlmResponse:
        ids = _CITE_RE.findall(r.messages[-1].content)
        claims = [{"text": f"claim {i}", "citation_ids": [cid]} for i, cid in enumerate(ids)]
        return stub.make_response(
            parsed_json={"paragraphs": [{"text": "Draft narrative.", "claims": claims}]}
        )

    stub.register_handler(lambda r: r.response_schema_name == "FillOutput", fill)
    stub.register_handler(
        lambda r: r.response_schema_name == "CritiqueOutput",
        lambda r: stub.make_response(parsed_json={"verdict": "pass", "issues": []}),
    )
    return stub


def test_authored_template_runs_across_all_sources(tmp_path: Path) -> None:
    doc_path = tmp_path / "e2e_multi_source.md"
    doc_path.write_text(DOC, encoding="utf-8")
    template = load_report_doc(doc_path)

    # BigQuery stand-in: SQLite executor + the sample named-query registry
    sql_gate = SqlSafetyGate(
        executor=SqliteQueryExecutor(EDC, source="edc_warehouse", read_only=True),
        registry=NamedQueryRegistry.from_directory(QUERIES),
    )
    # Confluence: the mock connector behind the API gate
    api_reg = ApiConnectorRegistry()
    api_reg.register(MockConfluenceConnector())
    api_gate = ApiCallGate(api_reg)

    # Files: the synthetic corpus
    connector = LocalFileConnector()
    ctx = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="e2e")
    parser = default_registry()
    docs, chunks_by_doc = [], {}
    for doc, raw in connector.ingest(str(CORPUS), ctx):
        docs.append(doc)
        chunks_by_doc[doc.doc_id] = parser.parse(doc, raw)

    gen = ReportGenerator(
        fill_client=_cite_all_stub(),
        safety_gate=sql_gate,
        api_gate=api_gate,
        max_retries_per_section=0,
    )
    result = gen.generate(
        template=template,
        documents=docs,
        chunks_by_doc=chunks_by_doc,
        free_text_inputs={"compound_id": "XYZ-001"},
        project_id="test/e2e",
    )

    assert result.citations, "expected citations from the multi-source run"
    source_types = {c.source_type for c in result.citations}
    # The BigQuery (SQL) table and the Confluence (API) page both cited:
    assert SourceType.SQL in source_types, f"no SQL citation; got {source_types}"
    assert SourceType.API in source_types, f"no API citation; got {source_types}"
