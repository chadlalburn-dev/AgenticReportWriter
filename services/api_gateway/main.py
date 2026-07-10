"""FastAPI dev server for the Report Generator Agent (api-gateway service).

This is the architecture's designated HTTP ingress. In production it sits
behind IAP and fans out to the other Cloud Run services; locally it wires
the pipeline together with the StubLlmClient so the whole flow runs
end-to-end with NO cloud access (no Vertex, no ADC, no keys).

Run locally:
    uvicorn services.api_gateway.main:app --host 127.0.0.1 --port 8080
or via .claude/launch.json (server name: "report-agent-api").

Endpoints:
    GET  /health              liveness
    GET  /                    HTML landing page
    GET  /templates           shipped template library
    POST /generate/ib-demo    run the synthetic IB pipeline end-to-end
    GET  /docs                interactive Swagger UI (FastAPI built-in)
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from services.api_integration import (
    ApiCallGate,
    ApiConnectorRegistry,
    MockChemblConnector,
    MockClinicalTrialsConnector,
)
from services.generation_orchestrator.orchestrator import ReportGenerator
from services.ingestion_service.connectors import ConnectorContext, LocalFileConnector
from services.parsing_service.registry import default_registry
from services.template_service import LibraryAdapter
from shared.llm import LlmRequest, LlmResponse, StubLlmClient
from shared.schemas import ReportTemplate

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "samples" / "synthetic_compound" / "sources"
IB_TEMPLATE = REPO_ROOT / "templates" / "library" / "ich_e6_ib.json"

app = FastAPI(
    title="Report Generator Agent — api-gateway (dev)",
    version="0.1.0",
    description=(
        "Local development ingress. Generation runs use a stub LLM so the "
        "full plan->fill->critique pipeline works without cloud access. "
        "Wire Vertex AI Claude (VertexLlmClient) for real output."
    ),
)

_CITE_RE = re.compile(r"\[citation_id=([0-9a-f-]+)\]")


def _smart_stub() -> StubLlmClient:
    """Deterministic stub that returns structurally valid responses by
    echoing the allocated citation_ids back (same shape the tests use)."""
    stub = StubLlmClient(strict=True)

    stub.register_handler(
        lambda r: r.response_schema_name == "PlanOutput",
        lambda r: stub.make_response(
            parsed_json={"overall_summary": "Dev-server stub plan.", "section_plans": []}
        ),
    )

    def _fill(r: LlmRequest) -> LlmResponse:
        msg = r.messages[-1].content
        ids = _CITE_RE.findall(msg)
        m = re.search(r"Target length:\s*(\d+)-(\d+)", msg)
        lo, hi = (int(m.group(1)), int(m.group(2))) if m else (200, 800)
        body = (
            "This section was produced by the local dev-server stub LLM; "
            "wire Vertex AI Claude for real text. "
        ) * max(1, ((lo + hi) // 2) // 20)
        words = body.split()
        if len(words) > hi:
            body = " ".join(words[:hi])
        claims = [{"text": "Stub claim.", "citation_ids": [ids[0]]}] if ids else []
        return stub.make_response(
            parsed_json={"paragraphs": [{"text": body, "claims": claims}]}
        )

    stub.register_handler(lambda r: r.response_schema_name == "FillOutput", _fill)
    stub.register_handler(
        lambda r: r.response_schema_name == "CritiqueOutput",
        lambda r: stub.make_response(parsed_json={"verdict": "pass", "issues": []}),
    )
    return stub


def _api_gate() -> ApiCallGate:
    reg = ApiConnectorRegistry()
    reg.register(MockChemblConnector())
    reg.register(MockClinicalTrialsConnector())
    return ApiCallGate(reg)


_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Report Generator Agent</title>
<style>
 body{font-family:system-ui,'Segoe UI',Arial,sans-serif;margin:2rem auto;max-width:720px;color:#1a1a1a;line-height:1.5}
 code{background:#f2f2f2;padding:2px 6px;border-radius:4px;font-size:.95em}
 a{color:#e07000;text-decoration:none} a:hover{text-decoration:underline}
 .tag{color:#666;font-size:.9rem} li{margin:.35rem 0}
 h1{margin-bottom:.2rem} h2{margin-top:1.6rem}
</style></head><body>
<h1>Report Generator Agent <span class="tag">&middot; api-gateway &middot; dev</span></h1>
<p>Local development server. Generation runs use a <strong>stub LLM</strong> &mdash;
no cloud access required. Wire Vertex AI Claude for real output.</p>
<h2>Endpoints</h2>
<ul>
 <li><a href="/health">GET /health</a> &mdash; liveness check</li>
 <li><a href="/templates">GET /templates</a> &mdash; shipped template library (ICH E6 IB, ICH E3 CSR, CONSORT)</li>
 <li><code>POST /generate/ib-demo</code> &mdash; run the synthetic Investigator's Brochure pipeline end-to-end
   (try it in <a href="/docs">/docs</a>)</li>
 <li><a href="/docs">GET /docs</a> &mdash; interactive Swagger UI</li>
</ul>
<p class="tag">plan &rarr; fill &rarr; critique &middot; every claim cited &middot; hash-chained audit trail</p>
</body></html>
"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-gateway", "mode": "dev/stub"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


@app.get("/templates")
def list_templates() -> dict[str, object]:
    adapter = LibraryAdapter()
    out = []
    for tid in adapter.list_ids():
        t = adapter.load(tid)
        out.append(
            {
                "template_id": t.template_id,
                "title": t.title,
                "report_type": t.report_type,
                "sections": len(t.all_sections()),
                "status": t.status.value,
            }
        )
    return {"count": len(out), "templates": out}


@app.post("/generate/ib-demo")
def generate_ib_demo() -> dict[str, object]:
    """Run the synthetic Investigator's Brochure pipeline end-to-end with
    the stub LLM. Ingests the committed synthetic corpus, resolves API
    connector bindings (mock ChEMBL + ClinicalTrials), runs
    plan->fill->critique, and returns a JSON summary. No cloud access."""
    template = ReportTemplate.model_validate(
        json.loads(IB_TEMPLATE.read_text(encoding="utf-8"))
    )
    connector = LocalFileConnector()
    ctx = ConnectorContext(tenant_id="gsk", team_id="ib-pilot", run_id="api-demo")
    registry = default_registry()
    documents = []
    chunks_by_doc: dict[str, list] = {}
    for doc, raw in connector.ingest(str(CORPUS), ctx):
        documents.append(doc)
        chunks_by_doc[doc.doc_id] = registry.parse(doc, raw)

    generator = ReportGenerator(
        fill_client=_smart_stub(),
        api_gate=_api_gate(),
        max_retries_per_section=1,
    )
    result = generator.generate(
        template=template,
        documents=documents,
        chunks_by_doc=chunks_by_doc,
        free_text_inputs={
            "product_name": "XYZ-001",
            "compound_id": "XYZ-001",
            "target_name": "Kinase Z",
            "indication_keyword": "Kinase Z",
            "sponsor_name": "Acme Therapeutics (synthetic)",
            "ib_edition": "Edition 1.0",
            "release_date": "2026-05-27",
        },
        project_id="dev/api-demo",
        tenant_id="gsk",
        actor_id="api-gateway-dev",
    )
    action_counts = Counter(e.action.value for e in result.audit_events)
    return {
        "instance_id": result.instance.instance_id,
        "template": f"{result.instance.template_id}@{result.instance.template_version}",
        "documents_ingested": len(documents),
        "chunks": sum(len(c) for c in chunks_by_doc.values()),
        "sections": len(template.all_sections()),
        "citations": len(result.citations),
        "audit_events": len(result.audit_events),
        "audit_by_action": dict(action_counts),
        "note": "Generated with the dev-server StubLlmClient. Wire Vertex AI Claude (VertexLlmClient) for real text.",
    }
