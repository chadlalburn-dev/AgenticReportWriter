"""Universal and personal templates.

Two scopes: `universal` is the shared library everyone sees, `user` belongs to
one person and nobody else. The tests that matter here are the negative ones —
a template being visible is obvious the moment you look at the page, whereas a
template leaking to another user is invisible until it is embarrassing.

Note what this is NOT. `identity.py` resolves an identity and never verifies a
secret, on purpose: the app sits behind IAP in the real deployment and rolling
its own login would become a second, weaker source of truth. So scoping here is
*attribution*, not access control. It keeps people out of each other's way; it
does not defend against someone who sets REPORTGEN_USER. That distinction is
worth stating plainly rather than letting a reader assume the stronger claim.
"""

from __future__ import annotations

import pytest

from services.api_gateway.runs import RunStore, user_dir_slug
from services.template_service.report_doc_writer import blank_draft

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def store(tmp_path) -> RunStore:
    templates = tmp_path / "report-templates"
    templates.mkdir()
    return RunStore(root=tmp_path / "var" / "runs", templates_dir=templates)


def _make(store: RunStore, key: str, *, scope: str, user_id: str | None = None):
    """A template that passes validation, so these tests fail on scoping rather
    than on a missing field."""
    draft = blank_draft(report_type=key)
    draft.title = key.replace("_", " ").title()
    draft.version = "0.1.0"
    draft.description = f"Fixture template {key}."
    draft.owner = "test-team"
    draft.inputs[0].id = "compound_id"
    draft.inputs[0].prompt = "Compound"
    draft.sources.clear()
    draft.sections[0].heading = "Only section"
    draft.sections[0].instruction = "Write something factual."
    draft.sections[0].source_keys = []
    draft.sections[0].table_key = ""
    store.save_draft(draft, create=True, scope=scope, user_id=user_id)


# --- the isolation property ------------------------------------------------


def test_a_personal_template_is_invisible_to_everyone_else(store: RunStore):
    """The property the whole feature exists for."""
    _make(store, "alice_only", scope="user", user_id=ALICE)

    assert "alice_only" in store.template_keys(ALICE)
    assert "alice_only" not in store.template_keys(BOB)
    assert "alice_only" not in store.template_keys()
    assert not store.template_exists("alice_only", BOB)


def test_a_universal_template_is_visible_to_everyone(store: RunStore):
    _make(store, "shared_one", scope="universal")

    for who in (ALICE, BOB, None):
        assert "shared_one" in store.template_keys(who)


def test_one_user_cannot_open_another_user_s_template(store: RunStore):
    """`draft_for` is what the editor calls. Resolving across users would let
    anyone edit anyone's template by typing a key into the URL."""
    _make(store, "alice_only", scope="user", user_id=ALICE)

    assert store.draft_for("alice_only", ALICE) is not None
    with pytest.raises(KeyError):
        store.draft_for("alice_only", BOB)


def test_omitting_the_user_shows_only_the_shared_library(store: RunStore):
    """The safe default. A caller that forgets to pass a user gets a template
    the owner cannot find — annoying. The other direction would put someone's
    private template on a shared page."""
    _make(store, "alice_only", scope="user", user_id=ALICE)
    _make(store, "shared_one", scope="universal")

    assert store.template_keys() == ["shared_one"]


# --- listing ---------------------------------------------------------------


def test_the_listing_labels_which_scope_each_template_is_in(store: RunStore):
    """A reader should not have to infer "who else can see this" from a folder
    path they never see. It also changes what Edit means: on a shared template
    it changes the report everyone else gets."""
    _make(store, "shared_one", scope="universal")
    _make(store, "alice_only", scope="user", user_id=ALICE)

    runnable, unavailable = store.list_templates(ALICE)
    by_key = {c.key: c for c in runnable + unavailable}

    assert by_key["shared_one"].scope == "universal"
    assert by_key["alice_only"].scope == "user"
    assert by_key["alice_only"].owned_by == user_dir_slug(ALICE)
    assert by_key["shared_one"].owned_by == ""


def test_personal_templates_sort_ahead_of_shared_ones(store: RunStore):
    """Someone who just made one is looking for it, and ordering purely by
    title buries it among a dozen shared templates.

    This is `list_templates` order only. The gallery groups before it sorts, so
    on the page this ordering is invisible — which is why grouping by scope
    exists, and why that has its own test below rather than this one being left
    to imply something the page does not do."""
    _make(store, "aaa_shared", scope="universal")
    _make(store, "zzz_mine", scope="user", user_id=ALICE)

    runnable, unavailable = store.list_templates(ALICE)
    keys = [c.key for c in runnable + unavailable]
    assert keys.index("zzz_mine") < keys.index("aaa_shared")


def test_the_gallery_can_group_by_who_can_see_a_template(store: RunStore):
    """The page groups before it sorts, so a scope-aware sort never showed.
    Grouping is the affordance that actually surfaces the split, and "Just me"
    comes first because that is what someone grouping by it came for."""
    _make(store, "aaa_shared", scope="universal")
    _make(store, "zzz_mine", scope="user", user_id=ALICE)

    view = store.gallery_view(group="scope", user_id=ALICE)
    labels = [g.label for g in view.groups]
    assert labels == ["Just me", "Everyone"], labels
    assert [c.key for c in view.groups[0].cards] == ["zzz_mine"]
    assert [c.key for c in view.groups[1].cards] == ["aaa_shared"]


def test_grouping_by_scope_is_offered_in_the_ui(store: RunStore):
    _make(store, "shared_one", scope="universal")
    view = store.gallery_view(user_id=ALICE)
    assert ("scope", "Who can see it") in view.group_options


# --- saving does not move a template between scopes ------------------------


def test_saving_a_personal_template_keeps_it_personal(store: RunStore):
    """Otherwise pressing Save publishes a private draft to everyone, which is
    not a thing a Save button should be able to do."""
    _make(store, "alice_only", scope="user", user_id=ALICE)
    draft = store.draft_for("alice_only", ALICE)
    draft.sections[0].instruction = "Changed."
    store.save_draft(draft, create=False, user_id=ALICE)

    assert "alice_only" not in store.template_keys(BOB)
    assert "alice_only" in store.template_keys(ALICE)


def test_saving_a_shared_template_does_not_fork_it_into_a_personal_copy(
    store: RunStore,
):
    """The other direction. An edit to a shared template has to keep landing on
    the shared file, or one person's fix silently stops reaching anyone else."""
    _make(store, "shared_one", scope="universal")
    draft = store.draft_for("shared_one", ALICE)
    draft.sections[0].instruction = "Changed."
    store.save_draft(draft, create=False, user_id=ALICE)

    assert "shared_one" in store.template_keys(BOB)
    assert store.list_templates(ALICE)[0][0].scope == "universal"


def test_a_personal_template_needs_a_user(store: RunStore):
    with pytest.raises(ValueError, match="needs a user"):
        _make(store, "orphan", scope="user", user_id=None)


# --- keys are unique across scopes -----------------------------------------


def test_a_suggested_key_avoids_names_taken_in_either_scope(store: RunStore):
    """Keys are unique across both scopes rather than shadowing, and that is a
    provenance decision: a run record stores `template_key`, so if a personal
    and a universal template could share one, an existing report would no longer
    say which template produced it."""
    _make(store, "probe_copy", scope="user", user_id=ALICE)

    suggested = store.suggest_template_key("probe")
    assert suggested != "probe_copy", (
        "suggested a key already taken by a personal template; a run record "
        "would then be ambiguous about which template drafted it"
    )


# --- the user id becomes a path, so it is guarded --------------------------


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "..\\..\\windows", "a/b", "", "   ", "..", "."],
)
def test_a_user_id_cannot_escape_the_templates_directory(hostile: str):
    """A user id arrives from a proxy header or the OS and ends up as a path
    segment. That is precisely where a traversal would live."""
    slug = user_dir_slug(hostile)
    assert "/" not in slug and "\\" not in slug
    assert ".." not in slug
    assert slug and slug not in (".", "..")


def test_two_users_with_ids_differing_only_in_case_share_one_directory():
    """`resolve_user` lowercases, so the same human arriving through different
    headers attributes to one identity. The storage slug has to agree, or their
    templates split across two folders and half of them vanish."""
    assert user_dir_slug("Alice@Example.COM") == user_dir_slug("alice@example.com")


# --- a personal template has to be runnable, not just visible --------------


def test_the_owner_can_resolve_their_own_template_for_a_run(store: RunStore):
    """Found in the running app, not here: `/templates` listed Alice's own
    template and linked to `/new/<key>`, and that link 404ed for Alice.

    `get_template` accepted `user_id` and never passed it to the parse — a
    parameter taken and ignored, the same defect shape as the chart reading
    `rows` instead of `typed_rows`. Everything downstream of it (the run-setup
    page, preflight, the outline) inherited the universal-only view, so a
    personal template could be created and seen and never used.
    """
    _make(store, "alice_only", scope="user", user_id=ALICE)

    card = store.get_template("alice_only", ALICE)
    assert card.key == "alice_only"
    assert card.scope == "user"

    assert store.template_outline("alice_only", ALICE), "no sections resolved"
    assert store.preflight("alice_only", {}, None, ALICE) is not None

    with pytest.raises(KeyError):
        store.get_template("alice_only", BOB)


def test_a_run_resolves_its_template_from_the_record_not_an_ambient_user(
    store: RunStore,
):
    """Generation happens on a worker thread with no request. A run started from
    someone's personal template has to keep resolving it after they close the
    tab, so the run record's `owner` is the authority — the same discipline as
    reading `model_version` off the run rather than the ambient engine."""
    _make(store, "alice_only", scope="user", user_id=ALICE)

    record = store.create(
        "alice_only", {"compound_id": "XYZ-001"}, None, owner=ALICE
    )
    assert record.owner == ALICE

    # What the worker does: resolve by key, as the recorded owner.
    assert store._template_or_raise(record.template_key, record.owner) is not None
    with pytest.raises(KeyError):
        store._template_or_raise(record.template_key, BOB)
