"""SharePoint / OneDrive source connectors (ApiConnector implementations).

Two implementations behind the same ApiCallGate as everything else:

- MockSharePointConnector: fixture-backed, no network. Lets a template that
  pulls a deck resolve offline, in tests and in the demo.
- SharePointConnector: Microsoft Graph. Code-ready, and honest about the fact
  that it cannot reach anything from this machine.

Operations (both):
  search_files  params: {site, query, folder, file_types}  -> matching files
  get_file      params: {site, folder, file_types}         -> a folder's files

Why a connector rather than a file-set binding
----------------------------------------------
`file` reads a local directory and cannot fail in an interesting way. This
crosses a network, needs a configured identity, and can be unreachable —
which is a different thing to say to a reader, and needs the gate's audit
record. Collapsing the two would hide a network dependency behind something
that looks like listing a folder.

What comes back, and what does not
----------------------------------
Rows are (file_id, name, path, modified, excerpt) so the filler can render and
cite them. `excerpt` is text already extracted upstream — this connector does
**not** parse .pptx itself. Slide extraction belongs in the parsing service,
which already handles the corpus formats and is where a citation gets its page
or slide locator. A connector that quietly grew its own parser would produce
citations that point at nothing the rest of the app can resolve.

Delivery note
-------------
In the Skills model these files can alternatively be pulled through the M365
connector and injected here as an ApiCallResult — same downstream path, same
audit record. That is the likelier route at GSK, where a direct Graph
registration needs an app registration this app does not have.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api_integration.connector import (
    ApiCallResult,
    ApiConnector,
    ApiOperationError,
)
from shared.connectivity import ConnectorStatus, unchecked, unconfigured
from shared.http_transport import HttpTransport

_COLUMNS = ("file_id", "name", "path", "modified", "excerpt")

ENV_TENANT = "REPORTGEN_GRAPH_TENANT_ID"
ENV_CLIENT = "REPORTGEN_GRAPH_CLIENT_ID"
ENV_SECRET = "REPORTGEN_GRAPH_CLIENT_SECRET"


def _excerpt(body: str, n: int = 600) -> str:
    body = " ".join((body or "").split())
    return body if len(body) <= n else body[:n].rstrip() + "…"


def _wanted_types(raw: Any) -> tuple[str, ...]:
    """`"pptx, docx"` -> `("pptx", "docx")`. Empty means every type.

    Empty is a real choice and not a mistake, but the editor warns about it:
    without a filter this pulls every document in scope, including ones nobody
    meant to cite.
    """
    text = str(raw or "").strip()
    if not text:
        return ()
    return tuple(
        part.strip().lower().lstrip(".") for part in text.split(",") if part.strip()
    )


def _matches_type(name: str, wanted: tuple[str, ...]) -> bool:
    if not wanted:
        return True
    lowered = name.lower()
    return any(lowered.endswith("." + ext) for ext in wanted)


@dataclass(frozen=True)
class _File:
    file_id: str
    name: str
    site: str
    path: str
    modified: str
    body: str


def _rows(files: list[_File]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (f.file_id, f.name, f.path, f.modified, _excerpt(f.body)) for f in files
    )


def _result(op: str, params: Mapping[str, Any], files: list[_File]) -> ApiCallResult:
    return ApiCallResult(
        connector_id="sharepoint",
        operation_id=op,
        parameters=dict(params),
        columns=_COLUMNS,
        rows=_rows(files),
        raw={
            "files": [
                {
                    "file_id": f.file_id,
                    "name": f.name,
                    "site": f.site,
                    "path": f.path,
                    "modified": f.modified,
                    "body": f.body,
                }
                for f in files
            ]
        },
        row_count=len(files),
        source="sharepoint",
    )


# --- Mock (fixture-backed) -------------------------------------------------


_DEMO_FILES: tuple[_File, ...] = (
    _File(
        file_id="sp-0001",
        name="XYZ-001 tox review - candidate selection.pptx",
        site="Nonclinical-Safety",
        path="Programmes/XYZ-001/Tox/XYZ-001 tox review - candidate selection.pptx",
        modified="2026-06-11T09:20:00Z",
        body=(
            "Candidate-selection tox review for XYZ-001. Rat and dog pivotal "
            "studies complete. Principal watch item: reversible hepatocellular "
            "hypertrophy at high dose. Exposure-margin confirmation requested "
            "before nomination. (Synthetic content for demo.)"
        ),
    ),
    _File(
        file_id="sp-0002",
        name="Safety pharmacology summary.pptx",
        site="Nonclinical-Safety",
        path="Programmes/XYZ-001/Tox/Safety pharmacology summary.pptx",
        modified="2026-05-28T14:05:00Z",
        body=(
            "hERG IC50 12 uM, approximately 60-fold over projected human Cmax. "
            "Anaesthetised dog cardiovascular study: no clinically meaningful "
            "change up to 30 mg/kg IV. Respiratory and CNS not yet run. "
            "(Synthetic content for demo.)"
        ),
    ),
    _File(
        file_id="sp-0003",
        name="Programme status.docx",
        site="Nonclinical-Safety",
        path="Programmes/XYZ-001/Programme status.docx",
        modified="2026-07-02T08:00:00Z",
        body=(
            "XYZ-001 at candidate-selection stage. Next milestone: nomination "
            "review. (Synthetic content for demo.)"
        ),
    ),
)


class MockSharePointConnector(ApiConnector):
    """Fixture-backed SharePoint. No network, deterministic ordering.

    Deterministic because a report that reorders its own evidence between two
    runs of the same template is a report whose citation numbers move, and
    citation numbers that move are worse than useless in review.
    """

    connector_id = "sharepoint"
    allowed_operations = frozenset({"search_files", "get_file"})

    def __init__(self, files: tuple[_File, ...] = _DEMO_FILES) -> None:
        self._files = tuple(files)

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            connector_id=self.connector_id,
            kind="sharepoint",
            configured=True,
            reachable=True,
            detail=(
                f"Offline fixture with {len(self._files)} synthetic files. "
                "Nothing leaves this machine."
            ),
        )

    def probe(self) -> ConnectorStatus:
        return self.status()

    def call(self, operation_id: str, parameters: Mapping[str, Any]) -> ApiCallResult:
        if operation_id not in self.allowed_operations:
            raise ApiOperationError(
                f"MockSharePointConnector: unknown operation {operation_id!r}"
            )

        site = str(parameters.get("site") or "").strip().lower()
        folder = str(parameters.get("folder") or "").strip().lower()
        wanted = _wanted_types(parameters.get("file_types"))
        query = str(parameters.get("query") or "").strip().lower()

        hits = [
            f
            for f in self._files
            if (not site or f.site.lower() == site)
            and (not folder or f.path.lower().startswith(folder))
            and _matches_type(f.name, wanted)
            and (
                not query
                or query in f.name.lower()
                or query in f.body.lower()
            )
        ]
        return _result(operation_id, parameters, sorted(hits, key=lambda f: f.file_id))


# --- Real (Microsoft Graph) -------------------------------------------------


class SharePointConnector(ApiConnector):
    """SharePoint / OneDrive over Microsoft Graph.

    Cannot reach anything from this machine, and says so rather than failing
    obscurely mid-run: GSK's tenant needs an app registration this application
    does not have, and the repo does not carry secrets. `status()` reports that
    from configuration alone; `probe()` is the only thing that touches the
    network, and only when a human asks.
    """

    connector_id = "sharepoint"
    allowed_operations = frozenset({"search_files", "get_file"})

    _SCOPE = "https://graph.microsoft.com/.default"
    _BASE = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self._tenant = tenant_id or os.environ.get(ENV_TENANT, "")
        self._client = client_id or os.environ.get(ENV_CLIENT, "")
        self._secret = client_secret or os.environ.get(ENV_SECRET, "")
        #: Proxy, trust store and timeout. Without this every call went out with
        #: no proxy and the default CA set, which on a GSK laptop fails against
        #: the TLS-inspecting proxy before it ever reaches Graph.
        self._transport = transport or HttpTransport()

    @property
    def _timeout_s(self) -> float:
        return self._transport.timeout_s

    # -- connectivity -------------------------------------------------------

    def _missing(self) -> tuple[str, ...]:
        missing = []
        if not self._tenant:
            missing.append(ENV_TENANT)
        if not self._client:
            missing.append(ENV_CLIENT)
        if not self._secret:
            missing.append(ENV_SECRET)
        return tuple(missing)

    def status(self) -> ConnectorStatus:
        missing = self._missing()
        if missing:
            return unconfigured(
                self.connector_id,
                "sharepoint",
                missing,
                hint=(
                    "Needs an Entra app registration with Files.Read.All. At "
                    "GSK that is a request to the platform team, not something "
                    "this app can provision."
                ),
            )
        return unchecked(self.connector_id, "sharepoint")

    def probe(self) -> ConnectorStatus:
        base = self.status()
        if not base.configured:
            return base
        try:
            self._token()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            return ConnectorStatus(
                connector_id=self.connector_id,
                kind="sharepoint",
                configured=True,
                reachable=False,
                detail=f"Could not obtain a Graph token: {_short(exc)}",
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            kind="sharepoint",
            configured=True,
            reachable=True,
            detail=(
                f"Graph issued a token for the configured registration "
                f"({self._transport.describe()})."
            ),
        )

    # -- calls --------------------------------------------------------------

    def call(self, operation_id: str, parameters: Mapping[str, Any]) -> ApiCallResult:
        if operation_id not in self.allowed_operations:
            raise ApiOperationError(
                f"SharePointConnector: unknown operation {operation_id!r}"
            )
        status = self.status()
        if not status.configured:
            # A sentence naming what is missing, not a traceback four frames
            # deep in urllib. This is the message that reaches the ledger and
            # then the page.
            raise ApiOperationError(f"SharePoint is not configured. {status.detail}")

        wanted = _wanted_types(parameters.get("file_types"))
        query = str(parameters.get("query") or "").strip()
        site = str(parameters.get("site") or "").strip()
        folder = str(parameters.get("folder") or "").strip()

        if operation_id == "search_files":
            payload = self._search(site, query)
        else:
            payload = self._list_folder(site, folder)

        files = [
            f for f in (_from_graph(item, site) for item in payload)
            if _matches_type(f.name, wanted)
        ]
        return _result(operation_id, parameters, sorted(files, key=lambda f: f.file_id))

    def _token(self) -> str:
        import json
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode(
            {
                "client_id": self._client,
                "client_secret": self._secret,
                "scope": self._SCOPE,
                "grant_type": "client_credentials",
            }
        ).encode()
        url = f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        opener = self._transport.opener()
        with opener.open(req, timeout=self._timeout_s) as resp:  # noqa: S310
            token = json.loads(resp.read().decode("utf-8")).get("access_token")
        if not token:
            raise ApiOperationError("Graph returned no access_token")
        return str(token)

    def _get_json(self, path: str) -> dict[str, Any]:
        import json
        import urllib.request

        req = urllib.request.Request(self._BASE + path)
        req.add_header("Authorization", f"Bearer {self._token()}")
        req.add_header("Accept", "application/json")
        try:
            opener = self._transport.opener()
            with opener.open(req, timeout=self._timeout_s) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network dependent
            raise ApiOperationError(f"Graph request failed: {_short(exc)}") from exc

    def _search(self, site: str, query: str) -> list[dict[str, Any]]:
        import urllib.parse

        scope = f"/sites/{urllib.parse.quote(site)}" if site else "/me"
        q = urllib.parse.quote(query or "")
        data = self._get_json(f"{scope}/drive/root/search(q='{q}')")
        return list(data.get("value") or [])

    def _list_folder(self, site: str, folder: str) -> list[dict[str, Any]]:
        import urllib.parse

        scope = f"/sites/{urllib.parse.quote(site)}" if site else "/me"
        path = urllib.parse.quote(folder.strip("/"))
        suffix = f":/{path}:/children" if path else "/children"
        data = self._get_json(f"{scope}/drive/root{suffix}")
        return list(data.get("value") or [])


def _from_graph(item: Mapping[str, Any], site: str) -> _File:
    parent = (item.get("parentReference") or {}).get("path", "")
    name = str(item.get("name") or "")
    return _File(
        file_id=str(item.get("id") or ""),
        name=name,
        site=site,
        path=f"{parent}/{name}".replace("/drive/root:", "").lstrip("/"),
        modified=str(item.get("lastModifiedDateTime") or ""),
        # Graph does not return document text on a listing, and this connector
        # deliberately does not parse .pptx — that is the parsing service's job,
        # and it is where a citation gets its slide locator.
        body="",
    )


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= 180 else text[:180].rstrip() + "…"
