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
from starlette.types import Scope

from services.api_gateway import ui as ui_module

_HERE = Path(__file__).resolve().parent


class _RevalidatingStatic(StaticFiles):
    """StaticFiles that always forces a revalidation.

    Starlette sends `etag` and `last-modified` but no `Cache-Control`. With no
    directive, browsers fall back to heuristic freshness and will happily serve
    a cached stylesheet WITHOUT asking us whether it changed — so an edit to
    titanium.css does not show up on reload, only on a hard refresh. That
    wasted real debugging time ("I don't see any changes") and the fix belongs
    in the server, not in a habit of hard-refreshing.

    `no-cache` does not mean "do not store" — it means "revalidate before
    use". The etag still answers 304 Not Modified, so a reload of an unchanged
    file is one cheap conditional request, not a re-download. This is a
    local-only dev server; correctness beats a saved round trip.
    """

    def file_response(self, *args: object, **kwargs: object):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


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


app.mount("/static", _RevalidatingStatic(directory=str(_HERE / "static")), name="static")
app.include_router(ui_module.router)
ui_module.install_error_handlers(app)
