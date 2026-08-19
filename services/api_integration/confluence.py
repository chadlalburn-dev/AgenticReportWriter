"""Confluence source connectors (ApiConnector implementations).

Two implementations behind the same ApiCallGate the rest of the engine uses:

- MockConfluenceConnector: fixture-backed, no network. Used for local runs,
  tests, and the end-to-end demo. Seeded with pages for the synthetic
  compound so a template that pulls Confluence context resolves offline.
- ConfluenceConnector: real Confluence Cloud REST v2 via a bearer/basic
  token. Code-ready; lazy-imports nothing heavier than stdlib urllib.

Operations (both):
  search_pages  params: {cql | space | text}    -> matching pages
  get_page      params: {page_id}               -> one page

Each returns an ApiCallResult whose rows are (page_id, title, space, url,
excerpt) so the filler can render + cite them; full body text is in `raw`
so the model has narrative context. Citations point at the page.

In the Skills delivery model, live Confluence can alternatively be pulled by
Claude via the Atlassian MCP connector and the results injected here as an
ApiCallResult — same downstream path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api_integration.connector import (
    ApiCallResult,
    ApiConnector,
    ApiOperationError,
)

_COLUMNS = ("page_id", "title", "space", "url", "excerpt")


def _rows_from_pages(pages: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (p.get("page_id"), p.get("title"), p.get("space"), p.get("url"), _excerpt(p.get("body", "")))
        for p in pages
    )


def _excerpt(body: str, n: int = 600) -> str:
    body = " ".join(body.split())
    return body if len(body) <= n else body[:n].rstrip() + "…"


# --- Mock (fixture-backed) -------------------------------------------------


@dataclass(frozen=True)
class _Page:
    page_id: str
    title: str
    space: str
    body: str

    @property
    def url(self) -> str:
        return f"https://confluence.local/pages/{self.page_id}"


# Seed pages anchored on the synthetic compound / target so the demo resolves.
_MOCK_PAGES: dict[str, _Page] = {
    "100001": _Page(
        "100001",
        "Kinase Z target rationale",
        "PSS",
        "Kinase Z is a receptor tyrosine kinase implicated in ~12% of solid "
        "tumours; activating alterations drive MAPK and PI3K-AKT signalling. "
        "Selective inhibition is hypothesised to give tumour regression in "
        "Kinase Z-altered cancers with a wider therapeutic window than "
        "broad-spectrum kinase inhibitors. (Synthetic content for demo.)",
    ),
    "100002": _Page(
        "100002",
        "XYZ-001 programme background",
        "PSS",
        "XYZ-001 is a small-molecule selective inhibitor of Kinase Z developed "
        "as an oral therapy for Kinase Z-positive solid tumours. Lead series "
        "optimised for selectivity and oral exposure. (Synthetic content.)",
    ),
    "100003": _Page(
        "100003",
        "XYZ-001 governance decisions",
        "PSS",
        "Prior candidate-selection review noted acceptable rat/dog tox margins "
        "and flagged reversible hepatocellular hypertrophy at high dose as the "
        "principal watch item; requested exposure-margin confirmation. "
        "(Synthetic content.)",
    ),
    "100004": _Page(
        "100004",
        "XYZ-001 status",
        "PSS",
        "Status: candidate-selection stage. Next milestone: nomination review. "
        "Open risk: confirm high-dose hepatic finding reversibility. (Synthetic.)",
    ),
}


class MockConfluenceConnector(ApiConnector):
    connector_id = "confluence"
    allowed_operations = frozenset({"search_pages", "get_page"})

    def call(self, operation_id: str, parameters: Mapping[str, Any]) -> ApiCallResult:
        if operation_id == "search_pages":
            return self._search(parameters)
        if operation_id == "get_page":
            return self._get(parameters)
        raise ApiOperationError(f"MockConfluenceConnector: unknown operation {operation_id!r}")

    def _search(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        needle = " ".join(
            str(parameters.get(k, "")) for k in ("cql", "text", "space")
        ).lower()
        # Loose keyword match against title/body (mimics a CQL text search).
        tokens = [t for t in _tokenize(needle) if len(t) > 2]
        matches = []
        for p in _MOCK_PAGES.values():
            hay = (p.title + " " + p.body).lower()
            if any(t in hay for t in tokens) or not tokens:
                matches.append(p)
        pages = [_page_dict(p) for p in matches]
        return self._result("search_pages", parameters, pages)

    def _get(self, parameters: Mapping[str, Any]) -> ApiCallResult:
        pid = str(parameters.get("page_id", ""))
        p = _MOCK_PAGES.get(pid)
        if p is None:
            raise ApiOperationError(f"MockConfluenceConnector.get_page: {pid!r} not found")
        return self._result("get_page", parameters, [_page_dict(p)])

    @staticmethod
    def _result(op: str, params: Mapping[str, Any], pages: list[dict[str, Any]]) -> ApiCallResult:
        return ApiCallResult(
            connector_id="confluence",
            operation_id=op,
            parameters=dict(params),
            columns=_COLUMNS,
            rows=_rows_from_pages(pages),
            raw={"pages": pages},
            row_count=len(pages),
            source="confluence(mock)",
        )


def _tokenize(s: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9\-]+", s.lower())


def _page_dict(p: _Page) -> dict[str, Any]:
    return {"page_id": p.page_id, "title": p.title, "space": p.space, "url": p.url, "body": p.body}


# --- Real REST (code-ready) ------------------------------------------------


class ConfluenceConnector(ApiConnector):
    """Confluence Cloud REST v2 connector.

    Auth: a token supplied at construction (Confluence API token / bearer).
    In the GSK Skills model, prefer pulling via the Atlassian MCP connector
    and injecting results; this class is for when the local app has a direct
    Confluence token.
    """

    connector_id = "confluence"
    allowed_operations = frozenset({"search_pages", "get_page"})

    def __init__(self, base_url: str, token: str, *, auth_scheme: str = "Bearer") -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._auth_scheme = auth_scheme

    def call(self, operation_id: str, parameters: Mapping[str, Any]) -> ApiCallResult:
        if operation_id == "search_pages":
            cql = str(parameters.get("cql") or f'text ~ "{parameters.get("text", "")}"')
            data = self._get_json(f"/wiki/rest/api/content/search?cql={_q(cql)}&expand=body.storage,space")
            pages = [_normalize(r) for r in data.get("results", [])]
            return self._result("search_pages", parameters, pages)
        if operation_id == "get_page":
            pid = str(parameters["page_id"])
            data = self._get_json(f"/wiki/rest/api/content/{pid}?expand=body.storage,space")
            return self._result("get_page", parameters, [_normalize(data)])
        raise ApiOperationError(f"ConfluenceConnector: unknown operation {operation_id!r}")

    def _get_json(self, path: str) -> dict[str, Any]:
        import json
        import urllib.request

        req = urllib.request.Request(self._base + path)
        req.add_header("Authorization", f"{self._auth_scheme} {self._token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - internal host
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network dependent
            raise ApiOperationError(f"Confluence request failed: {exc}") from exc

    @staticmethod
    def _result(op: str, params: Mapping[str, Any], pages: list[dict[str, Any]]) -> ApiCallResult:
        return ApiCallResult(
            connector_id="confluence",
            operation_id=op,
            parameters=dict(params),
            columns=_COLUMNS,
            rows=_rows_from_pages(pages),
            raw={"pages": pages},
            row_count=len(pages),
            source="confluence",
        )


def _q(s: str) -> str:
    import urllib.parse

    return urllib.parse.quote(s)


def _normalize(result: dict[str, Any]) -> dict[str, Any]:
    import re

    pid = str(result.get("id", ""))
    title = result.get("title", "")
    space = (result.get("space") or {}).get("key", "")
    html = ((result.get("body") or {}).get("storage") or {}).get("value", "")
    body = re.sub(r"<[^>]+>", " ", html)  # strip storage-format HTML tags
    return {
        "page_id": pid,
        "title": title,
        "space": space,
        "url": f"/wiki/pages/{pid}",
        "body": body,
    }
