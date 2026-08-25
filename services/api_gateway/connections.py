"""Connections a person can add and edit in the app.

What is stored, and what is deliberately not
--------------------------------------------
Settings, and never a secret. A connection records where a system is and which
environment variable holds the password — the value itself stays in the
environment where it already lives.

That is not caution for its own sake. This file sits in a repository, gets
committed by accident, appears in a support bundle, and is read back onto a page
that anyone using the app can see. A DSN and a username belong there; a password
does not, and no amount of care about the form field changes that. The same rule
already governs the CLI engine, which refuses to use an API key.

So the form asks for `REPORTGEN_ORACLE_PASSWORD`, not for the password. Someone
who wants a working connection still has to set that variable — which is a real
extra step, and the honest one, because the alternative is a plaintext
credential store nobody asked for.

Where it goes
-------------
`var/connections.json`, beside the run store, because it is machine state rather
than source. It is not in `report-templates/`: a template names a source by id
and should not carry the environment's idea of where that source lives, or the
same template would stop working when someone else opens it.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shared.connectivity import ConnectorStatus, unchecked, unconfigured

CONNECTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")

#: Which settings each kind asks for, and which of those are required. The
#: editor renders from this rather than from hand-written markup per kind, so a
#: new kind cannot arrive with a form that forgets one of its fields.
KIND_FIELDS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "bigquery": (
        ("project", "GCP project", True),
        ("dataset", "Default dataset", False),
        ("location", "Location", False),
    ),
    "oracle": (
        ("service", "Service or schema", True),
        ("dsn_env", "Env var holding the DSN", True),
        ("user_env", "Env var holding the username", True),
        ("password_env", "Env var holding the password", True),
    ),
    "sharepoint": (
        ("site", "Default site or drive", False),
        ("tenant_env", "Env var holding the tenant id", True),
        ("client_env", "Env var holding the client id", True),
        ("secret_env", "Env var holding the client secret", True),
    ),
    "confluence": (
        ("base_url", "Base URL", True),
        ("token_env", "Env var holding the API token", True),
    ),
}

#: Fields whose value is the NAME of an environment variable. Rendered with a
#: different hint, and checked for looking like a variable rather than like a
#: secret — someone pasting the password straight in is the mistake this whole
#: module is arranged to prevent, so it is worth catching in the form.
ENV_FIELDS = frozenset(
    {"dsn_env", "user_env", "password_env", "tenant_env", "client_env", "secret_env", "token_env"}
)

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass
class Connection:
    """One configured place to read from."""

    id: str
    kind: str
    label: str = ""
    settings: dict[str, str] = field(default_factory=dict)

    def missing(self) -> tuple[str, ...]:
        """Required settings with nothing in them."""
        return tuple(
            name
            for name, _label, required in KIND_FIELDS.get(self.kind, ())
            if required and not str(self.settings.get(name, "")).strip()
        )

    def status(self) -> ConnectorStatus:
        """Configuration only. Never opens a connection — the same rule the
        executors follow, and for the same reason: this renders on a page."""
        missing = self.missing()
        if missing:
            return unconfigured(
                self.id,
                self.kind,
                missing,
                hint="Fill these in on the Connections page.",
            )
        return unchecked(
            self.id,
            self.kind,
            detail=(
                f"Configured. No connection test has been run from here — "
                f"reachability is only checked when you ask for it."
            ),
        )


def validate(conn: Connection, existing_ids: tuple[str, ...] = ()) -> list[str]:
    """Everything wrong with this connection, in sentences.

    Returned rather than raised so the form can show all of them at once. A
    form that reports one problem per submission makes fixing four problems
    into four round trips.
    """
    problems: list[str] = []
    if not CONNECTION_ID_RE.match(conn.id or ""):
        problems.append(
            "The id must start with a lowercase letter and use only lowercase "
            "letters, digits and underscores."
        )
    elif conn.id in existing_ids:
        problems.append(f"A connection called {conn.id!r} already exists.")
    if conn.kind not in KIND_FIELDS:
        problems.append(
            f"{conn.kind!r} is not a kind this app knows. "
            f"Choose one of: {', '.join(sorted(KIND_FIELDS))}."
        )
        return problems

    for name, label, required in KIND_FIELDS[conn.kind]:
        value = str(conn.settings.get(name, "")).strip()
        if required and not value:
            problems.append(f"{label} is required.")
        if name in ENV_FIELDS and value and not _ENV_NAME_RE.match(value):
            # The likeliest way to get this wrong is to paste the secret itself,
            # which would then be written to disk and rendered back onto a page.
            problems.append(
                f"{label} should be the NAME of an environment variable, in "
                f"capitals — for example REPORTGEN_ORACLE_PASSWORD. It looks "
                f"like a value was entered instead; this app never stores the "
                f"secret itself."
            )
    return problems


class ConnectionStore:
    """Reads and writes `connections.json`. Small, so it is read on demand."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[Connection]:
        """Every configured connection, or an empty list.

        A malformed file yields nothing rather than raising: the Connections
        page is where someone goes to fix a broken connection, so it is the one
        page that must not fall over because a connection is broken.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        out: list[Connection] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            settings = item.get("settings")
            out.append(
                Connection(
                    id=str(item["id"]),
                    kind=str(item.get("kind", "")),
                    label=str(item.get("label", "")),
                    settings={
                        str(k): str(v)
                        for k, v in (settings or {}).items()
                        if isinstance(settings, dict)
                    },
                )
            )
        return out

    def save_all(self, connections: list[Connection]) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = [asdict(c) for c in connections]
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def upsert(self, conn: Connection) -> None:
        existing = [c for c in self.load() if c.id != conn.id]
        self.save_all(existing + [conn])

    def remove(self, connection_id: str) -> bool:
        existing = self.load()
        kept = [c for c in existing if c.id != connection_id]
        if len(kept) == len(existing):
            return False
        self.save_all(kept)
        return True

    def get(self, connection_id: str) -> Connection | None:
        return next((c for c in self.load() if c.id == connection_id), None)


#: The last probe result per connection, for this process only.
#:
#: Deliberately in memory rather than in `connections.json`. A "reachable" that
#: survives a restart is a claim about a network that may have changed since —
#: and the whole vocabulary here rests on "not checked" meaning nobody checked.
#: Losing the result on restart is the honest failure: it reverts to unknown
#: rather than to a stale yes.
_LAST_PROBE: dict[str, ConnectorStatus] = {}
_PROBE_LOCK = threading.Lock()


def record_probe(connection_id: str, status: ConnectorStatus) -> None:
    with _PROBE_LOCK:
        _LAST_PROBE[connection_id] = status


def last_probe(connection_id: str) -> ConnectorStatus | None:
    with _PROBE_LOCK:
        return _LAST_PROBE.get(connection_id)


def forget_probe(connection_id: str) -> None:
    """Drop a remembered result.

    Called when a connection is edited or removed: a result obtained against
    the previous settings says nothing about the new ones, and showing it
    beside changed settings would be the page vouching for something it never
    tested.
    """
    with _PROBE_LOCK:
        _LAST_PROBE.pop(connection_id, None)


def effective_status(conn: Connection) -> ConnectorStatus:
    """What to show: the last probe if there is one, else configuration alone."""
    return last_probe(conn.id) or conn.status()
