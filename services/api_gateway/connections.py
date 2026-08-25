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
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shared.connectivity import ConnectorStatus, unchecked, unconfigured

CONNECTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")

@dataclass(frozen=True)
class SettingField:
    """One setting on a connection form.

    `only_when` is what lets a kind have variants without a second kind. An
    Oracle service authenticating with Kerberos has no username and no password
    — asking for them would be asking for something that does not exist, and
    marking them required would make a correct connection unsaveable.
    """

    name: str
    label: str
    required: bool = True
    control: str = "text"  # "text" | "choice"
    choices: tuple[tuple[str, str], ...] = ()
    #: (field, value) — this setting applies only when that field holds that
    #: value. Evaluated against the connection's own settings.
    only_when: tuple[str, str] | None = None
    hint: str = ""

    def applies(self, settings: Mapping[str, str], default_mode: str = "") -> bool:
        if self.only_when is None:
            return True
        field, value = self.only_when
        current = str(settings.get(field, "") or default_mode)
        return current == value


#: Proxy and CA, on every kind that makes an HTTPS call.
#:
#: Not optional polish. GSK's TLS-inspecting proxy presents its own CA, which is
#: why `npm install` in this repo fails with SELF_SIGNED_CERT_IN_CHAIN until
#: `--use-system-ca` is set — and why the SSH remote is the only working git
#: path. Every connector here calls urllib with no proxy and no CA bundle, so
#: each one meets that same wall on first contact with a real endpoint. Per
#: connection rather than global because a warehouse behind the proxy and an
#: internal host that bypasses it are both normal.
_NETWORK_FIELDS: tuple[SettingField, ...] = (
    SettingField(
        "proxy_url",
        "Proxy URL",
        required=False,
        hint="Leave blank to go direct. e.g. http://proxy.gsk.com:8080",
    ),
    SettingField(
        "ca_bundle",
        "CA bundle path",
        required=False,
        hint=(
            "A PEM file for the proxy's own certificate authority. Leave blank "
            "to use the operating system's trust store."
        ),
    ),
    SettingField(
        "timeout_s",
        "Timeout (seconds)",
        required=False,
        hint="Blank means 30.",
    ),
)

ORACLE_AUTH_MODES: tuple[tuple[str, str], ...] = (
    ("kerberos", "Kerberos / external authentication"),
    ("wallet", "Wallet (mTLS)"),
    ("password", "Username and password"),
)

#: The default when a connection does not say. Kerberos first because it stores
#: no credential at all, which is the outcome this module is arranged around —
#: and because a corporate Oracle estate is far likelier to use external
#: authentication than a username this app would have to hold.
DEFAULT_ORACLE_AUTH = "kerberos"

KIND_FIELDS: dict[str, tuple[SettingField, ...]] = {
    # GSK authenticates to GCP with Google SSO, so Application Default
    # Credentials is not a fallback here — it IS the sign-in. `gcloud auth
    # application-default login` runs the SSO flow and leaves short-lived
    # credentials the client picks up; in Cloud Run, workload identity does the
    # same with no login at all.
    #
    # There is deliberately no field for a service-account key file. A key is a
    # long-lived credential that outlives the person who made it, which is the
    # onboarding and offboarding problem GSK already has with API keys — and it
    # is the reason the CLI engine in this repo refuses one too.
    "bigquery": (
        SettingField("project", "GCP project"),
        SettingField(
            "auth_mode",
            "How it authenticates",
            control="choice",
            choices=(
                ("adc", "Google SSO (application-default credentials)"),
                ("impersonate", "Google SSO, then impersonate a service account"),
                ("workload_identity", "Workload identity (running in GCP)"),
            ),
            hint=(
                "Run `gcloud auth application-default login` once for the "
                "first two. No key file is ever read."
            ),
        ),
        SettingField(
            "impersonate_sa",
            "Service account to impersonate",
            only_when=("auth_mode", "impersonate"),
            hint="e.g. reportgen-reader@my-project.iam.gserviceaccount.com",
        ),
        SettingField("dataset", "Default dataset", required=False),
        SettingField("location", "Location", required=False, hint="Blank means EU."),
    )
    + _NETWORK_FIELDS,
    "oracle": (
        SettingField("service", "Service or schema"),
        SettingField(
            "auth_mode",
            "How it authenticates",
            control="choice",
            choices=ORACLE_AUTH_MODES,
            hint="Changing this changes which settings below apply.",
        ),
        SettingField(
            "dsn_env",
            "Env var holding the DSN",
            hint="e.g. LIMS_PROD_DSN. The name, not the value.",
        ),
        SettingField(
            "wallet_dir",
            "Wallet directory (TNS_ADMIN)",
            only_when=("auth_mode", "wallet"),
            hint="The directory holding tnsnames.ora and the wallet files.",
        ),
        SettingField(
            "user_env", "Env var holding the username", only_when=("auth_mode", "password")
        ),
        SettingField(
            "password_env",
            "Env var holding the password",
            only_when=("auth_mode", "password"),
        ),
    ),
    "sharepoint": (
        SettingField("site", "Default site or drive", required=False),
        SettingField(
            "auth_mode",
            "How it authenticates",
            control="choice",
            choices=(
                ("certificate", "Certificate"),
                ("secret", "Client secret"),
            ),
        ),
        SettingField("tenant_env", "Env var holding the tenant id"),
        SettingField("client_env", "Env var holding the client id"),
        SettingField(
            "secret_env",
            "Env var holding the client secret",
            only_when=("auth_mode", "secret"),
        ),
        SettingField(
            "cert_path",
            "Certificate path (PEM)",
            only_when=("auth_mode", "certificate"),
        ),
        SettingField(
            "cert_thumbprint_env",
            "Env var holding the certificate thumbprint",
            only_when=("auth_mode", "certificate"),
        ),
    )
    + _NETWORK_FIELDS,
    "confluence": (
        SettingField("base_url", "Base URL"),
        SettingField(
            "auth_scheme",
            "Authorization scheme",
            control="choice",
            choices=(("Bearer", "Bearer (personal access token)"), ("Basic", "Basic")),
            hint="The connector sends this verbatim in the Authorization header.",
        ),
        SettingField("token_env", "Env var holding the API token"),
    )
    + _NETWORK_FIELDS,
}

#: The default choice for any `control="choice"` field, so a form rendered
#: before anything is typed still describes a valid connection.
DEFAULT_CHOICE = {
    ("bigquery", "auth_mode"): "adc",
    ("oracle", "auth_mode"): DEFAULT_ORACLE_AUTH,
    ("sharepoint", "auth_mode"): "certificate",
    ("confluence", "auth_scheme"): "Bearer",
}


def default_settings(kind: str) -> dict[str, str]:
    return {
        field.name: DEFAULT_CHOICE.get((kind, field.name), "")
        for field in KIND_FIELDS.get(kind, ())
        if field.control == "choice"
    }


def fields_for(kind: str, settings: Mapping[str, str] | None = None) -> list[SettingField]:
    """The settings that apply to this connection as configured."""
    current = dict(settings or {})
    for (k, name), value in DEFAULT_CHOICE.items():
        if k == kind:
            current.setdefault(name, value)
    return [f for f in KIND_FIELDS.get(kind, ()) if f.applies(current)]

#: Fields whose value is the NAME of an environment variable, derived from
#: the table above rather than listed twice. A second hand-kept list is how
#: a new credential field arrives without the check that stops someone
#: pasting the secret itself.
ENV_FIELDS = frozenset(
    f.name
    for fields in KIND_FIELDS.values()
    for f in fields
    if f.name.endswith('_env')
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
        """Required settings with nothing in them, for this auth mode.

        `fields_for` rather than the whole table: a Kerberos Oracle connection
        has no username, so counting one as missing would make a correct
        connection permanently unsaveable.
        """
        return tuple(
            f.name
            for f in fields_for(self.kind, self.settings)
            if f.required and not str(self.settings.get(f.name, "")).strip()
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

    for f in fields_for(conn.kind, conn.settings):
        name, label, required = f.name, f.label, f.required
        value = str(conn.settings.get(name, "")).strip()
        if required and not value:
            problems.append(f"{label} is required.")
        if f.control == "choice" and value and value not in {c for c, _ in f.choices}:
            problems.append(
                f"{label}: {value!r} is not one of "
                f"{', '.join(c for c, _ in f.choices)}."
            )
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
