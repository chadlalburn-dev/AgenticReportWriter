"""In-app template authoring: taxonomy, serializer round-trip, CRUD, gallery.

Everything here is offline and touches the network not at all. Two safety
properties are enforced by the fixtures rather than trusted:

  * `_the_real_corpus_is_sacred` hashes `report-templates/` before and after
    this module runs. Any test that writes there fails the module, not just
    itself. Round-trip work goes through `tmp_path`.
  * `store` re-points the process-wide `RunStore` singleton at a throwaway
    directory, so every route test creates, edits and deletes templates that
    only exist for the duration of one test.

The centre of gravity is the round-trip invariant: `load -> serialize -> load`
must be a fixed point for every file in the corpus. Without a serializer there
is no in-app authoring, and a serializer that loses a source parameter loses
the citation trail that the whole product exists to provide.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import time
import urllib.parse
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app
from services.template_service.report_doc import ReportDocError, load_report_doc
from services.template_service.report_doc_writer import (
    SCRATCH_DIR,
    DraftInput,
    DraftSection,
    DraftSource,
    TemplateDraft,
    TemplateWriteError,
    draft_from_path,
    draft_from_text,
    serialize_draft,
    write_template,
)
from services.template_service.taxonomy import (
    TAXONOMY_PATH,
    UNTAGGED,
    Taxonomy,
    load_taxonomy,
)
from shared.schemas.template import FreeTextInputBinding

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "report-templates"

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}

RUN_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# the corpus, split by whether it is actually a report template
# ---------------------------------------------------------------------------


def _loads(path: Path) -> bool:
    try:
        load_report_doc(path)
    except Exception:  # noqa: BLE001 - "does it load" is the whole question
        return False
    return True


#: Every `.md` in the template folder, whether or not it is a template.
ALL_MARKDOWN = sorted(TEMPLATES_DIR.glob("*.md"))

#: The ones that are real report templates. Globbed, not named: a template a
#: user authors through the editor has to obey the same invariants as a
#: shipped one, and hard-coding six names would quietly stop testing that.
LOADABLE = [p for p in ALL_MARKDOWN if _loads(p)]

#: Deliberately NOT templates. They live in the folder as documentation and
#: scaffolding, and the gallery is expected to list them as unavailable.
NOT_TEMPLATES = ("README.md", "SKILL.template.md")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _corpus_digest() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(TEMPLATES_DIR.iterdir())
        if p.is_file()
    }


@pytest.fixture(scope="module", autouse=True)
def _the_real_corpus_is_sacred():
    """No test in this module may add to, remove from or edit the real folder.

    This is asserted rather than assumed because the failure mode — a test
    that quietly rewrites a shipped template — is invisible until someone
    reads a diff.
    """
    before = _corpus_digest()
    yield
    after = _corpus_digest()
    assert after == before, (
        "report-templates/ changed while this module ran. "
        f"added={sorted(set(after) - set(before))} "
        f"removed={sorted(set(before) - set(after))} "
        f"edited={sorted(k for k in set(before) & set(after) if before[k] != after[k])}"
    )


@pytest.fixture()
def taxonomy() -> Taxonomy:
    """The shipped taxonomy, read fresh from disk rather than from the cache."""
    return load_taxonomy(TAXONOMY_PATH, refresh=True)


@pytest.fixture()
def config() -> dict:
    """The raw YAML, so "as configured" is checked against the file itself and
    not against the parser's own opinion of it."""
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def store(tmp_path: Path):
    """The app's store, re-pointed at a throwaway templates/backups/trash dir."""
    templates = tmp_path / "report-templates"
    templates.mkdir()
    replacement = runs_module.RunStore(
        root=tmp_path / "runs",
        templates_dir=templates,
        backups_dir=tmp_path / "backups",
        trash_dir=tmp_path / "trash",
    )
    previous = runs_module._STORE
    runs_module._STORE = replacement
    try:
        yield replacement
    finally:
        runs_module._STORE = previous
        replacement.shutdown(wait=True)


@pytest.fixture()
def client(store) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def form_pairs(key: str = "probe_template", **overrides) -> list[tuple[str, str]]:
    """A minimal but genuinely valid editor submission.

    Two sources of different kinds and two sections, so the section -> source
    cross-references are exercised rather than assumed. An override whose value
    is `None` DELETES the field; an override naming a field the baseline does
    not carry ADDS it, so a test can never think it exercised something it did
    not.
    """
    pairs = [
        ("base_sha", ""),
        ("report_type", key),
        ("title", f"Probe {key}"),
        ("description", "A probe template with a description long enough to be real."),
        ("version", "0.1.0"),
        ("owner", "dmpk"),
        ("doc_heading", f"Probe {key}"),
        ("tags__domain", "pre_clinical"),
        ("tags__compliance", "non_gxp"),
        ("input.k", "i1"),
        ("input.i1.id", "compound_id"),
        ("input.i1.prompt", "Compound identifier"),
        ("input.i1.required", "1"),
        ("source.k", "s1"),
        ("source.s1.id", "assays"),
        ("source.s1.kind", "bigquery"),
        ("source.s1.dataset", "preclin"),
        ("source.s1.query_id", "assays_v1"),
        ("source.k", "s2"),
        ("source.s2.id", "method_notes"),
        ("source.s2.kind", "confluence"),
        ("source.s2.space", "DMPK"),
        ("section.k", "t1"),
        ("section.t1.heading", "Overview"),
        ("section.t1.instruction", "Summarise the assay results and cite every value."),
        ("section.t1.sources", "s1"),
        ("section.t1.table", "s1"),
        ("section.k", "t2"),
        ("section.t2.heading", "Method"),
        ("section.t2.instruction", "Describe the assay method and where it is written down."),
        ("section.t2.sources", "s2"),
        ("section.t2.table", ""),
        ("citation_required", "1"),
        ("citation_granularity", "claim"),
        ("citation_min", "1"),
    ]
    if not overrides:
        return pairs
    out: list[tuple[str, str]] = []
    for name, value in pairs:
        if name in overrides:
            replacement = overrides[name]
            if replacement is None:
                continue
            out.append((name, replacement))
        else:
            out.append((name, value))
    present = {name for name, _ in pairs}
    for name, value in overrides.items():
        if name not in present and value is not None:
            out.append((name, value))
    return out


def post_form(client: TestClient, path: str, pairs, op: str = "save"):
    body = urllib.parse.urlencode(list(pairs) + [("op", op)])
    return client.post(path, content=body, headers=FORM_HEADERS, follow_redirects=False)


def create(client: TestClient, key: str = "probe_template", **overrides):
    return post_form(client, "/templates", form_pairs(key, **overrides))


def strip_tags(markup: str) -> str:
    import html as html_module

    return html_module.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)))


def issue_text(page: str) -> str:
    rows = re.findall(r'<li class="rg-issues__row[^"]*">(.*?)</li>', page, re.S)
    return " ".join(strip_tags(row) for row in rows)


def card_keys_in_order(page: str) -> list[str]:
    """The template keys the gallery actually rendered, in document order."""
    return re.findall(r'href="[^"]*/new/([A-Za-z0-9_.-]+)"', page)


def group_headings(page: str) -> list[str]:
    return [strip_tags(h).strip() for h in re.findall(r'<h3 class="rg-h3"[^>]*>(.*?)</h3>', page, re.S)]


RAW_TEMPLATE = """---
report_type: {key}
title: {title}
version: 0.1.0
owner: {owner}
updated: {updated}
{tags}inputs:
  - id: compound_id
    prompt: Compound identifier
    required: true
sources:
  - id: assays
    type: bigquery
    dataset: preclin
    query_id: assays_v1
citation:
  required: true
  granularity: claim
  min_per_paragraph: 1
---

# {title}

## 1. Overview

> Instruction: Summarise the assay results for {{{{inputs.compound_id}}}}.
> Sources: assays
> Table: assays
"""


def write_raw(
    directory: Path,
    key: str,
    *,
    title: str = "",
    owner: str = "dmpk",
    updated: str = "2026-01-01",
    tags: str = "",
) -> Path:
    """A hand-authored template file, bypassing the editor entirely."""
    path = directory / f"{key}.md"
    path.write_text(
        RAW_TEMPLATE.format(
            key=key,
            title=title or key.replace("_", " ").title(),
            owner=owner,
            updated=updated,
            tags=tags,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def fingerprint(template) -> dict:
    """Everything about a loaded template that a serializer could lose.

    Deliberately written out here rather than borrowed from the writer's own
    `_comparable`: a test that reuses the implementation's notion of equality
    cannot detect the implementation narrowing it. `metadata.authored_at` is a
    wall clock and is the one thing excluded.
    """
    return {
        "template_id": template.template_id,
        "report_type": template.report_type,
        "title": template.title,
        "version": template.version,
        "description": template.description,
        "status": str(template.status),
        "owner": template.metadata.authored_by,
        "tags": template.tags,
        "global_style": template.global_style.model_dump(),
        "sections": [
            {
                "section_id": section.section_id,
                "title": section.title,
                "level": section.level,
                "instruction": section.generation.prompt_template,
                "mode": str(section.generation.mode),
                "output_shape": str(section.generation.output_shape),
                "style_directives": list(section.generation.style_directives),
                "citation_policy": section.citation_policy.model_dump(),
                "validation_rules": [r.model_dump() for r in section.validation_rules],
                "inputs": [
                    b.model_dump()
                    for b in section.data_bindings
                    if isinstance(b, FreeTextInputBinding)
                ],
                # Every non-input binding, dumped whole: kind, ids, sql, cql,
                # filter tags, connector, endpoint and every parameter.
                "sources": [
                    b.model_dump()
                    for b in section.data_bindings
                    if not isinstance(b, FreeTextInputBinding)
                ],
            }
            for section in template.all_sections()
        ],
    }


def wait_for_terminal(client: TestClient, run_id: str, timeout: float = RUN_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}/progress")
        assert response.status_code == 200, response.text[:2000]
        payload = response.json()
        if payload["terminal"]:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s: {payload}")


# ===========================================================================
# 1. TAXONOMY
# ===========================================================================


def test_the_shipped_taxonomy_loads_with_no_complaints(taxonomy: Taxonomy) -> None:
    assert taxonomy.load_errors == ()
    assert taxonomy.source == str(TAXONOMY_PATH)
    assert taxonomy.facets, "a taxonomy with no facets cannot classify anything"


def test_every_facet_is_exposed_exactly_as_the_file_configures_it(
    taxonomy: Taxonomy, config: dict
) -> None:
    """Facet order, ids, labels, cardinality, required and mode all come from
    the file. Nothing in the code may name or reorder a facet."""
    configured = config["facets"]
    assert [f.id for f in taxonomy.facets] == [f["id"] for f in configured]
    assert taxonomy.facet_order_ids() == tuple(f["id"] for f in configured)

    for facet, raw in zip(taxonomy.facets, configured):
        assert facet.label == raw["label"]
        assert facet.cardinality == raw.get("cardinality", "single")
        assert facet.required is bool(raw.get("required", False))
        assert facet.mode == raw.get("mode", "closed")
        assert facet.groupable is bool(raw.get("groupable", True))
        assert facet.default == raw.get("default")


def test_every_value_is_exposed_exactly_as_the_file_configures_it(
    taxonomy: Taxonomy, config: dict
) -> None:
    for raw in config["facets"]:
        facet = taxonomy.facet(raw["id"])
        assert facet is not None, f"{raw['id']} configured but not exposed"
        assert list(facet.value_ids) == [v["id"] for v in raw["values"]]
        for value, raw_value in zip(facet.values, raw["values"]):
            assert value.label == raw_value["label"]
            assert taxonomy.label_for(facet.id, value.id) == raw_value["label"]


def test_pre_clinical_is_a_value_on_the_domain_facet(taxonomy: Taxonomy) -> None:
    """The user's own work. It is a value on a facet, not a special case."""
    domain = taxonomy.facet("domain")
    assert domain is not None
    assert "pre_clinical" in domain.value_ids
    assert domain.value("pre_clinical").label == "Pre-Clinical"


def test_the_taxonomy_is_extensible_not_a_hardcoded_list(tmp_path: Path) -> None:
    """A facet nothing in the code has ever heard of works end to end."""
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "taxonomy_version: 1\n"
        "defaults: {group_by: instrument, sort: name}\n"
        "facets:\n"
        "  - id: instrument\n"
        "    label: Instrument\n"
        "    cardinality: multi\n"
        "    mode: open\n"
        "    groupable: true\n"
        "    values:\n"
        "      - {id: lcms, label: LC-MS}\n",
        encoding="utf-8",
    )
    custom = load_taxonomy(path, refresh=True)

    assert custom.load_errors == ()
    assert custom.facet_ids == ("instrument",)
    assert custom.default_group == "instrument"
    tags, issues = custom.normalize({"instrument": ["lcms", "nmr"]})
    assert tags == {"instrument": ["lcms", "nmr"]}
    assert issues == [], "an open facet must accept a new value silently"


def test_compliance_offers_both_gxp_and_non_gxp(taxonomy: Taxonomy) -> None:
    compliance = taxonomy.facet("compliance")
    assert compliance is not None
    assert set(compliance.value_ids) == {"non_gxp", "gxp"}
    assert compliance.value("gxp").label == "GxP"
    assert compliance.value("non_gxp").label == "Non-GxP"
    assert not compliance.value("gxp").deprecated, "GxP must be genuinely selectable"


def test_non_gxp_is_the_default_and_gxp_is_never_a_default(taxonomy: Taxonomy) -> None:
    assert taxonomy.facet("compliance").default == "non_gxp"
    assert taxonomy.defaults_for_new()["compliance"] == ["non_gxp"]
    assert "gxp" not in [
        value for values in taxonomy.defaults_for_new().values() for value in values
    ]


def test_a_brand_new_draft_starts_non_gxp(taxonomy: Taxonomy) -> None:
    from services.template_service.report_doc_writer import blank_draft

    draft = blank_draft(report_type="fresh", tags=taxonomy.defaults_for_new())
    assert draft.tags["compliance"] == ["non_gxp"]


def test_the_new_template_form_preselects_non_gxp_and_never_gxp(client: TestClient) -> None:
    select = re.search(
        r'<select[^>]*name="tags__compliance".*?</select>',
        client.get("/templates/new").text,
        re.S,
    )
    assert select is not None, "the compliance facet is not on the new-template form"
    assert re.search(r'value="non_gxp"[^>]*\bselected\b', select.group(0))
    assert not re.search(r'value="gxp"[^>]*\bselected\b', select.group(0))


def test_selecting_gxp_is_a_label_and_changes_nothing_else(client: TestClient, store) -> None:
    """§9. Two templates identical apart from the compliance value must produce
    identical cards apart from the tag-derived fields."""
    create(client, "probe_non_gxp")
    create(client, "probe_gxp", **{"tags__compliance": "gxp"})

    a = store.get_template("probe_non_gxp").to_dict()
    b = store.get_template("probe_gxp").to_dict()
    tag_derived = {
        "tags", "tag_tokens", "chips", "n_more_tags", "tag_aria",
        "key", "path", "template_id", "title", "search",
    }
    assert {k: v for k, v in a.items() if k not in tag_derived} == {
        k: v for k, v in b.items() if k not in tag_derived
    }
    assert store.get_template("probe_gxp").tags["compliance"] == ["gxp"]


def test_an_unknown_facet_is_kept_verbatim_and_only_warned_about(taxonomy: Taxonomy) -> None:
    """Deleting a facet from the config must not make tagged templates vanish."""
    tags, issues = taxonomy.normalize({"nonsense_facet": ["whatever"]})
    assert tags["nonsense_facet"] == ["whatever"]
    codes = {(i.code, i.severity) for i in issues}
    assert ("unknown_facet", "warn") in codes
    assert all(i.severity == "warn" for i in issues)

    reported = taxonomy.validate({"nonsense_facet": ["whatever"]})
    assert any(i.code == "unknown_facet" and i.severity == "warn" for i in reported)


def test_an_unknown_value_on_a_closed_facet_is_kept_and_warned_about(
    taxonomy: Taxonomy,
) -> None:
    assert taxonomy.facet("domain").mode == "closed"
    tags, issues = taxonomy.normalize({"domain": ["not_a_domain"], "compliance": ["non_gxp"]})
    assert tags["domain"] == ["not_a_domain"], "an unknown value must survive a round trip"
    assert any(
        i.code == "unknown_value" and i.facet == "domain" and i.severity == "warn"
        for i in issues
    )


def test_a_new_value_on_an_open_facet_is_accepted_in_silence(taxonomy: Taxonomy) -> None:
    assert taxonomy.facet("therapeutic_area").mode == "open"
    tags, issues = taxonomy.normalize(
        {"domain": ["dmpk"], "compliance": ["non_gxp"], "therapeutic_area": ["dermatology"]}
    )
    assert tags["therapeutic_area"] == ["dermatology"]
    assert [i for i in issues if i.facet == "therapeutic_area"] == []


def test_a_missing_required_facet_gets_its_default_in_memory_only(taxonomy: Taxonomy) -> None:
    tags, issues = taxonomy.normalize({"domain": ["dmpk"]})
    assert tags["compliance"] == ["non_gxp"]
    missing = [i for i in issues if i.code == "missing_required"]
    assert [i.facet for i in missing] == ["compliance"]
    assert missing[0].severity == "warn"


def test_a_missing_required_facet_with_no_default_is_reported(taxonomy: Taxonomy) -> None:
    assert taxonomy.facet("domain").required is True
    assert taxonomy.facet("domain").default is None
    issues = taxonomy.validate({"compliance": ["non_gxp"]})
    assert any(i.code == "missing_required" and i.facet == "domain" for i in issues)


def test_a_single_value_facet_is_truncated_not_silently_widened(taxonomy: Taxonomy) -> None:
    assert taxonomy.facet("domain").is_multi is False
    tags, issues = taxonomy.normalize({"domain": ["dmpk", "clinical"], "compliance": ["gxp"]})
    assert tags["domain"] == ["dmpk"]
    assert any(i.code == "cardinality" and i.facet == "domain" for i in issues)


@pytest.mark.parametrize(
    "tags, blocking, non_blocking",
    [
        ({"domain": ["dmpk"]}, ["missing_required"], []),
        ({"domain": ["dmpk", "clinical"], "compliance": ["non_gxp"]}, ["cardinality"], []),
        ({"domain": ["invented"], "compliance": ["non_gxp"]}, [], ["unknown_value"]),
        ({"domain": ["dmpk"], "compliance": ["non_gxp"], "zzz": ["x"]}, [], ["unknown_facet"]),
    ],
    ids=["missing-required", "cardinality", "unknown-value", "unknown-facet"],
)
def test_only_missing_required_and_cardinality_block_a_save(
    taxonomy: Taxonomy, tags, blocking, non_blocking
) -> None:
    """The contract's split: a tag problem that means "you have not decided"
    blocks; a tag problem that means "another team decided differently" does
    not, or a value typed by someone else would be silently deleted."""
    draft = draft_from_path(LOADABLE[0])
    draft.tags = tags
    issues = runs_module.validate_template_draft(draft, is_new=False, taxonomy=taxonomy)

    errors = {i.code for i in issues if i.severity == "error"}
    warnings = {i.code for i in issues if i.severity == "warning"}
    for code in blocking:
        assert code in errors, f"{code} should block a save"
    for code in non_blocking:
        assert code in warnings, f"{code} should warn, not block"
        assert code not in errors


# ===========================================================================
# 2. ROUND-TRIP — the critical invariant
# ===========================================================================


def test_the_corpus_actually_contains_templates() -> None:
    """A guard on every parametrized test below: an empty list would make them
    all pass vacuously."""
    assert len(LOADABLE) >= 6, f"expected the shipped corpus, found {LOADABLE}"


@pytest.mark.parametrize("name", NOT_TEMPLATES)
def test_the_documentation_files_are_not_templates(name: str) -> None:
    path = TEMPLATES_DIR / name
    assert path.exists()
    assert path not in LOADABLE
    with pytest.raises(ReportDocError):
        load_report_doc(path)


@pytest.mark.parametrize("path", LOADABLE, ids=lambda p: p.stem)
def test_load_serialize_load_is_a_fixed_point(path: Path, tmp_path: Path, taxonomy) -> None:
    """INV-1. Sections, inputs, every source kind and its parameters, the
    citation policy and the tags all survive being written and read back."""
    original = load_report_doc(path)

    draft = draft_from_path(path)
    text = serialize_draft(draft, facet_order=taxonomy.facet_order_ids())

    rewritten = tmp_path / path.name
    rewritten.write_text(text, encoding="utf-8", newline="\n")
    reloaded = load_report_doc(rewritten)

    assert fingerprint(reloaded) == fingerprint(original)


@pytest.mark.parametrize("path", LOADABLE, ids=lambda p: p.stem)
def test_serializing_is_idempotent(path: Path, taxonomy) -> None:
    """A no-op save must not churn the file, or every save becomes a diff."""
    first = serialize_draft(draft_from_path(path), facet_order=taxonomy.facet_order_ids())
    second = serialize_draft(
        draft_from_text(first, report_type=draft_from_path(path).report_type),
        facet_order=taxonomy.facet_order_ids(),
    )
    assert second == first


@pytest.mark.parametrize("path", LOADABLE, ids=lambda p: p.stem)
def test_every_source_parameter_survives_the_round_trip(
    path: Path, tmp_path: Path, taxonomy
) -> None:
    """Stated separately from the fingerprint because this is the one that
    matters: a lost `query_id` or `cql` is a citation that cannot be resolved."""
    before = draft_from_path(path)
    text = serialize_draft(before, facet_order=taxonomy.facet_order_ids())
    (tmp_path / path.name).write_text(text, encoding="utf-8", newline="\n")
    after = draft_from_text(text, report_type=before.report_type)

    def described(draft) -> list[dict]:
        return [
            {
                k: v
                for k, v in dataclasses.asdict(source).items()
                if k != "key" and v not in ("", [], {})
            }
            for source in draft.sources
        ]

    assert described(after) == described(before)
    assert [s.kind for s in after.sources] == [s.kind for s in before.sources]


@pytest.mark.parametrize("path", LOADABLE, ids=lambda p: p.stem)
def test_tags_survive_the_round_trip_including_multi_valued_facets(
    path: Path, tmp_path: Path, taxonomy
) -> None:
    before = draft_from_path(path)
    before.tags = {
        "domain": ["pre_clinical"],
        "compliance": ["non_gxp"],
        "therapeutic_area": ["respiratory", "oncology"],
        "invented_facet": ["invented_value"],
    }
    text = serialize_draft(before, facet_order=taxonomy.facet_order_ids())
    rewritten = tmp_path / path.name
    rewritten.write_text(text, encoding="utf-8", newline="\n")

    assert load_report_doc(rewritten).tags == before.tags


@pytest.mark.parametrize(
    "required, granularity, minimum",
    [
        (True, "claim", 1),
        (True, "paragraph", 3),
        (False, "section", 0),
    ],
    ids=["claim-1", "paragraph-3", "off-section-0"],
)
def test_a_non_default_citation_policy_survives_the_round_trip(
    tmp_path: Path, taxonomy, required, granularity, minimum
) -> None:
    """Every shipped template happens to use `claim` / 1, so the corpus-wide
    round-trip cannot tell a real serializer from one that hardcodes the
    default. This one can."""
    draft = draft_from_path(LOADABLE[0])
    draft.citation_required = required
    draft.citation_granularity = granularity
    draft.citation_min_per_paragraph = minimum

    text = serialize_draft(draft, facet_order=taxonomy.facet_order_ids())
    out = tmp_path / "citation_probe.md"
    out.write_text(text, encoding="utf-8", newline="\n")

    policies = {
        (s.citation_policy.required, s.citation_policy.granularity,
         s.citation_policy.min_citations_per_paragraph)
        for s in load_report_doc(out).all_sections()
    }
    assert policies == {(required, granularity, minimum)}

    reparsed = draft_from_text(text, report_type=draft.report_type)
    assert reparsed.citation_required is required
    assert reparsed.citation_granularity == granularity
    assert reparsed.citation_min_per_paragraph == minimum


def test_a_template_authored_in_the_app_round_trips_like_a_shipped_one(
    client: TestClient, store, tmp_path: Path, taxonomy
) -> None:
    """The invariant has to hold for new files too, not just the migrated six."""
    assert create(client, "authored_here").status_code == 303
    written = store.templates_dir / "authored_here.md"

    original = load_report_doc(written)
    text = serialize_draft(draft_from_path(written), facet_order=taxonomy.facet_order_ids())
    again = tmp_path / "again.md"
    again.write_text(text, encoding="utf-8", newline="\n")

    assert fingerprint(load_report_doc(again)) == fingerprint(original)


# ===========================================================================
# 3. WRITER SAFETY
# ===========================================================================


@pytest.mark.parametrize("path", LOADABLE, ids=lambda p: p.stem)
def test_serializing_then_loading_never_raises(path: Path, tmp_path: Path, taxonomy) -> None:
    text = serialize_draft(draft_from_path(path), facet_order=taxonomy.facet_order_ids())
    out = tmp_path / path.name
    out.write_text(text, encoding="utf-8", newline="\n")
    load_report_doc(out)  # must not raise


def valid_draft(key: str = "safety_probe") -> TemplateDraft:
    return TemplateDraft(
        report_type=key,
        title="Safety Probe",
        description="A template used to prove nothing invalid reaches disk.",
        version="0.1.0",
        owner="dmpk",
        doc_heading="Safety Probe",
        tags={"domain": ["pre_clinical"], "compliance": ["non_gxp"]},
        inputs=[DraftInput(key="i1", id="compound_id", prompt="Compound identifier")],
        sources=[
            DraftSource(key="s1", id="assays", kind="bigquery", dataset="p", query_id="q")
        ],
        sections=[
            DraftSection(
                key="t1",
                heading="Overview",
                instruction="Summarise the assays and cite every value.",
                source_keys=["s1"],
                table_key="s1",
            )
        ],
    )


def test_a_valid_draft_does_reach_disk(tmp_path: Path, taxonomy) -> None:
    """The control for the refusal tests below — without it they could pass
    because the writer refuses everything."""
    target = tmp_path / "safety_probe.md"
    result = write_template(
        valid_draft(), target, facet_order=taxonomy.facet_order_ids(), create=True
    )
    assert target.exists()
    assert result.created is True
    assert load_report_doc(target).report_type == "safety_probe"


def test_a_section_referencing_an_unknown_source_never_reaches_disk(
    tmp_path: Path, taxonomy
) -> None:
    draft = valid_draft("dangling_probe")
    draft.sections[0].source_keys = ["s_does_not_exist"]

    target = tmp_path / "dangling_probe.md"
    scratch_before = set(SCRATCH_DIR.glob("dangling_probe.*")) if SCRATCH_DIR.exists() else set()

    with pytest.raises(TemplateWriteError):
        write_template(draft, target, facet_order=taxonomy.facet_order_ids(), create=True)

    assert not target.exists(), "a refused template must leave NO file behind"
    assert list(tmp_path.iterdir()) == [], f"stray files: {list(tmp_path.iterdir())}"
    scratch_after = set(SCRATCH_DIR.glob("dangling_probe.*")) if SCRATCH_DIR.exists() else set()
    assert scratch_after == scratch_before, "a refused write left a scratch file behind"


def test_a_section_whose_table_names_an_unknown_source_never_reaches_disk(
    tmp_path: Path, taxonomy
) -> None:
    draft = valid_draft("dangling_table_probe")
    draft.sections[0].table_key = "s_does_not_exist"

    target = tmp_path / "dangling_table_probe.md"
    with pytest.raises(TemplateWriteError):
        write_template(draft, target, facet_order=taxonomy.facet_order_ids(), create=True)

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_writer_refuses_to_overwrite_when_asked_to_create(
    tmp_path: Path, taxonomy
) -> None:
    target = tmp_path / "safety_probe.md"
    write_template(valid_draft(), target, facet_order=taxonomy.facet_order_ids(), create=True)
    before = target.read_text(encoding="utf-8")

    second = valid_draft()
    second.title = "Something Else Entirely"
    with pytest.raises(TemplateWriteError):
        write_template(second, target, facet_order=taxonomy.facet_order_ids(), create=True)

    assert target.read_text(encoding="utf-8") == before


# ===========================================================================
# 4. CRUD ROUTES
# ===========================================================================


def test_create_writes_a_file_the_loader_reads_back_and_shows_it_in_the_gallery(
    client: TestClient, store
) -> None:
    response = create(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/?saved=probe_template"

    path = store.templates_dir / "probe_template.md"
    assert path.exists()

    template = load_report_doc(path)
    assert template.report_type == "probe_template"
    assert template.tags == {"domain": ["pre_clinical"], "compliance": ["non_gxp"]}
    assert [s.title for s in template.all_sections()] == ["Overview", "Method"]

    assert "probe_template" in card_keys_in_order(client.get("/").text)


def test_a_duplicate_key_is_refused_and_the_original_is_untouched(
    client: TestClient, store
) -> None:
    create(client, "probe_template")
    path = store.templates_dir / "probe_template.md"
    before = path.read_text(encoding="utf-8")

    response = create(client, "probe_template", title="Impostor")

    assert 400 <= response.status_code < 500, response.status_code
    assert "already exists" in issue_text(response.text)
    assert path.read_text(encoding="utf-8") == before
    assert load_report_doc(path).title != "Impostor"


def test_a_duplicate_key_differing_only_in_case_is_still_a_duplicate(
    client: TestClient, store
) -> None:
    """NTFS treats X.md and x.md as one file, so a case-only difference would
    be a silent overwrite rather than a second template."""
    create(client, "probe_template")
    before = (store.templates_dir / "probe_template.md").read_text(encoding="utf-8")

    response = create(client, "Probe_Template", title="Impostor")

    assert 400 <= response.status_code < 500
    assert (store.templates_dir / "probe_template.md").read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "overrides, expect",
    [
        ({"tags__compliance": ""}, "Compliance has not been set"),
        ({"tags__domain": ""}, "Domain area has not been set"),
        ({"tags__domain": "pre_clinical", "tags__domain_2": None}, None),
    ],
    ids=["no-compliance", "no-domain", "control"],
)
def test_a_draft_with_a_blocking_tag_problem_writes_no_file(
    client: TestClient, store, overrides, expect
) -> None:
    response = create(client, "tagless_probe", **overrides)
    path = store.templates_dir / "tagless_probe.md"

    if expect is None:  # the control: the same form WITH its tags does save
        assert response.status_code == 303
        assert path.exists()
        return

    assert 400 <= response.status_code < 500, response.status_code
    assert expect in issue_text(response.text)
    assert not path.exists(), "a refused template must not appear on disk"
    assert list(store.templates_dir.iterdir()) == []


def test_two_values_on_a_single_value_facet_are_refused(client: TestClient, store) -> None:
    pairs = form_pairs("cardinality_probe") + [("tags__domain", "clinical")]
    response = post_form(client, "/templates", pairs)

    assert 400 <= response.status_code < 500
    assert "single value" in issue_text(response.text)
    assert not (store.templates_dir / "cardinality_probe.md").exists()


def test_an_unknown_tag_value_is_a_warning_that_still_saves_and_survives(
    client: TestClient, store
) -> None:
    """The deliberate counterpart to the test above. A value another team
    invented is preserved, not deleted — that is what makes the taxonomy
    extensible rather than a closed enum."""
    response = create(
        client,
        "open_tag_probe",
        **{"tags_new__therapeutic_area": "Dermatology", "tags__zzz_custom": "anything"},
    )
    assert response.status_code == 303

    tags = load_report_doc(store.templates_dir / "open_tag_probe.md").tags
    assert tags["therapeutic_area"] == ["dermatology"]
    assert tags["zzz_custom"] == ["anything"]


def test_edit_persists_and_does_not_change_the_key(client: TestClient, store) -> None:
    create(client)
    edited = form_pairs(
        "probe_template",
        **{
            "title": "Edited Title",
            "tags__domain": "clinical",
            "section.t2.heading": "Method and limitations",
            "version": "0.2.0",
        },
    )
    response = post_form(client, "/templates/probe_template", edited)
    assert response.status_code == 303

    template = load_report_doc(store.templates_dir / "probe_template.md")
    assert template.title == "Edited Title"
    assert template.version == "0.2.0"
    assert template.tags["domain"] == ["clinical"]
    assert template.all_sections()[1].title == "Method and limitations"
    assert store.template_keys() == ["probe_template"]


def test_editing_one_template_leaves_every_other_one_alone(
    client: TestClient, store
) -> None:
    create(client, "probe_one")
    create(client, "probe_two")
    untouched = (store.templates_dir / "probe_two.md").read_text(encoding="utf-8")

    post_form(client, "/templates/probe_one", form_pairs("probe_one", title="Changed"))

    assert (store.templates_dir / "probe_two.md").read_text(encoding="utf-8") == untouched


def test_clone_prefills_a_new_key_and_carries_the_tags_over(
    client: TestClient, store
) -> None:
    create(client, "probe_template", **{"tags__domain": "nonclinical_safety"})

    page = client.get("/templates/new?from=probe_template").text
    key_field = re.search(r'name="report_type"[^>]*value="([^"]*)"', page)
    assert key_field is not None
    assert key_field.group(1) == "probe_template_copy"
    assert key_field.group(1) != "probe_template"

    version_field = re.search(r'name="version"[^>]*value="([^"]*)"', page)
    assert version_field.group(1) == "0.1.0", "a clone starts at version 0.1.0"

    domain = re.search(r'<select[^>]*name="tags__domain".*?</select>', page, re.S).group(0)
    assert re.search(r'value="nonclinical_safety"[^>]*\bselected\b', domain)


def test_a_clone_is_an_independent_file(client: TestClient, store) -> None:
    create(client, "probe_template")
    assert create(client, "probe_template_copy", title="The Copy").status_code == 303

    assert sorted(store.template_keys()) == ["probe_template", "probe_template_copy"]

    post_form(
        client,
        "/templates/probe_template_copy",
        form_pairs("probe_template_copy", title="Copy Edited Alone"),
    )

    assert load_report_doc(store.templates_dir / "probe_template_copy.md").title == (
        "Copy Edited Alone"
    )
    assert load_report_doc(store.templates_dir / "probe_template.md").title == (
        "Probe probe_template"
    )


def test_delete_removes_it_from_disk_and_from_the_gallery(client: TestClient, store) -> None:
    create(client)
    path = store.templates_dir / "probe_template.md"

    response = client.post("/templates/probe_template/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?deleted=probe_template"
    assert not path.exists()
    assert "probe_template" not in card_keys_in_order(client.get("/").text)
    assert client.get("/templates/probe_template/edit").status_code == 404


def test_delete_is_recoverable_because_the_file_is_moved_not_erased(
    client: TestClient, store
) -> None:
    create(client)
    before = (store.templates_dir / "probe_template.md").read_text(encoding="utf-8")

    client.post("/templates/probe_template/delete", follow_redirects=False)
    trashed = list(store.trash_dir.glob("probe_template.*.md"))
    assert len(trashed) == 1
    assert trashed[0].read_text(encoding="utf-8") == before

    response = client.post("/templates/probe_template/undelete", follow_redirects=False)
    assert response.status_code == 303
    assert (store.templates_dir / "probe_template.md").read_text(encoding="utf-8") == before
    assert "probe_template" in card_keys_in_order(client.get("/").text)


def test_a_submitted_section_pointing_at_a_missing_source_is_refused_not_trimmed(
    client: TestClient, store
) -> None:
    """The route must hand the dangling reference to the validator rather than
    filtering it out while reading the form.

    Trimming it would save a template whose section has no evidence behind it —
    a report section generated from nothing — and would make the writer's
    `dangling_source` rule unreachable from the only path that writes files.
    """
    broken = form_pairs(
        "dangling_probe", **{"section.t1.sources": "s404", "section.t1.table": "s404"}
    )
    response = post_form(client, "/templates", broken)

    assert 400 <= response.status_code < 500, response.status_code
    assert not (store.templates_dir / "dangling_probe.md").exists()
    assert list(store.templates_dir.iterdir()) == []
    text = issue_text(response.text)
    assert "source that no longer exists" in text
    assert "table refers to a source that no longer exists" in text


def test_a_stale_base_sha_is_a_conflict_rather_than_a_silent_overwrite(
    client: TestClient, store
) -> None:
    create(client)
    stale = form_pairs("probe_template", title="Mine") + [("base_sha", "0" * 64)]

    response = post_form(client, "/templates/probe_template", stale)
    assert response.status_code == 409
    assert load_report_doc(store.templates_dir / "probe_template.md").title != "Mine"

    response = post_form(client, "/templates/probe_template", stale + [("force", "1")])
    assert response.status_code == 303
    assert load_report_doc(store.templates_dir / "probe_template.md").title == "Mine"


# ===========================================================================
# 5. GALLERY — grouping, sorting, filtering
# ===========================================================================


@pytest.fixture()
def populated(client: TestClient, store) -> TestClient:
    """Four templates chosen so grouping, sorting and filtering each have a
    discriminating case rather than an accidental one.

    Every `domain` here is a CONFIGURED value, and the three are picked so that
    config order (discovery, pre_clinical, clinical) matches nothing else a
    buggy implementation might order groups by:

      * alphabetically it would be clinical, discovery, pre_clinical;
      * by the cards' own sort it would be clinical (alpha), discovery (bravo),
        pre_clinical (charlie) — the alphabetically FIRST card deliberately
        sits in the LAST group;
      * by size it would put the two-card group (clinical) first.

    An unconfigured value could not do that job — it lands in the trailing
    alphabetical bucket whichever way the configured groups are ordered — which
    is why the fine-grained areas live on `discipline` here, not on `domain`.
    """
    create(
        client,
        "alpha_dmpk",
        title="Alpha DMPK",
        owner="dmpk",
        **{
            "tags__domain": "clinical",
            "tags__discipline": "dmpk",
            "tags__compliance": "non_gxp",
        },
    )
    create(
        client,
        "bravo_safety",
        title="Bravo Safety",
        owner="safety",
        **{
            "tags__domain": "discovery",
            "tags__discipline": "nonclinical_safety",
            "tags__compliance": "gxp",
        },
    )
    create(
        client,
        "charlie_preclinical",
        title="Charlie Preclinical",
        owner="dmpk",
        **{
            "tags__domain": "pre_clinical",
            "tags__compliance": "non_gxp",
            "tags__therapeutic_area": "respiratory",
        },
    )
    # A third section, APPENDED rather than overridden — `section.k` is a
    # repeating anchor field, so replacing it would delete the other two rows.
    delta = form_pairs(
        "delta_dmpk",
        title="Delta DMPK",
        owner="alpha_team",
        **{
            "tags__domain": "clinical",
            "tags__discipline": "dmpk",
            "tags__compliance": "gxp",
        },
    ) + [
        ("section.k", "t3"),
        ("section.t3.heading", "Limitations"),
        ("section.t3.instruction", "State what this report does not cover."),
        ("section.t3.sources", "s2"),
        ("section.t3.table", ""),
    ]
    assert post_form(client, "/templates", delta).status_code == 303
    assert store.get_template("delta_dmpk").n_sections == 3
    return client


def test_group_by_domain_puts_each_card_under_its_own_heading(populated) -> None:
    view = runs_module.get_store().gallery_view(group="domain", sort="name")

    assert view.group == "domain"
    by_value = {g.value_id: [c.key for c in g.cards] for g in view.groups}
    assert by_value == {
        "discovery": ["bravo_safety"],
        "pre_clinical": ["charlie_preclinical"],
        "clinical": ["alpha_dmpk", "delta_dmpk"],
    }
    # Group ORDER follows config order, never the sort: alphabetically this
    # would be clinical, discovery, pre_clinical, and by the cards' own name
    # sort it would be clinical (alpha) first.
    assert [g.value_id for g in view.groups] == [
        "discovery",
        "pre_clinical",
        "clinical",
    ]
    assert sum(g.count for g in view.groups) == view.n_shown


def test_group_by_compliance_separates_gxp_from_non_gxp(populated) -> None:
    view = runs_module.get_store().gallery_view(group="compliance", sort="name")
    by_value = {g.value_id: sorted(c.key for c in g.cards) for g in view.groups}
    assert by_value == {
        "non_gxp": ["alpha_dmpk", "charlie_preclinical"],
        "gxp": ["bravo_safety", "delta_dmpk"],
    }


def test_group_by_owner_is_a_derived_group_not_a_configured_facet(populated) -> None:
    view = runs_module.get_store().gallery_view(group="owner", sort="name")
    assert {g.label: [c.key for c in g.cards] for g in view.groups} == {
        "alpha_team": ["delta_dmpk"],
        "dmpk": ["alpha_dmpk", "charlie_preclinical"],
        "safety": ["bravo_safety"],
    }


def test_group_none_is_one_flat_list(populated) -> None:
    view = runs_module.get_store().gallery_view(group="none", sort="name")
    assert len(view.groups) == 1
    assert [c.key for c in view.groups[0].cards] == [c.key for c in view.cards]


def test_the_gallery_page_renders_the_group_headings(populated) -> None:
    """Every group heading is the configured LABEL, never the raw value id.

    Checked across two facets because the labels live on two: the broad bucket
    is on `domain`, the scientific sub-areas are on `discipline`.
    """
    headings = group_headings(populated.get("/?group=domain").text)
    assert "Discovery" in headings
    assert "Pre-Clinical" in headings
    assert "Clinical" in headings

    headings = group_headings(populated.get("/?group=discipline").text)
    assert "DMPK / ADME" in headings
    assert "Nonclinical Safety" in headings


@pytest.mark.parametrize(
    "sort, expected",
    [
        ("name", ["alpha_dmpk", "bravo_safety", "charlie_preclinical", "delta_dmpk"]),
        ("sections", ["delta_dmpk", "alpha_dmpk", "bravo_safety", "charlie_preclinical"]),
        ("owner", ["delta_dmpk", "alpha_dmpk", "charlie_preclinical", "bravo_safety"]),
    ],
)
def test_sort_orders_the_flat_list(populated, sort, expected) -> None:
    view = runs_module.get_store().gallery_view(group="none", sort=sort)
    assert view.sort == sort
    assert [c.key for c in view.cards] == expected


def test_sort_by_recently_updated_puts_the_newest_first(client: TestClient, store) -> None:
    write_raw(store.templates_dir, "old_one", title="Old One", updated="2020-01-01")
    write_raw(store.templates_dir, "new_one", title="New One", updated="2026-08-01")

    view = store.gallery_view(group="none", sort="updated")
    assert [c.key for c in view.cards] == ["new_one", "old_one"]


def test_sort_survives_the_round_trip_through_the_rendered_page(populated) -> None:
    page = populated.get("/?group=none&sort=sections").text
    assert card_keys_in_order(page)[:1] == ["delta_dmpk"]


def test_a_tag_filter_narrows_the_result_set(populated) -> None:
    view = runs_module.get_store().gallery_view(tags=["domain:clinical"])
    assert sorted(c.key for c in view.cards) == ["alpha_dmpk", "delta_dmpk"]
    assert view.n_shown == 2
    assert view.n_total == 4
    assert view.filtering is True
    assert view.n_active == 1


def test_two_values_of_one_facet_are_combined_with_or(populated) -> None:
    view = runs_module.get_store().gallery_view(
        tags=["domain:clinical", "domain:pre_clinical"]
    )
    assert sorted(c.key for c in view.cards) == [
        "alpha_dmpk",
        "charlie_preclinical",
        "delta_dmpk",
    ]


def test_two_different_facets_are_combined_with_and(populated) -> None:
    view = runs_module.get_store().gallery_view(
        tags=["domain:clinical", "compliance:non_gxp"]
    )
    assert [c.key for c in view.cards] == ["alpha_dmpk"]

    both = runs_module.get_store().gallery_view(
        tags=["domain:pre_clinical", "compliance:gxp"]
    )
    assert both.cards == [], "AND across facets must be able to yield nothing"


def test_or_within_a_facet_and_and_across_facets_compose(populated) -> None:
    view = runs_module.get_store().gallery_view(
        tags=["domain:clinical", "domain:discovery", "compliance:gxp"]
    )
    assert sorted(c.key for c in view.cards) == ["bravo_safety", "delta_dmpk"]


def test_the_untagged_sentinel_selects_the_cards_with_that_facet_unset(
    populated,
) -> None:
    view = runs_module.get_store().gallery_view(tags=[f"therapeutic_area:{UNTAGGED}"])
    assert sorted(c.key for c in view.cards) == ["alpha_dmpk", "bravo_safety", "delta_dmpk"]


def test_the_free_text_search_narrows_too(populated) -> None:
    view = runs_module.get_store().gallery_view(q="Charlie")
    assert [c.key for c in view.cards] == ["charlie_preclinical"]


def test_no_match_shows_the_empty_state_not_a_blank_page(populated) -> None:
    response = populated.get("/?tag=domain:pre_clinical&tag=compliance:gxp")

    assert response.status_code == 200
    body = strip_tags(response.text)
    assert "No report types match these filters" in body
    assert "Clear filters" in body
    assert card_keys_in_order(response.text) == []


def test_a_stale_bookmark_widens_instead_of_erroring(populated) -> None:
    response = populated.get("/?tag=domain:no_such_value&tag=no_such_facet:x&group=nonsense")
    assert response.status_code == 200
    assert len(card_keys_in_order(response.text)) == 4

    view = runs_module.get_store().gallery_view(
        tags=["domain:no_such_value"], group="nonsense"
    )
    assert view.n_shown == view.n_total == 4
    assert view.group == "domain", "an unknown group falls back to the configured default"


def test_the_query_parameters_round_trip_into_the_rendered_controls(populated) -> None:
    page = populated.get(
        "/?group=compliance&sort=sections&tag=domain:clinical&q=Alpha"
    ).text

    group_select = re.search(r'name="group".*?</select>', page, re.S).group(0)
    assert re.search(r'value="compliance"[^>]*\bselected\b', group_select)

    sort_select = re.search(r'name="sort".*?</select>', page, re.S).group(0)
    assert re.search(r'value="sections"[^>]*\bselected\b', sort_select)

    assert re.search(r'name="q"[^>]*value="Alpha"', page)
    assert re.search(r'name="tag" value="domain:clinical"[^>]*\bchecked\b', page)


def test_the_clear_url_keeps_the_view_but_drops_the_filters(populated) -> None:
    view = runs_module.get_store().gallery_view(
        group="compliance", sort="sections", tags=["domain:clinical"], q="Alpha"
    )
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(view.clear_url).query)
    assert parsed.get("group") == ["compliance"]
    assert parsed.get("sort") == ["sections"]
    assert "tag" not in parsed
    assert "q" not in parsed


def test_facet_counts_ignore_that_facets_own_selection(populated) -> None:
    """A value showing "(2)" has to actually yield 2 when it is ticked; if the
    count were filtered by its own facet every unselected value would read 0."""
    view = runs_module.get_store().gallery_view(tags=["domain:clinical"])
    domain = next(f for f in view.facets if f.id == "domain")
    counts = {v.id: v.count for v in domain.values}
    assert counts["clinical"] == 2
    assert counts["discovery"] == 1
    assert counts["pre_clinical"] == 1

    compliance = next(f for f in view.facets if f.id == "compliance")
    assert {v.id: v.count for v in compliance.values} == {"non_gxp": 1, "gxp": 1}


def test_unparseable_files_stay_out_of_the_counts_groups_and_facets(
    client: TestClient, store
) -> None:
    (store.templates_dir / "just_notes.md").write_text("# notes\n", encoding="utf-8")
    create(client)

    view = store.gallery_view(group="domain")
    assert [c.key for c in view.cards] == ["probe_template"]
    assert view.n_total == 1
    assert [u.key for u in view.unavailable] == ["just_notes"]
    assert sum(g.count for g in view.groups) == 1


# ===========================================================================
# 6. BACKWARD COMPATIBILITY
# ===========================================================================


def test_a_template_with_no_tags_block_still_loads(client: TestClient, store) -> None:
    path = write_raw(store.templates_dir, "untagged_probe", title="Untagged Probe")
    assert "tags:" not in path.read_text(encoding="utf-8")

    template = load_report_doc(path)
    assert template.tags == {}
    assert template.tag_tokens() == []
    assert [s.title for s in template.all_sections()] == ["Overview"]


def test_a_template_with_no_tags_still_renders_in_the_gallery(
    client: TestClient, store
) -> None:
    write_raw(store.templates_dir, "untagged_probe", title="Untagged Probe")
    # A tagged neighbour, so "untagged sorts last" has something to sort after.
    create(client, "tagged_probe", **{"tags__domain": "pre_clinical"})

    response = client.get("/?group=domain")
    assert response.status_code == 200
    assert "untagged_probe" in card_keys_in_order(response.text)

    view = store.gallery_view(group="domain")
    assert view.n_total == 2
    untagged = [g for g in view.groups if g.untagged]
    assert len(untagged) == 1
    assert untagged[0].label == "No domain set"
    assert [c.key for c in untagged[0].cards] == ["untagged_probe"]
    assert [g.untagged for g in view.groups] == [False, True], (
        "the untagged group always sorts last, whatever the sort is"
    )
    assert view.groups[-1] is untagged[0]
    # and the rendered page agrees: the untagged card comes after the tagged one
    assert card_keys_in_order(response.text) == ["tagged_probe", "untagged_probe"]


def test_an_untagged_template_is_findable_by_the_untagged_filter(
    client: TestClient, store
) -> None:
    write_raw(store.templates_dir, "untagged_probe")
    create(client, "tagged_probe")

    view = store.gallery_view(tags=[f"domain:{UNTAGGED}"])
    assert [c.key for c in view.cards] == ["untagged_probe"]


def test_an_untagged_template_can_be_opened_and_saved_in_the_editor(
    client: TestClient, store
) -> None:
    """Opening a pre-tags file in the editor must not require re-authoring it,
    only choosing the two required facets."""
    write_raw(store.templates_dir, "untagged_probe", title="Untagged Probe")

    page = client.get("/templates/untagged_probe/edit")
    assert page.status_code == 200
    assert 'name="tags__domain"' in page.text


def test_a_template_carrying_a_facet_this_taxonomy_never_heard_of_still_loads(
    client: TestClient, store
) -> None:
    write_raw(
        store.templates_dir,
        "alien_probe",
        tags="tags:\n  domain: dmpk\n  compliance: non_gxp\n  from_another_team: [xyz]\n",
    )

    card = store.get_template("alien_probe")
    assert card.ok is True
    assert card.tags["from_another_team"] == ["xyz"]

    view = store.gallery_view()
    assert "from_another_team" in [f.id for f in view.facets]
    assert [c.key for c in store.gallery_view(tags=["from_another_team:xyz"]).cards] == [
        "alien_probe"
    ]


# ===========================================================================
# 7. A RUN STILL STARTS FROM A NEWLY CREATED TEMPLATE
# ===========================================================================


def test_a_run_can_be_started_from_a_template_created_in_the_app(
    client: TestClient, store
) -> None:
    assert create(client, "runnable_probe").status_code == 303

    card = store.get_template("runnable_probe")
    assert card.ok is True

    payload = {"template_key": "runnable_probe"}
    payload.update({f.binding_id: "GSK-TEST-1" for f in card.form_fields})

    response = client.post("/runs", data=payload, follow_redirects=False)
    assert response.status_code == 303, response.text[:2000]

    run_id = response.headers["location"].rstrip("/").rsplit("/", 1)[-1]
    progress = wait_for_terminal(client, run_id)
    assert progress["terminal"] is True

    page = client.get(f"/runs/{run_id}?tab=draft")
    assert page.status_code == 200
    assert "Overview" in page.text


def test_the_json_surface_lists_a_newly_created_template(client: TestClient) -> None:
    create(client, "runnable_probe")
    body = client.get("/api/templates").json()
    keys = [t["key"] for t in (body["templates"] if isinstance(body, dict) else body)]
    assert "runnable_probe" in keys
