"""One place that builds an HTTPS opener, so a proxy and a CA bundle apply.

Why this exists
---------------
Every connector in this repo called `urllib.request.urlopen` directly, which
uses no proxy and the default trust store. On a GSK laptop that fails, and not
subtly: the corporate proxy terminates TLS and presents its own certificate
authority. It is the same wall that makes `npm install` fail here with
`SELF_SIGNED_CERT_IN_CHAIN` until `--use-system-ca` is passed, and the same
reason SSH is the only working git remote.

So a connection can carry a proxy URL and a CA bundle path, and this is where
those stop being decoration.

What it deliberately does not offer
-----------------------------------
There is no "skip certificate verification" option, and adding one would undo
the point. Turning verification off makes the proxy error disappear while
leaving every request open to whatever is terminating it — in an application
that reads preclinical data. The fix for a corporate CA is to trust that CA,
which is what `ca_bundle` is for; `npm config set strict-ssl false` was rejected
for the same reason earlier in this project's history.
"""

from __future__ import annotations

import ssl
import urllib.request
from dataclasses import dataclass

#: Matches the default in `SettingField("timeout_s", ...)`. Stated once.
DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class HttpTransport:
    """Proxy, trust and timeout for one connection."""

    proxy_url: str = ""
    ca_bundle: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S

    @classmethod
    def from_settings(cls, settings: dict[str, str] | None) -> "HttpTransport":
        raw = dict(settings or {})
        try:
            timeout = float(str(raw.get("timeout_s", "") or DEFAULT_TIMEOUT_S))
        except ValueError:
            # A typo in a timeout should not take a connection down; the
            # documented default is the safe reading of "I did not mean to
            # change this".
            timeout = DEFAULT_TIMEOUT_S
        return cls(
            proxy_url=str(raw.get("proxy_url", "") or "").strip(),
            ca_bundle=str(raw.get("ca_bundle", "") or "").strip(),
            timeout_s=timeout,
        )

    def context(self) -> ssl.SSLContext:
        """A verifying TLS context, trusting the named CA when one is given.

        `create_default_context` verifies hostnames and certificates. Loading a
        bundle ADDS the corporate CA to that; it never replaces the checks.
        """
        ctx = ssl.create_default_context()
        if self.ca_bundle:
            ctx.load_verify_locations(cafile=self.ca_bundle)
        return ctx

    def opener(self) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(context=self.context())
        ]
        if self.proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": self.proxy_url, "https": self.proxy_url}
                )
            )
        else:
            # An empty ProxyHandler means "no proxy", which is different from
            # omitting it: urllib would otherwise fall back to the environment's
            # http_proxy, and a connection that says "go direct" should go
            # direct rather than inherit an ambient setting it never mentioned.
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def describe(self) -> str:
        """One clause for a status line."""
        via = f"via {self.proxy_url}" if self.proxy_url else "direct"
        trust = f"trusting {self.ca_bundle}" if self.ca_bundle else "system trust store"
        return f"{via}, {trust}"
