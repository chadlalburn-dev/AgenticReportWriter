"""What a data source can actually do from here, said plainly.

Why this exists
---------------
The app can now be pointed at BigQuery, Oracle, SharePoint and Confluence. On
this machine it can reach none of them: GSK's VPC-SC perimeter blocks
self-service access, the repo never uses API keys, and the corporate proxy
terminates TLS with its own CA. So a connector configured in the editor and a
connector that works are two different things, and the gap between them has to
be visible *before* someone commits a run to it — not discovered as a
half-drafted report with four empty sections.

The three states, and why there are three
-----------------------------------------
Two states would collapse the interesting case. "Working" and "broken" leaves
nowhere to put *we have not asked* — and that is the honest answer for most
sources most of the time, because probing a warehouse on every page render is
what made this app take sixteen seconds a click. So:

- `reachable=True`  — a probe ran and succeeded.
- `reachable=False` — a probe ran and failed. `detail` says how.
- `reachable=None`  — nobody asked. Not a synonym for either.

The rule the UI must follow is that `None` renders as "not checked", never as a
tick and never as a cross. An unprobed source shown as working is the same
species of error as placeholder prose behind a real provenance claim, and this
application does not get to make that mistake twice.

`configured` is separate from `reachable` on purpose. A source with no dataset
and no query is misconfigured, which the author can fix; a correctly configured
source behind a firewall is not their mistake and needs different words.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

#: Matches the source kinds a template can declare.
SourceKind = Literal["bigquery", "oracle", "confluence", "sharepoint", "file", "api"]


@dataclass(frozen=True)
class ConnectorStatus:
    """One source's answer to "can this be used from here, and how do you know?"."""

    connector_id: str
    kind: str

    #: Does it have the settings it needs? An authoring question.
    configured: bool

    #: True / False / None, where None means no probe was attempted. Never
    #: default this to a bool: the whole point is that "unknown" survives.
    reachable: bool | None

    #: One sentence a reader can act on. Names the missing thing when
    #: unconfigured, the failure when unreachable, and says so plainly when
    #: nothing was checked.
    detail: str

    #: What is missing, when `configured` is False. Empty otherwise.
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """Only a probed success counts.

        Deliberately not `reachable is not False`. Treating unknown as usable is
        how an unreachable warehouse becomes four empty sections in a
        nonclinical safety summary.
        """
        return self.configured and self.reachable is True

    @property
    def label(self) -> str:
        """The short phrase the UI shows. No state renders as a bare tick."""
        if not self.configured:
            return "Not configured"
        if self.reachable is True:
            return "Reachable"
        if self.reachable is False:
            return "Cannot be reached"
        return "Not checked"

    @property
    def state_class(self) -> str:
        """The Titanium `.ti-state--*` suffix: `ok`, `neutral` or `bad`.

        Here rather than as a ternary in the template, because the first version
        of that ternary rendered "Not configured" as neutral — reading a missing
        credential as an open question when it is a definite negative someone
        can fix. Two copies of a rule is how the copies disagree; this is the
        one copy.

        `neutral` is reserved for genuine unknown. It has to look like neither
        good news nor bad, because that is what it is.
        """
        return {"ok": "ok", "warn": "neutral", "error": "bad"}[self.tone]

    @property
    def tone(self) -> str:
        """`ok` / `warn` / `error`, for the status dot.

        Unknown is `warn`, not `ok`. It is a thing to resolve before a run, not
        a clean bill of health.
        """
        if not self.configured:
            return "error"
        if self.reachable is True:
            return "ok"
        if self.reachable is False:
            return "error"
        return "warn"


class Checkable(Protocol):
    """A connector or executor that can describe itself.

    `status()` must not perform network I/O — it answers from configuration
    alone and returns `reachable=None`. `probe()` is the one that goes out to
    the network, and it is only ever called when a human asks, because a probe
    on every page render is exactly the mistake that made this app take sixteen
    seconds a click.
    """

    def status(self) -> ConnectorStatus: ...

    def probe(self) -> ConnectorStatus: ...


def unconfigured(
    connector_id: str, kind: str, missing: tuple[str, ...], hint: str = ""
) -> ConnectorStatus:
    """The status for a source that cannot be used because it is incomplete."""
    listed = ", ".join(missing)
    detail = f"Missing {listed}." if missing else "Not configured."
    return ConnectorStatus(
        connector_id=connector_id,
        kind=kind,
        configured=False,
        reachable=None,
        detail=f"{detail} {hint}".strip(),
        missing=tuple(missing),
    )


def unchecked(connector_id: str, kind: str, detail: str = "") -> ConnectorStatus:
    """Configured, and nobody has asked whether it answers."""
    return ConnectorStatus(
        connector_id=connector_id,
        kind=kind,
        configured=True,
        reachable=None,
        detail=detail or "Configured. No connection test has been run from here.",
    )
