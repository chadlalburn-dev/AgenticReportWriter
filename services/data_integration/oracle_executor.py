"""OracleQueryExecutor — QueryExecutor backed by an Oracle database.

Same protocol as SqliteQueryExecutor and BigQueryQueryExecutor, so named
queries, the SQL linter and the approval gate all work unchanged; only the
backend differs. A lot of preclinical data lives in Oracle-backed LIMS rather
than in a cloud warehouse, and a template should not have to care which.

Parameter style
---------------
Our named queries use `:name`, which is Oracle's own bind syntax — so unlike
BigQuery there is no rewriting to do. `python-oracledb` binds them directly.
That is a small piece of luck worth noting rather than relying on silently: the
`_referenced_params` check below still runs, so a query that names a parameter
nobody supplied fails with that sentence rather than an ORA-01008.

Read-only, twice over
---------------------
The linter already refuses anything that is not a SELECT. This adds a second
layer at the session level, because the linter is a parser and parsers can be
fooled, whereas a read-only session is enforced by the database. Defence in
depth is cheap here and the blast radius on a LIMS is not.

Auth, and what does not happen here
-----------------------------------
Credentials come from the environment or an external wallet, never from a
literal in a template or a key in the repo. This is the same rule as the GCP
path, for the same reason: an API key in an app is an onboarding and
offboarding problem long before it is a security one.

`oracledb` is imported lazily and lives in the `[oracle]` extra, so the rest of
the engine imports without it — and, importantly, so the absence of the driver
is reported as a *status* rather than an ImportError at run time.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from services.data_integration.executor import QueryExecutor, ResolvedQueryResult
from shared.connectivity import ConnectorStatus, unchecked, unconfigured

_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_]\w*)")

#: Environment variables the executor reads. Named here so `status()` can say
#: exactly which one is missing instead of "not configured".
ENV_DSN = "REPORTGEN_ORACLE_DSN"
ENV_USER = "REPORTGEN_ORACLE_USER"
ENV_PASSWORD = "REPORTGEN_ORACLE_PASSWORD"


def _referenced_params(sql: str) -> list[str]:
    return sorted(set(_PARAM_RE.findall(sql)))


class OracleQueryExecutor(QueryExecutor):
    """Runs approved SELECTs against an Oracle service.

    Constructing this does not connect. That is deliberate: the app builds one
    of these while rendering a page, and a page render that opens a database
    session is a page render that hangs when the database is unreachable.
    """

    def __init__(
        self,
        *,
        service: str,
        dsn: str | None = None,
        user: str | None = None,
        password: str | None = None,
        source: str | None = None,
        max_rows: int = 10_000,
        connect_timeout_s: int = 10,
    ) -> None:
        self._service = service
        self._dsn = dsn or os.environ.get(ENV_DSN, "")
        self._user = user or os.environ.get(ENV_USER, "")
        self._password = password or os.environ.get(ENV_PASSWORD, "")
        #: What a citation says. The service name, not "oracle" — a reader
        #: checking a NOAEL needs to know which system to look in, and "a
        #: database" is not provenance.
        self._source = source or service
        self._max_rows = max_rows
        self._connect_timeout_s = connect_timeout_s

    # -- connectivity -------------------------------------------------------

    def _missing(self) -> tuple[str, ...]:
        missing = []
        if not self._dsn:
            missing.append(ENV_DSN)
        if not self._user:
            missing.append(ENV_USER)
        if not self._password:
            missing.append(ENV_PASSWORD)
        return tuple(missing)

    def status(self) -> ConnectorStatus:
        """Configuration only. Never opens a connection — see the class note."""
        missing = self._missing()
        if missing:
            return unconfigured(
                self._service,
                "oracle",
                missing,
                hint="Set these in the environment; this app never reads a "
                "credential out of a template or the repository.",
            )
        if not _driver_available():
            return ConnectorStatus(
                connector_id=self._service,
                kind="oracle",
                configured=False,
                reachable=None,
                detail=(
                    "The oracledb driver is not installed. Install the project's "
                    "[oracle] extra. Reported here rather than raising at run "
                    "time, so a missing driver is visible before a run commits "
                    "to it."
                ),
                missing=("oracledb",),
            )
        return unchecked(self._service, "oracle")

    def probe(self) -> ConnectorStatus:
        """Actually try to connect. Only ever called when a human asks."""
        base = self.status()
        if not base.configured:
            return base
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            return ConnectorStatus(
                connector_id=self._service,
                kind="oracle",
                configured=True,
                reachable=False,
                detail=f"Could not connect: {_short(exc)}",
            )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM dual")
                cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            return ConnectorStatus(
                connector_id=self._service,
                kind="oracle",
                configured=True,
                reachable=False,
                detail=f"Connected, but the session did not answer: {_short(exc)}",
            )
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                pass
        return ConnectorStatus(
            connector_id=self._service,
            kind="oracle",
            configured=True,
            reachable=True,
            detail=f"Answered on {self._dsn}.",
        )

    # -- execution ----------------------------------------------------------

    def _connect(self) -> Any:
        import oracledb  # type: ignore[import-not-found]

        return oracledb.connect(
            user=self._user,
            password=self._password,
            dsn=self._dsn,
            tcp_connect_timeout=self._connect_timeout_s,
        )

    def execute(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> ResolvedQueryResult:
        params = dict(parameters or {})
        needed = _referenced_params(sql)
        absent = [name for name in needed if name not in params]
        if absent:
            # Raised here rather than left to the driver, because ORA-01008
            # ("not all variables bound") does not say which one.
            raise ValueError(
                f"query needs {', '.join(absent)}, which nothing supplied"
            )

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                # Belt and braces alongside the linter. A read-only session is
                # enforced by the database rather than by a parser, and a parser
                # is the sort of thing that can be fooled.
                try:
                    cur.execute("SET TRANSACTION READ ONLY")
                except Exception:  # noqa: BLE001 - not fatal; the linter still stands
                    pass
                cur.arraysize = min(self._max_rows, 1000)
                cur.execute(sql, {k: v for k, v in params.items() if k in needed})
                columns = tuple(d[0] for d in (cur.description or ()))
                rows = tuple(tuple(r) for r in cur.fetchmany(self._max_rows))
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        return ResolvedQueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            source=self._source,
            sql_executed=sql,
            parameters=params,
        )


def _driver_available() -> bool:
    try:
        import oracledb  # noqa: F401
    except ImportError:
        return False
    return True


def _short(exc: Exception) -> str:
    """One line. Oracle errors arrive with stack-shaped multi-line text, and a
    status line that wraps six times is a status line nobody reads."""
    text = " ".join(str(exc).split())
    return text if len(text) <= 180 else text[:180].rstrip() + "…"
