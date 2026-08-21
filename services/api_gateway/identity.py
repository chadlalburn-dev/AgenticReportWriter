"""Who is using the app.  OWNER: ENG-6.

## Attribution, not authentication — and why that is the right call here

A "My compounds / All compounds" split needs to know *who made each run*. That
is **attribution**. It does not need to *prove* who you are, which is
**authentication**. The two have very different costs and this app should only
pay for the first:

* This app runs **locally against local data**. An app-managed password store
  would protect nothing — anyone who can start the process already owns the
  machine and the data directory. It would be security theatre with a real
  maintenance and breach cost (hashing, reset flows, lockout, a credential
  table to leak).
* In the GSK deployment the api-gateway sits **behind IAP** (see
  docs/architecture-plan.md). Identity is the platform's job: IAP/SSO
  terminates auth and forwards a verified identity header. An app that rolled
  its own login would have to be *bypassed* to deploy there, and would become a
  second, weaker source of truth about who someone is.

So: this module resolves an identity, and never verifies a secret. The
resolution order is deliberate — most trustworthy first:

1. **Proxy-asserted identity** (IAP / OAuth2-proxy / SSO). Only consulted when
   `REPORTGEN_TRUST_PROXY_AUTH=1`, because a header is trivially forgeable by
   any client if nothing upstream is actually stripping and setting it. Opting
   in is how the deployment states "there IS a proxy in front of me".
2. **`REPORTGEN_USER`** — explicit override for local dev, demos and tests.
3. **OS user** — the honest local answer. One human, one machine, one identity.

When it ever needs to become real multi-user auth, the seam is
`resolve_user()`: replace step 1's header read with a verified JWT check
(IAP signs one) and nothing else in the app changes.
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from typing import Mapping

# Headers set by common identity-aware proxies, most specific first.
# IAP prefixes its values with "accounts.google.com:".
_PROXY_HEADERS = (
    "x-goog-authenticated-user-email",
    "x-auth-request-email",
    "x-forwarded-email",
    "x-goog-authenticated-user-id",
    "x-forwarded-user",
    "x-remote-user",
)

_TRUST_ENV = "REPORTGEN_TRUST_PROXY_AUTH"
_USER_ENV = "REPORTGEN_USER"

ANONYMOUS = "unknown"


@dataclass(frozen=True)
class User:
    """The current user. `user_id` is the stable attribution key."""

    user_id: str
    display_name: str
    source: str  # "proxy" | "env" | "os" | "fallback"

    @property
    def is_known(self) -> bool:
        return self.user_id != ANONYMOUS

    @property
    def initials(self) -> str:
        parts = [p for p in re.split(r"[.\s_-]+", self.display_name) if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


def _clean(value: str | None) -> str:
    """Normalise an identity string to a stable key.

    Strips IAP's `accounts.google.com:` prefix and lowercases, so the same human
    arriving via different headers attributes to one identity.
    """
    v = (value or "").strip()
    if not v:
        return ""
    if ":" in v and v.lower().startswith("accounts.google.com:"):
        v = v.split(":", 1)[1]
    return v.strip().lower()


def _display_from_id(user_id: str) -> str:
    """'chad.l.alburn@gsk.com' -> 'chad.l.alburn'. Keeps bare usernames as-is."""
    if not user_id or user_id == ANONYMOUS:
        return "unknown user"
    return user_id.split("@", 1)[0]


def trust_proxy_auth() -> bool:
    return os.environ.get(_TRUST_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def resolve_user(headers: Mapping[str, str] | None = None) -> User:
    """Resolve the current user. Never raises; falls back to a usable identity.

    `headers` is a case-insensitive mapping (Starlette's `request.headers` is).
    """
    # 1. Proxy-asserted — only when the deployment says a proxy is really there.
    if headers is not None and trust_proxy_auth():
        for name in _PROXY_HEADERS:
            uid = _clean(headers.get(name))
            if uid:
                return User(uid, _display_from_id(uid), "proxy")

    # 2. Explicit override (local dev / demo / tests).
    uid = _clean(os.environ.get(_USER_ENV))
    if uid:
        return User(uid, _display_from_id(uid), "env")

    # 3. The honest local answer.
    try:
        uid = _clean(getpass.getuser())
        if uid:
            return User(uid, _display_from_id(uid), "os")
    except Exception:
        pass

    return User(ANONYMOUS, "unknown user", "fallback")
