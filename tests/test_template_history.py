"""Template version history, and the fact that restoring is reversible.

The history already existed and nothing showed it: `backup_template` has been
copying every save into `var/template-backups/<key>/<stamp>.md` since long
before this file. What was missing was any way to see what a template used to
say, or to put it back.

So this reads those files rather than adding a second store. A parallel history
would be a second thing to keep correct, and the one that already runs on every
save is the one that is actually true.
"""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.runs import (
    RunStore,
    template_version_text,
    template_versions,
)
from services.template_service.report_doc_writer import blank_draft

FORM = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture
def store(tmp_path) -> RunStore:
    templates = tmp_path / "report-templates"
    templates.mkdir()
    return RunStore(
        root=tmp_path / "var" / "runs",
        templates_dir=templates,
        backups_dir=tmp_path / "var" / "template-backups",
    )


def _draft(key: str, instruction: str):
    draft = blank_draft(report_type=key)
    draft.title = key.replace("_", " ").title()
    draft.version = "0.1.0"
    draft.description = "Fixture."
    draft.owner = "test-team"
    draft.inputs[0].id = "compound_id"
    draft.inputs[0].prompt = "Compound"
    draft.sources.clear()
    draft.sections[0].heading = "Only section"
    draft.sections[0].instruction = instruction
    draft.sections[0].source_keys = []
    draft.sections[0].table_key = ""
    return draft


# --- reading what is already on disk ---------------------------------------


def test_the_live_file_is_listed_and_marked(store: RunStore, tmp_path):
    """"What does it say now" is the first thing someone comparing versions
    needs, and the least convenient thing to have to find elsewhere."""
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")

    versions = template_versions(
        "probe",
        backups_dir=tmp_path / "var" / "template-backups",
        current=store.template_path("probe"),
    )
    assert versions[0].is_current
    assert versions[0].when_human == "now"
    assert versions[0].version == "0.1.0"


def test_each_save_adds_a_version(store: RunStore, tmp_path):
    backups = tmp_path / "var" / "template-backups"
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")
    before = len(template_versions("probe", backups_dir=backups))

    store.save_draft(_draft("probe", "Second."), create=False)
    after = template_versions("probe", backups_dir=backups)

    assert len(after) == before + 1
    # The backup holds the state BEFORE the save, which is the whole point.
    assert "First." in template_version_text(
        "probe", after[0].handle, backups_dir=backups
    )


def test_a_template_with_no_history_lists_only_itself(store: RunStore, tmp_path):
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")
    versions = template_versions(
        "probe",
        backups_dir=tmp_path / "var" / "template-backups",
        current=store.template_path("probe"),
    )
    assert [v.is_current for v in versions] == [True]


# --- a filename from a URL is where a traversal lives ----------------------


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "..\\..\\win.ini", "", "not-a-stamp", "20260825T140116Z/../x"],
)
def test_a_version_handle_is_matched_not_trusted(tmp_path, hostile: str):
    """The handle comes off a URL and becomes a filename. `..` does not match
    eight digits, a T, six digits and a Z."""
    with pytest.raises(KeyError):
        template_version_text("probe", hostile, backups_dir=tmp_path)


# --- restoring -------------------------------------------------------------


@pytest.fixture
def client(store, monkeypatch) -> TestClient:
    from services.api_gateway import runs as runs_module
    from services.api_gateway.main import app

    monkeypatch.setattr(runs_module, "_STORE", store)
    monkeypatch.setattr(
        runs_module, "TEMPLATE_BACKUPS_DIR", store.backups_dir
    )
    return TestClient(app)


def test_restoring_puts_the_old_text_back(store: RunStore, client: TestClient):
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")
    store.save_draft(_draft("probe", "Second."), create=False)
    assert "Second." in store.template_path("probe").read_text(encoding="utf-8")

    versions = template_versions("probe", backups_dir=store.backups_dir)
    oldest = versions[-1]
    response = client.post(
        f"/templates/probe/history/{oldest.handle}/restore", follow_redirects=False
    )
    assert response.status_code == 303
    assert "First." in store.template_path("probe").read_text(encoding="utf-8")


def test_a_restore_is_itself_reversible(store: RunStore, client: TestClient):
    """The difference between a history and a trapdoor. Restoring backs up the
    current state first, so the thing you just replaced is still there."""
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")
    store.save_draft(_draft("probe", "Second."), create=False)

    oldest = template_versions("probe", backups_dir=store.backups_dir)[-1]
    client.post(f"/templates/probe/history/{oldest.handle}/restore")

    texts = [
        template_version_text("probe", v.handle, backups_dir=store.backups_dir)
        for v in template_versions("probe", backups_dir=store.backups_dir)
    ]
    assert any("Second." in t for t in texts), (
        "restoring discarded the state it replaced"
    )


def test_restore_is_a_post_not_a_link(store: RunStore, client: TestClient):
    """A crawler, a prefetch or a stray middle-click must not be able to
    overwrite a template. The whole point of a history is that nothing in it
    changes by accident."""
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")
    store.save_draft(_draft("probe", "Second."), create=False)
    oldest = template_versions("probe", backups_dir=store.backups_dir)[-1]

    got = client.get(f"/templates/probe/history/{oldest.handle}/restore")
    assert got.status_code in (404, 405), (
        "restore answered a GET; a link could then overwrite a template"
    )


def test_viewing_a_version_returns_the_file_not_a_page(
    store: RunStore, client: TestClient
):
    """The point of looking at an old version is seeing exactly what it said."""
    store.save_draft(_draft("probe", "First."), create=True, scope="universal")
    store.save_draft(_draft("probe", "Second."), create=False)
    oldest = template_versions("probe", backups_dir=store.backups_dir)[-1]

    got = client.get(f"/templates/probe/history/{oldest.handle}")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("text/plain")
    assert "First." in got.text
