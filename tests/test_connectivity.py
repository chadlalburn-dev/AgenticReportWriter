"""What a data source can do from here, and the refusal to overstate it.

The failure this guards against is quiet. A source that is configured but
unreachable produces a run with empty sections and no obvious cause, and the
tempting shortcut — treating "we did not check" as "probably fine" — turns that
into a report someone trusts. So `reachable` has three states and the third one
has to survive every hop from the executor to the page.
"""

from __future__ import annotations

import pytest

from services.api_integration.sharepoint import (
    ENV_CLIENT,
    ENV_SECRET,
    ENV_TENANT,
    MockSharePointConnector,
    SharePointConnector,
)
from services.data_integration.oracle_executor import (
    ENV_DSN,
    ENV_PASSWORD,
    ENV_USER,
    OracleQueryExecutor,
)
from shared.connectivity import ConnectorStatus, unchecked, unconfigured


# --- unknown is not a synonym for working ----------------------------------


def test_an_unprobed_source_is_not_usable():
    """The whole point. `usable` is `reachable is True`, not
    `reachable is not False` — treating unknown as fine is how an unreachable
    warehouse becomes four empty sections in a safety summary."""
    status = unchecked("warehouse", "bigquery")
    assert status.configured is True
    assert status.reachable is None
    assert status.usable is False


def test_unknown_reads_as_neither_good_nor_bad():
    """It must not render as a tick or a cross. An unprobed source shown as
    working is the same species of error as placeholder prose behind a real
    provenance claim."""
    status = unchecked("warehouse", "bigquery")
    assert status.label == "Not checked"
    assert status.state_class == "neutral"


def test_a_missing_credential_is_a_definite_negative_not_an_unknown():
    """Caught in the rendered page: a first version of this mapping lived as a
    ternary in the template and showed "Not configured" as neutral, reading a
    fixable mistake as an open question."""
    status = unconfigured("lims", "oracle", ("REPORTGEN_ORACLE_DSN",))
    assert status.label == "Not configured"
    assert status.state_class == "bad"
    assert status.usable is False


def test_the_state_mapping_has_exactly_one_definition():
    """Every state routes through `state_class`, so a template cannot grow a
    second opinion about what a status means."""
    cases = {
        (False, None): "bad",       # not configured
        (True, None): "neutral",    # configured, unprobed
        (True, True): "ok",         # probed, answered
        (True, False): "bad",       # probed, failed
    }
    for (configured, reachable), expected in cases.items():
        status = ConnectorStatus(
            connector_id="x",
            kind="bigquery",
            configured=configured,
            reachable=reachable,
            detail="",
        )
        assert status.state_class == expected, (configured, reachable)


# --- the detail has to be actionable ---------------------------------------


def test_an_unconfigured_source_names_what_is_missing(monkeypatch):
    """"Not configured" alone sends someone hunting. The variable names are the
    difference between a fixable message and a puzzle."""
    for var in (ENV_DSN, ENV_USER, ENV_PASSWORD):
        monkeypatch.delenv(var, raising=False)

    # Password mode, because that is the mode with a username and a password to
    # be missing. Kerberos has neither, and asking it for them would report a
    # correctly configured service as broken — which is its own test below.
    status = OracleQueryExecutor(service="LIMSPRD", auth_mode="password").status()
    assert status.usable is False
    for var in (ENV_DSN, ENV_USER, ENV_PASSWORD):
        assert var in status.detail
        assert var in status.missing


def test_kerberos_needs_no_credential_to_be_configured(monkeypatch):
    """The reason the mode exists. An external-auth connection stores nothing,
    so counting a missing password against it would make a correct service
    permanently unconfigurable — no amount of filling that field in would help,
    because the field does not apply."""
    monkeypatch.setenv(ENV_DSN, "host:1521/SVC")
    for var in (ENV_USER, ENV_PASSWORD):
        monkeypatch.delenv(var, raising=False)

    status = OracleQueryExecutor(service="LIMSPRD", auth_mode="kerberos").status()
    assert ENV_USER not in status.missing
    assert ENV_PASSWORD not in status.missing


def test_wallet_mode_asks_for_the_wallet_and_nothing_else(monkeypatch):
    monkeypatch.setenv(ENV_DSN, "host:1521/SVC")
    without = OracleQueryExecutor(service="LIMSPRD", auth_mode="wallet").status()
    assert any("wallet" in m.lower() for m in without.missing), without.missing

    with_dir = OracleQueryExecutor(
        service="LIMSPRD", auth_mode="wallet", wallet_dir="/opt/wallet"
    ).status()
    assert not any("wallet" in m.lower() for m in with_dir.missing)


def test_sharepoint_says_what_it_would_take(monkeypatch):
    """At GSK the answer is a request to the platform team, not a setting. Worth
    saying, because otherwise someone spends an afternoon looking for the
    setting."""
    for var in (ENV_TENANT, ENV_CLIENT, ENV_SECRET):
        monkeypatch.delenv(var, raising=False)

    status = SharePointConnector().status()
    assert status.usable is False
    assert "app registration" in status.detail.lower()
    assert set(status.missing) == {ENV_TENANT, ENV_CLIENT, ENV_SECRET}


def test_status_never_opens_a_connection(monkeypatch):
    """A page render that probes a warehouse is the mistake that made every page
    in this app take sixteen seconds. `status()` answers from configuration
    only; `probe()` is the one that goes out, and only when a human asks."""
    monkeypatch.setenv(ENV_DSN, "host:1521/SVC")
    monkeypatch.setenv(ENV_USER, "reader")
    monkeypatch.setenv(ENV_PASSWORD, "secret")

    executor = OracleQueryExecutor(service="LIMSPRD")

    def explode(*a, **k):
        raise AssertionError("status() opened a connection")

    monkeypatch.setattr(executor, "_connect", explode)
    status = executor.status()
    assert status.reachable is None, "status() claimed to know about reachability"


def test_a_credential_never_appears_in_the_status(monkeypatch):
    """This text goes on a page. A password reaching it would be a leak through
    the very surface built to explain the failure."""
    monkeypatch.setenv(ENV_DSN, "host:1521/SVC")
    monkeypatch.setenv(ENV_USER, "reader")
    monkeypatch.setenv(ENV_PASSWORD, "hunter2-do-not-render")

    status = OracleQueryExecutor(service="LIMSPRD").status()
    assert "hunter2" not in status.detail
    assert "hunter2" not in " ".join(status.missing)


# --- the offline fixtures say they are fixtures ----------------------------


def test_the_mock_admits_it_is_a_fixture():
    """A reader comparing two reports needs to know whether the evidence came
    from the real system. "Reachable" with no further detail would let a
    fixture pass for SharePoint."""
    status = MockSharePointConnector().status()
    assert status.usable is True
    assert "fixture" in status.detail.lower()
    assert "nothing leaves this machine" in status.detail.lower()


# --- the mock behaves like the thing it stands in for ----------------------


def test_the_file_type_filter_actually_filters():
    connector = MockSharePointConnector()
    decks = connector.call("search_files", {"file_types": "pptx"})
    assert decks.row_count == 2
    assert all(str(r[1]).endswith(".pptx") for r in decks.rows)


def test_no_file_type_filter_means_everything():
    """Empty is a real choice, not a mistake — but it pulls every document in
    scope, which is why the editor warns about it."""
    everything = MockSharePointConnector().call("search_files", {})
    assert everything.row_count == 3


def test_results_are_ordered_deterministically():
    """A report that reorders its own evidence between two runs of the same
    template is a report whose citation numbers move, and moving citation
    numbers are worse than useless in review."""
    connector = MockSharePointConnector()
    first = connector.call("search_files", {})
    second = connector.call("search_files", {})
    assert first.rows == second.rows


def test_an_unknown_operation_is_refused():
    """The gate authorises per operation, so an unlisted one must not slip
    through as a no-op that returns nothing."""
    from services.api_integration.connector import ApiOperationError

    with pytest.raises(ApiOperationError):
        MockSharePointConnector().call("delete_everything", {})


def test_the_real_connector_refuses_before_it_reaches_the_network(monkeypatch):
    """An unconfigured call must fail with the sentence naming what is missing,
    not a traceback four frames deep in urllib — because this message is what
    reaches the source ledger and then the page."""
    from services.api_integration.connector import ApiOperationError

    for var in (ENV_TENANT, ENV_CLIENT, ENV_SECRET):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(ApiOperationError) as caught:
        SharePointConnector().call("search_files", {"site": "X"})
    assert "not configured" in str(caught.value).lower()
    assert ENV_TENANT in str(caught.value)


# --- proxy and TLS ---------------------------------------------------------


def test_a_connection_can_go_through_a_proxy_and_trust_its_ca():
    """Not polish. GSK's proxy terminates TLS and presents its own certificate
    authority, which is why `npm install` in this repo fails with
    SELF_SIGNED_CERT_IN_CHAIN until --use-system-ca is set. Every connector here
    called urllib directly, so each met that wall on first contact."""
    from shared.http_transport import HttpTransport

    transport = HttpTransport.from_settings(
        {"proxy_url": "http://proxy.example:8080", "timeout_s": "45"}
    )
    names = {h.__class__.__name__ for h in transport.opener().handlers}
    assert "ProxyHandler" in names
    assert "HTTPSHandler" in names
    assert transport.timeout_s == 45.0


def test_going_direct_does_not_inherit_an_ambient_proxy(monkeypatch):
    """A connection that says "go direct" should go direct. Omitting the handler
    would let urllib fall back to http_proxy from the environment — an ambient
    setting the connection never mentioned."""
    from shared.http_transport import HttpTransport

    monkeypatch.setenv("http_proxy", "http://ambient.example:3128")

    # Absence is the proof here, and it is worth spelling out because the
    # behaviour is inverted from the obvious reading.
    #
    # `build_opener` installs a default ProxyHandler that reads http_proxy from
    # the environment — unless you pass one yourself. A ProxyHandler built from
    # an empty dict registers no `*_open` methods, so it never appears in
    # `handlers`; what it does is suppress the default. So no ProxyHandler in
    # the list means no proxying at all, which is what "go direct" has to mean.
    direct = [
        h.__class__.__name__
        for h in HttpTransport.from_settings({}).opener().handlers
    ]
    assert "ProxyHandler" not in direct, (
        "a ProxyHandler is installed, so urllib will read http_proxy from the "
        "environment — an ambient setting this connection never mentioned"
    )

    # And the control: a connection that names a proxy does get one.
    via = [
        h.__class__.__name__
        for h in HttpTransport.from_settings(
            {"proxy_url": "http://named.example:8080"}
        ).opener().handlers
    ]
    assert "ProxyHandler" in via


def test_there_is_no_way_to_switch_certificate_checking_off():
    """Adding one would undo the point. It makes the proxy error disappear while
    leaving every request open to whatever terminated it, in an application that
    reads preclinical data — the same reason `npm config set strict-ssl false`
    was rejected earlier in this project."""
    import inspect

    from shared import http_transport

    source = inspect.getsource(http_transport)
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
    assert "_create_unverified" not in source


def test_a_malformed_timeout_falls_back_rather_than_failing():
    """A typo in a timeout should not take a connection down; the documented
    default is the safe reading of "I did not mean to change this"."""
    from shared.http_transport import DEFAULT_TIMEOUT_S, HttpTransport

    assert HttpTransport.from_settings({"timeout_s": "soon"}).timeout_s == DEFAULT_TIMEOUT_S
