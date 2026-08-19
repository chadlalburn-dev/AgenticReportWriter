"""FastAPI dev server for the Report Generator Agent (api-gateway service).

This is the architecture's designated HTTP ingress. In production it sits
behind IAP and fans out to the other Cloud Run services; locally it wires
the pipeline together with the StubLlmClient so the whole flow runs
end-to-end with NO cloud access (no Vertex, no ADC, no keys).

Run locally:
    uvicorn services.api_gateway.main:app --host 127.0.0.1 --port 8080
or via .claude/launch.json (server name: "report-agent-api").

Everything except /health lives on the UI router in `services.api_gateway.ui`:

    GET  /health                 liveness (defined here, above every router)
    GET  /                       report-type gallery
    GET  /runs                   run history
    GET  /new/{template_key}     inputs + preflight
    POST /runs                   start a run  -> 303 /runs/{run_id}
    GET  /runs/{run_id}          progress / draft / sources / log
    GET  /api/...                the JSON surface (see ui.py)
    GET  /templates              307 -> /api/templates      (legacy shim)
    POST /generate/ib-demo       307 -> /api/generate/ib-demo (legacy shim)
    GET  /docs                   interactive Swagger UI (FastAPI built-in)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from services.api_gateway import ui as ui_module

_HERE = Path(__file__).resolve().parent


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    ui_module.get_store().shutdown(wait=False)


app = FastAPI(
    title="Report Generator Agent — api-gateway (dev)",
    version="0.2.0",
    description=(
        "Local development ingress. Generation runs use a stub LLM so the "
        "full plan->fill->critique pipeline works without cloud access. "
        "Wire Vertex AI Claude (VertexLlmClient) for real output."
    ),
    lifespan=_lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-gateway", "mode": "dev/stub"}


app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
app.include_router(ui_module.router)
ui_module.install_error_handlers(app)
