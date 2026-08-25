"""Adding and editing a connection in the app.

The rule the whole design hangs on: settings are stored, secrets are not. A
connection records which environment variable holds the password, never the
password. That file gets committed by accident, lands in a support bundle, and
is read back onto a page anyone using the app can see — so the form asks for
`REPORTGEN_ORACLE_PASSWORD` and refuses what looks like a value.
"""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import connections as connections_module
from services.api_gateway.connections import (
    Connection,
    ConnectionStore,
    validate,
)

FORM = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A connection store in a temp dir, wired into the app's routes."""
    from services.api_gateway import ui as ui_module

    path = tmp_path / "connections.json"
    monkeypatch.setattr(ui_module, "CONNECTIONS_PATH", path)
    for cid in list(connections_module._LAST_PROBE):
        connections_module.forget_probe(cid)
    return ConnectionStore(path)


@pytest.fixture
def client(store) -> TestClient:
    from services.api_gateway.main import app

    return TestClient(app)


def post(client: TestClient, path: str, fields: list[tuple[str, str]]):
    """Form-encoded explicitly.

    Passing a list of tuples as `data=` looked right and silently sent an empty
    body, which made a working editor look completely broken — the handler saw
    zero form keys and reported the template as having no title and no sections.
    """
    return client.post(
        path,
        content=urllib.parse.urlencode(fields),
        headers=FORM,
        follow_redirects=False,
    )


#: A password-mode Oracle connection. `auth_mode` is stated rather than left to
#: the default: the default is now Kerberos, which needs no username at all, and
#: a fixture that relies on an unstated default tests whatever the default
#: happens to be that week.
ORACLE = [
    ("op", "save"),
    ("id", "lims_prod"),
    ("kind", "oracle"),
    ("label", "LIMS production"),
    ("service", "LIMSPRD"),
    ("auth_mode", "password"),
    ("dsn_env", "LIMS_PROD_DSN"),
    ("user_env", "LIMS_PROD_USER"),
    ("password_env", "LIMS_PROD_PW"),
]

#: Kerberos stores no credential at all, which is the point of offering it.
ORACLE_KERBEROS = [
    ("op", "save"),
    ("id", "lims_sso"),
    ("kind", "oracle"),
    ("service", "LIMSPRD"),
    ("auth_mode", "kerberos"),
    ("dsn_env", "LIMS_PROD_DSN"),
]


# --- the rule that matters -------------------------------------------------


def test_a_pasted_secret_is_refused():
    """The mistake the entire module is arranged to prevent. `hunter2` is not a
    plausible environment variable name, and accepting it would write a
    credential to disk and render it back onto a page."""
    conn = Connection(
        id="lims",
        kind="oracle",
        settings={
            "service": "LIMSPRD",
            "auth_mode": "password",
            "dsn_env": "host:1521/SVC",
            "user_env": "reader",
            "password_env": "hunter2",
        },
    )
    problems = validate(conn)
    assert len(problems) == 3, problems
    assert all("NAME of an environment variable" in p for p in problems)


def test_a_saved_connection_holds_no_credential(store, client, monkeypatch):
    """Even with the secret present in the environment, what lands on disk is
    the variable's name."""
    monkeypatch.setenv("LIMS_PROD_PW", "hunter2")
    assert post(client, "/connections", ORACLE).status_code == 303

    on_disk = store.path.read_text(encoding="utf-8")
    assert "LIMS_PROD_PW" in on_disk
    assert "hunter2" not in on_disk
    assert "hunter2" not in client.get("/connections").text


def test_every_problem_is_reported_at_once(store, client):
    """One problem per submission turns fixing four things into four round
    trips."""
    bad = [("op", "save"), ("id", "X!"), ("kind", "oracle")]
    response = post(client, "/connections", bad)
    assert response.status_code == 422
    body = response.text
    assert "must start with a lowercase letter" in body
    assert body.count("<li>") >= 3, "only some of the problems were reported"


# --- the round trip --------------------------------------------------------


def test_a_connection_can_be_added_edited_and_removed(store, client):
    assert post(client, "/connections", ORACLE).status_code == 303
    assert [c.id for c in store.load()] == ["lims_prod"]

    edited = [(k, v) for k, v in ORACLE if k != "service"]
    edited += [("service", "LIMSPRD2"), ("editing", "lims_prod")]
    assert post(client, "/connections", edited).status_code == 303
    assert store.get("lims_prod").settings["service"] == "LIMSPRD2"

    assert post(
        client, "/connections", [("op", "delete"), ("id", "lims_prod")]
    ).status_code == 303
    assert store.load() == []


def test_the_edit_form_is_prefilled(store, client):
    post(client, "/connections", ORACLE)
    page = client.get("/connections?edit=lims_prod").text
    assert "LIMS_PROD_DSN" in page
    assert "LIMSPRD" in page


def test_saving_an_edit_does_not_collide_with_itself(store, client):
    """An edit keeps its own id out of the taken list, or saving a connection
    unchanged reports that it already exists."""
    post(client, "/connections", ORACLE)
    again = ORACLE + [("editing", "lims_prod")]
    assert post(client, "/connections", again).status_code == 303


# --- what "tested" means ---------------------------------------------------


def test_an_untested_connection_says_so(store, client):
    """Not a tick. Reachability is only checked when someone asks, so anything
    else would be the page claiming a check it never ran."""
    post(client, "/connections", ORACLE)
    assert "Not checked" in client.get("/connections").text


def test_testing_a_connection_names_its_own_variables(store, client, monkeypatch):
    """The connection points at LIMS_PROD_DSN. Telling the reader to set
    REPORTGEN_ORACLE_DSN — the executor's default — would be instructions for a
    variable their connection does not use."""
    monkeypatch.delenv("LIMS_PROD_DSN", raising=False)
    post(client, "/connections", ORACLE)
    assert post(client, "/connections/lims_prod/test", []).status_code == 303

    # Scoped to this connection's own recorded result, not the whole page: the
    # "Not wired up here" section legitimately names REPORTGEN_ORACLE_DSN when
    # describing the unwired Oracle executor's defaults, and a page-wide
    # assertion confuses that with the instruction given for this connection.
    recorded = connections_module.last_probe("lims_prod")
    assert recorded is not None, "the Test button discarded its result"
    assert "LIMS_PROD_DSN" in recorded.detail
    assert "REPORTGEN_ORACLE_DSN" not in recorded.detail
    assert "LIMS_PROD_DSN" in client.get("/connections").text


def test_a_probe_result_does_not_outlive_the_settings_it_tested(store, client):
    """Editing a connection drops its remembered result. Showing a result
    obtained against the previous settings would be the page vouching for
    something it never tested."""
    post(client, "/connections", ORACLE)
    post(client, "/connections/lims_prod/test", [])
    assert connections_module.last_probe("lims_prod") is not None

    edited = ORACLE + [("editing", "lims_prod")]
    post(client, "/connections", edited)
    assert connections_module.last_probe("lims_prod") is None


def test_probe_results_are_not_persisted(store, client):
    """A "reachable" surviving a restart is a claim about a network that may
    have changed since. Reverting to unknown is the honest failure."""
    post(client, "/connections", ORACLE)
    post(client, "/connections/lims_prod/test", [])
    assert "reachable" not in store.path.read_text(encoding="utf-8").lower()


# --- resilience ------------------------------------------------------------


def test_a_broken_connections_file_does_not_break_the_page(store, client):
    """This page is where someone goes to fix a broken connection, so it is the
    one page that must not fall over because a connection is broken."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json at all", encoding="utf-8")
    assert client.get("/connections").status_code == 200


def test_every_kind_offers_a_complete_form(store, client):
    """The form renders from KIND_FIELDS rather than hand-written markup, so a
    new kind cannot arrive with a form that forgets one of its fields."""
    page = client.get("/connections").text
    for kind, fields in connections_module.KIND_FIELDS.items():
        for f in fields:
            assert f'name="{f.name}"' in page, f"{kind}: no input for {f.name}"


# --- built-ins are defaults, not fixtures forever --------------------------


def test_a_configured_connection_replaces_the_builtin(store, client):
    """What "configurable" has to mean. Otherwise the page records settings that
    nothing reads, and this application has already shipped one of those."""
    from services.api_gateway.runs import build_api_gate

    conn = connections_module.Connection(
        id="confluence",
        kind="confluence",
        settings={
            "base_url": "https://confluence.example",
            "auth_scheme": "Bearer",
            "token_env": "CONF_TOKEN",
        },
    )
    gate = build_api_gate([conn])
    built = gate._registry.get("confluence")
    assert type(built).__name__ == "ConfluenceConnector", (
        f"the gate still holds {type(built).__name__}; configuring did nothing"
    )


def test_without_a_configuration_the_builtin_still_answers(store):
    """The default has to keep working, or a laptop with no network loses the
    demo and every offline test with it."""
    from services.api_gateway.runs import build_api_gate

    gate = build_api_gate([])
    assert type(gate._registry.get("confluence")).__name__ == "MockConfluenceConnector"


def test_a_half_configured_connection_falls_back_rather_than_breaking(store):
    """A connection missing its settings must not take down the page that exists
    to fix it."""
    from services.api_gateway.runs import build_api_gate

    broken = connections_module.Connection(id="confluence", kind="confluence", settings={})
    gate = build_api_gate([broken])
    assert type(gate._registry.get("confluence")).__name__ == "MockConfluenceConnector"


def test_the_status_says_which_one_is_in_force(store):
    """A reader comparing two reports needs to know whether the evidence came
    from the real system. "Reachable" alone would let a fixture pass for
    Confluence."""
    from services.api_gateway.runs import connector_statuses

    plain = {s.connector_id: s for s in connector_statuses()}
    assert "fixture" in plain["confluence"].detail.lower()

    conn = connections_module.Connection(
        id="confluence",
        kind="confluence",
        settings={
            "base_url": "https://confluence.example",
            "auth_scheme": "Bearer",
            "token_env": "CONF_TOKEN",
        },
    )
    overridden = {s.connector_id: s for s in connector_statuses([conn])}
    assert "replacing the built-in" in overridden["confluence"].detail


# --- every row can be tested ----------------------------------------------


@pytest.mark.parametrize(
    "connector_id", ["confluence", "mock_chembl", "sharepoint", "local-sqlite"]
)
def test_every_connector_can_be_tested(store, client, connector_id: str):
    """A test that only works on the rows someone added leaves the ones that
    ship unverifiable."""
    response = post(client, f"/connections/{connector_id}/test", [])
    assert response.status_code == 303


def test_testing_the_local_database_opens_it(store):
    """Checked by opening the file, not by asserting it exists: an unreadable or
    truncated database passes the second test and fails the first."""
    from services.api_gateway.runs import probe_any

    result = probe_any("local-sqlite")
    assert result.reachable is True
    assert "tables" in result.detail


def test_testing_an_unknown_connector_is_a_404(store, client):
    assert post(client, "/connections/not-a-connector/test", []).status_code == 404


def test_a_test_probes_the_override_not_the_builtin(store):
    """The configured connection is the one a run would call. Testing the
    fixture while an override is in force would report on something the app is
    not going to use."""
    from services.api_gateway.runs import probe_any

    conn = connections_module.Connection(
        id="confluence", kind="confluence", settings={}
    )
    result = probe_any("confluence", [conn])
    assert "fixture" not in result.detail.lower(), result.detail


# --- the form is a disclosure ---------------------------------------------


def test_the_add_form_is_closed_by_default(store, client):
    """It is the tallest thing on the page and most visits are to read a status,
    not to add a source."""
    body = client.get("/connections").text
    assert '<details class="ti-disclose">' in body
    assert '<details class="ti-disclose" open>' not in body


def test_the_form_opens_when_a_save_was_refused(store, client):
    """Hiding the reason a save failed would be worse than the scrolling it
    saves — the same rule the editor's tabs follow."""
    response = post(client, "/connections", [("op", "save"), ("id", "X!"), ("kind", "oracle")])
    assert response.status_code == 422
    assert '<details class="ti-disclose" open>' in response.text


def test_configuring_a_builtin_opens_the_form_with_its_id(store, client):
    """A connection replaces a built-in only by carrying its id, so the link
    fixes it rather than leaving someone to retype it exactly."""
    body = client.get("/connections?add=confluence&kind=confluence").text
    assert '<details class="ti-disclose" open>' in body
    assert 'value="confluence"' in body
