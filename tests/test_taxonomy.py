"""Tests for the faceted tag taxonomy (ENG-1).

Covers: the shipped `report-templates/taxonomy.yaml`, the loader's tolerance of
a broken or missing config, the normalise/validate rule table, the token and
ordering helpers, the `ReportTemplate.tags` schema change and its backward
compatibility, the migration of the six existing templates, and the GxP policy
as it applies to configuration.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from services.template_service.report_doc import load_report_doc
from services.template_service.taxonomy import (
    FACET_ID_RE,
    FALLBACK_TAXONOMY,
    RESERVED_FACET_IDS,
    TAXONOMY_ENV_VAR,
    TAXONOMY_PATH,
    UNTAGGED,
    VALUE_ID_RE,
    Facet,
    FacetValue,
    Taxonomy,
    load_taxonomy,
    parse_token,
    slugify_value,
)
from shared.schemas.template import (
    ReportTemplate,
    TemplateMetadata,
    TemplateStatus,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "report-templates"

#: Every report template currently on disk — the six shipped ones plus any a
#: user has authored in the app. README.md and SKILL.template.md are
#: deliberately not report templates and must stay unparseable.
#: Use this for rules that must hold for the WHOLE corpus, forever.
REAL_TEMPLATES = sorted(
    p for p in TEMPLATES_DIR.glob("*.md") if p.name not in ("README.md", "SKILL.template.md")
)

#: The six templates the tag migration touched, BY NAME.
#: Migration assertions ("the block sits here", "this facet was left unset")
#: are statements about these six files only. Globbing the directory for them
#: would turn every template a user creates through the in-app editor into a
#: test failure — i.e. the suite would go red precisely when the feature works.
MIGRATED_TEMPLATES = [
    TEMPLATES_DIR / f"{name}.md"
    for name in (
        "candidate_selection_dossier",
        "compound_profile_onepager",
        "dmpk_adme_summary",
        "ib_nonclinical_sections",
        "nonclinical_safety_summary",
        "target_assessment",
    )
]


@pytest.fixture()
def taxonomy() -> Taxonomy:
    """The shipped taxonomy, loaded fresh (never the module cache)."""
    return load_taxonomy(TAXONOMY_PATH, refresh=True)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "taxonomy.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _template(**kwargs) -> ReportTemplate:
    return ReportTemplate(
        template_id="t",
        version="0.1.0",
        report_type="t",
        title="T",
        metadata=TemplateMetadata(
            authored_by="a",
            authored_at=datetime.now(timezone.utc),
            source_origin="from_scratch",
        ),
        sections=[],
        **kwargs,
    )


# --- the shipped config -----------------------------------------------------


def test_shipped_taxonomy_loads_without_a_single_complaint(taxonomy: Taxonomy) -> None:
    assert taxonomy.load_errors == ()
    assert taxonomy.source == str(TAXONOMY_PATH)
    assert taxonomy.version == 1
    assert taxonomy.facet_ids == (
        "domain",
        "discipline",
        "compliance",
        "document_class",
        "therapeutic_area",
        "modality",
    )


def test_shipped_defaults_group_by_domain_and_sort_by_name(taxonomy: Taxonomy) -> None:
    assert taxonomy.default_group == "domain"
    assert taxonomy.default_sort == "name"
    assert taxonomy.facet(taxonomy.default_group) is not None


def test_domain_facet_offers_pre_clinical(taxonomy: Taxonomy) -> None:
    """The user's own work is preclinical: that value has to be there."""
    domain = taxonomy.facet("domain")
    assert domain is not None
    assert "pre_clinical" in domain.value_ids
    assert taxonomy.label_for("domain", "pre_clinical") == "Pre-Clinical"


def test_every_configured_id_matches_the_identifier_rules(taxonomy: Taxonomy) -> None:
    for facet in taxonomy.facets:
        assert FACET_ID_RE.match(facet.id), facet.id
        assert facet.id not in RESERVED_FACET_IDS
        assert facet.label
        for value in facet.values:
            assert VALUE_ID_RE.match(value.id), (facet.id, value.id)
            assert value.label


def test_facet_and_value_ids_are_unique(taxonomy: Taxonomy) -> None:
    assert len(taxonomy.facet_ids) == len(set(taxonomy.facet_ids))
    for facet in taxonomy.facets:
        assert len(facet.value_ids) == len(set(facet.value_ids))


def test_the_taxonomy_file_carries_no_colour_configuration() -> None:
    """Chips are neutral by design; a per-value colour is not configurable."""
    raw = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))
    for facet in raw["facets"]:
        assert "color" not in facet and "colour" not in facet
        for value in facet["values"]:
            assert "color" not in value and "colour" not in value


def test_fallback_taxonomy_matches_the_shipped_file(taxonomy: Taxonomy) -> None:
    """If the config goes missing the app must behave identically."""
    assert FALLBACK_TAXONOMY.facets == taxonomy.facets
    assert FALLBACK_TAXONOMY.version == taxonomy.version
    assert FALLBACK_TAXONOMY.default_group == taxonomy.default_group
    assert FALLBACK_TAXONOMY.default_sort == taxonomy.default_sort
    assert FALLBACK_TAXONOMY.source == "<fallback>"
    assert FALLBACK_TAXONOMY.load_errors == ()


# --- loading ----------------------------------------------------------------


def test_a_missing_file_falls_back_and_says_so(tmp_path: Path) -> None:
    result = load_taxonomy(tmp_path / "nope.yaml")
    assert result.facets == FALLBACK_TAXONOMY.facets
    assert result.source == "<fallback>"
    assert len(result.load_errors) == 1


def test_unparseable_yaml_falls_back_instead_of_raising(tmp_path: Path) -> None:
    path = _write(tmp_path, "facets: [\n  - id: broken\n")
    result = load_taxonomy(path, refresh=True)
    assert result.facets == FALLBACK_TAXONOMY.facets
    assert result.load_errors


def test_an_empty_file_loads_as_an_empty_taxonomy(tmp_path: Path) -> None:
    """Empty is a valid, if useless, configuration — not a fallback trigger."""
    path = _write(tmp_path, "taxonomy_version: 1\nfacets: []\n")
    result = load_taxonomy(path, refresh=True)
    assert result.facets == ()
    assert result.default_group == "none"
    assert result.normalize({"domain": ["dmpk"]})[0] == {"domain": ["dmpk"]}


def test_env_var_overrides_the_default_path(tmp_path: Path, monkeypatch) -> None:
    path = _write(
        tmp_path,
        "facets:\n  - id: only\n    label: Only\n    values:\n      - {id: a, label: A}\n",
    )
    monkeypatch.setenv(TAXONOMY_ENV_VAR, str(path))
    assert load_taxonomy(refresh=True).facet_ids == ("only",)


def test_the_explicit_path_argument_wins_over_the_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(TAXONOMY_ENV_VAR, str(tmp_path / "ignored.yaml"))
    assert load_taxonomy(TAXONOMY_PATH, refresh=True).facet_ids[0] == "domain"


def test_the_cache_notices_an_edited_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "facets:\n  - id: one\n    label: One\n    values:\n      - {id: a, label: A}\n",
    )
    assert load_taxonomy(path).facet_ids == ("one",)
    # Same mtime+size would be a stale hit; a different size cannot be.
    path.write_text(
        "facets:\n  - id: two\n    label: Two\n    values:\n      - {id: b, label: Beta}\n",
        encoding="utf-8",
    )
    assert load_taxonomy(path).facet_ids == ("two",)


def test_refresh_bypasses_the_cache(tmp_path: Path) -> None:
    path = _write(tmp_path, "facets:\n  - id: one\n    label: One\n")
    first = load_taxonomy(path)
    assert load_taxonomy(path, refresh=True).facets == first.facets


@pytest.mark.parametrize("reserved", sorted(RESERVED_FACET_IDS))
def test_reserved_facet_ids_are_refused(tmp_path: Path, reserved: str) -> None:
    """`owner` and `readiness` are derived; `none` means 'do not group'."""
    path = _write(
        tmp_path,
        f"facets:\n  - id: {reserved}\n    label: Nope\n"
        f"  - id: keep\n    label: Keep\n",
    )
    result = load_taxonomy(path, refresh=True)
    assert result.facet_ids == ("keep",)
    assert any(reserved in e for e in result.load_errors)


def test_one_broken_facet_does_not_take_the_rest_with_it(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "facets:\n"
        "  - id: 9bad\n    label: Bad id\n"
        "  - id: nolabel\n"
        "  - id: good\n    label: Good\n"
        "    values:\n"
        "      - {id: ok, label: OK}\n"
        "      - {id: 'not ok', label: Bad}\n"
        "      - {id: nolabel}\n"
        "      - {id: ok, label: Duplicate}\n"
        "  - id: good\n    label: Duplicate facet\n",
    )
    result = load_taxonomy(path, refresh=True)
    assert result.facet_ids == ("good",)
    good = result.facet("good")
    assert good is not None and good.value_ids == ("ok",)
    # One note per dropped thing: 2 facets, 3 values, 1 duplicate facet.
    assert len(result.load_errors) == 6


def test_unknown_keys_are_ignored_with_a_note_not_a_failure(tmp_path: Path) -> None:
    """`parent:` is the one that matters — one-level hierarchy is a v2 idea,
    and the config must stay forward-compatible with it."""
    path = _write(
        tmp_path,
        "taxonomy_version: 1\n"
        "future_setting: 1\n"
        "defaults: {group_by: d, sort: name, future: 1}\n"
        "facets:\n"
        "  - id: d\n    label: D\n    parent: something\n"
        "    values:\n      - {id: a, label: A, parent: b}\n",
    )
    result = load_taxonomy(path, refresh=True)
    assert result.facet_ids == ("d",)
    assert result.facet("d").value_ids == ("a",)  # type: ignore[union-attr]
    assert sum("parent" in e for e in result.load_errors) == 2


def test_a_default_naming_a_missing_value_is_ignored(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "facets:\n  - id: d\n    label: D\n    default: ghost\n"
        "    values:\n      - {id: a, label: A}\n",
    )
    result = load_taxonomy(path, refresh=True)
    assert result.facet("d").default is None  # type: ignore[union-attr]
    assert result.load_errors


@pytest.mark.parametrize(
    "group_by, expected",
    [("ghost", "none"), ("flat", "none"), ("none", "none"), ("d", "d")],
)
def test_a_bad_default_group_falls_back_to_no_grouping(
    tmp_path: Path, group_by: str, expected: str
) -> None:
    path = _write(
        tmp_path,
        f"defaults: {{group_by: {group_by}}}\n"
        "facets:\n"
        "  - id: d\n    label: D\n"
        "  - id: flat\n    label: Flat\n    groupable: false\n",
    )
    assert load_taxonomy(path, refresh=True).default_group == expected


def test_a_bad_default_sort_falls_back_to_name(tmp_path: Path) -> None:
    path = _write(tmp_path, "defaults: {sort: by_vibes}\nfacets: []\n")
    result = load_taxonomy(path, refresh=True)
    assert result.default_sort == "name"
    assert result.load_errors


@pytest.mark.parametrize(
    "body",
    [
        "",
        "[]",
        "just a string",
        "facets: 7",
        "defaults: 7\nfacets:\n  - 3\n",
        "facets:\n  - id: d\n    label: D\n    values: 7\n    notable: 7\n",
        "facets:\n  - id: d\n    label: D\n    cardinality: lots\n    mode: ajar\n"
        "    card_chip: maybe\n",
        "taxonomy_version: many\nfacets: []\n",
    ],
)
def test_load_taxonomy_never_raises(tmp_path: Path, body: str) -> None:
    assert isinstance(load_taxonomy(_write(tmp_path, body), refresh=True), Taxonomy)


# --- normalise --------------------------------------------------------------


def test_normalize_shape_forms(taxonomy: Taxonomy) -> None:
    assert taxonomy.normalize(None)[0] == {"compliance": ["non_gxp"]}
    assert taxonomy.normalize({"domain": "dmpk"})[0]["domain"] == ["dmpk"]
    assert taxonomy.normalize(["domain:dmpk"])[0]["domain"] == ["dmpk"]
    assert taxonomy.normalize({"modality": ["a", "b", "a"]})[0]["modality"] == ["a", "b"]
    assert "orphan" not in taxonomy.normalize(["orphan"])[0]


def test_normalize_keeps_an_unknown_facet_verbatim(taxonomy: Taxonomy) -> None:
    """An admin deleting a facet at 4pm must not make tagged templates vanish."""
    tags, issues = taxonomy.normalize({"retired_facet": ["something"]})
    assert tags["retired_facet"] == ["something"]
    assert [i.code for i in issues if i.facet == "retired_facet"] == ["unknown_facet"]


def test_normalize_keeps_an_unknown_value_on_a_closed_facet(taxonomy: Taxonomy) -> None:
    tags, issues = taxonomy.normalize({"domain": "mystery", "compliance": "non_gxp"})
    assert tags["domain"] == ["mystery"]
    assert [i.code for i in issues] == ["unknown_value"]


def test_normalize_is_silent_about_new_values_on_an_open_facet(taxonomy: Taxonomy) -> None:
    tags, issues = taxonomy.normalize(
        {"domain": "pre_clinical", "compliance": "non_gxp", "modality": ["peptide"]}
    )
    assert tags["modality"] == ["peptide"]
    assert issues == []


def test_normalize_truncates_a_single_cardinality_facet(taxonomy: Taxonomy) -> None:
    tags, issues = taxonomy.normalize(
        {"domain": ["discovery", "clinical"], "compliance": "non_gxp"}
    )
    assert tags["domain"] == ["discovery"]
    (issue,) = issues
    assert issue.code == "cardinality"
    assert "Clinical" in issue.message  # names what it discarded


def test_normalize_applies_a_default_for_a_missing_required_facet(
    taxonomy: Taxonomy,
) -> None:
    tags, issues = taxonomy.normalize({"domain": "pre_clinical"})
    assert tags["compliance"] == ["non_gxp"]
    assert [i.code for i in issues] == ["missing_required"]


def test_normalize_flags_a_required_facet_with_no_default(taxonomy: Taxonomy) -> None:
    _tags, issues = taxonomy.normalize({"compliance": "non_gxp"})
    assert [(i.code, i.facet) for i in issues] == [("missing_required", "domain")]


def test_normalize_resolves_aliases_without_complaint(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "facets:\n  - id: d\n    label: D\n"
        "    values:\n      - {id: dmpk, label: DMPK, aliases: [dm_pk, adme]}\n",
    )
    result = load_taxonomy(path, refresh=True)
    tags, issues = result.normalize({"d": ["adme"]})
    assert tags == {"d": ["dmpk"]}
    assert issues == []


def test_normalize_slugifies_an_unusable_value_id(taxonomy: Taxonomy) -> None:
    tags, issues = taxonomy.normalize({"modality": ["Small Molecule"]})
    assert tags["modality"] == ["small_molecule"]
    assert any(i.code == "bad_id" for i in issues)


def test_normalize_drops_a_value_that_slugifies_to_nothing(taxonomy: Taxonomy) -> None:
    tags, _issues = taxonomy.normalize({"modality": ["!!!", "biologic"]})
    assert tags["modality"] == ["biologic"]


def test_normalize_flags_a_deprecated_value_but_keeps_it(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "facets:\n  - id: d\n    label: D\n"
        "    values:\n      - {id: old, label: Old, deprecated: true}\n",
    )
    result = load_taxonomy(path, refresh=True)
    tags, issues = result.normalize({"d": ["old"]})
    assert tags == {"d": ["old"]}
    assert [i.code for i in issues] == ["deprecated_value"]


def test_normalize_returns_configured_facets_in_config_order(taxonomy: Taxonomy) -> None:
    tags, _ = taxonomy.normalize(
        {"zz_custom": ["x"], "modality": ["biologic"], "domain": ["dmpk"]}
    )
    assert list(tags) == ["domain", "compliance", "modality", "zz_custom"]


@pytest.mark.parametrize(
    "raw",
    [None, "", [], {}, 7, 3.5, object(), {"a": None}, {"": ""}, ["", ":", "a:"], "text"],
)
def test_normalize_never_raises(taxonomy: Taxonomy, raw: object) -> None:
    tags, issues = taxonomy.normalize(raw)
    assert isinstance(tags, dict)
    assert isinstance(issues, list)


# --- validate ---------------------------------------------------------------


def test_validate_reports_without_changing_anything(taxonomy: Taxonomy) -> None:
    tags = {"domain": ["pre_clinical", "clinical"], "retired": ["x"]}
    before = {k: list(v) for k, v in tags.items()}
    codes = {i.code for i in taxonomy.validate(tags)}
    assert tags == before
    assert codes == {"cardinality", "unknown_facet", "missing_required"}


def test_validate_is_quiet_on_a_conforming_template(taxonomy: Taxonomy) -> None:
    assert taxonomy.validate(
        {
            "domain": ["pre_clinical"],
            "discipline": ["dmpk"],
            "compliance": ["non_gxp"],
            "document_class": ["technical_summary"],
            "modality": ["small_molecule"],
        }
    ) == []


@pytest.mark.parametrize("tags", [{}, {"domain": []}, {"domain": None}, {"bad id": ["x"]}])
def test_validate_never_raises(taxonomy: Taxonomy, tags: dict) -> None:
    assert isinstance(taxonomy.validate(tags), list)


def test_defaults_for_new_is_the_non_gxp_default(taxonomy: Taxonomy) -> None:
    assert taxonomy.defaults_for_new() == {"compliance": ["non_gxp"]}


# --- tokens -----------------------------------------------------------------


def test_tokens_include_an_untagged_sentinel_per_empty_configured_facet(
    taxonomy: Taxonomy,
) -> None:
    tokens = taxonomy.tokens_for({"domain": ["pre_clinical"], "custom": ["x"]})
    assert tokens == [
        "domain:pre_clinical",
        f"discipline:{UNTAGGED}",
        f"compliance:{UNTAGGED}",
        f"document_class:{UNTAGGED}",
        f"therapeutic_area:{UNTAGGED}",
        f"modality:{UNTAGGED}",
        "custom:x",
    ]


def test_every_token_round_trips_through_parse_token(taxonomy: Taxonomy) -> None:
    for token in taxonomy.tokens_for({"domain": ["dmpk"], "modality": ["a", "b"]}):
        facet, value = parse_token(token)  # type: ignore[misc]
        assert taxonomy.token(facet, value) == token


@pytest.mark.parametrize(
    "token, expected",
    [
        ("domain:dmpk", ("domain", "dmpk")),
        (f"domain:{UNTAGGED}", ("domain", UNTAGGED)),
        ("api:endpoint:v2", ("api", "endpoint:v2")),  # splits on the FIRST colon
        ("  domain : dmpk  ", ("domain", "dmpk")),
        ("orphan", None),
        ("", None),
        (":dmpk", None),
        ("domain:", None),
        ("Domain:dmpk", None),
        ("9domain:x", None),
    ],
)
def test_parse_token(token: str, expected: tuple[str, str] | None) -> None:
    assert parse_token(token) == expected


def test_the_untagged_sentinel_cannot_collide_with_a_real_value() -> None:
    assert not VALUE_ID_RE.match(UNTAGGED)


# --- labels and ordering ----------------------------------------------------


def test_label_for_falls_back_to_the_raw_id_verbatim(taxonomy: Taxonomy) -> None:
    assert taxonomy.label_for("discipline", "dmpk") == "DMPK / ADME"
    assert taxonomy.label_for("domain", "some_new_thing") == "some_new_thing"
    assert taxonomy.label_for("no_such_facet", "x") == "x"
    assert taxonomy.facet_label("no_such_facet") == "no_such_facet"


def test_untagged_labels(taxonomy: Taxonomy) -> None:
    assert taxonomy.untagged_label_for("domain") == "No domain set"
    assert taxonomy.label_for("domain", UNTAGGED) == "No domain set"
    assert taxonomy.untagged_label_for("no_such_facet") == "No no_such_facet set"


def test_a_facet_without_an_untagged_label_gets_a_derived_one(tmp_path: Path) -> None:
    path = _write(tmp_path, "facets:\n  - id: d\n    label: Domain area\n")
    assert load_taxonomy(path, refresh=True).untagged_label_for("d") == "No domain area set"


def test_sort_values_follows_config_order_then_the_stragglers(taxonomy: Taxonomy) -> None:
    assert taxonomy.sort_values(
        "domain", ["clinical", "zebra", "pre_clinical", "discovery", "apple"]
    ) == ["discovery", "pre_clinical", "clinical", "apple", "zebra"]


def test_the_untagged_sentinel_always_sorts_last(taxonomy: Taxonomy) -> None:
    assert taxonomy.sort_values("domain", [UNTAGGED, "zebra", "dmpk"])[-1] == UNTAGGED


def test_facet_order_ids_is_config_order(taxonomy: Taxonomy) -> None:
    assert taxonomy.facet_order_ids() == taxonomy.facet_ids


def test_groupable_facets_excludes_the_ungroupable(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "facets:\n  - id: a\n    label: A\n  - id: b\n    label: B\n    groupable: false\n",
    )
    assert [f.id for f in load_taxonomy(path, refresh=True).groupable_facets()] == ["a"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Small Molecule", "small_molecule"),
        ("DMPK / ADME", "dmpk_adme"),
        ("  Immunology & Inflammation  ", "immunology_inflammation"),
        ("v1.2-beta", "v1.2-beta"),
        ("!!!", ""),
        ("", ""),
        ("___a___", "a"),
        ("x" * 200, "x" * 64),
    ],
)
def test_slugify_value(text: str, expected: str) -> None:
    assert slugify_value(text) == expected


def test_slugify_always_produces_a_usable_value_id() -> None:
    for text in ("Small Molecule", "DMPK / ADME", "99 red balloons", "  ...x...  "):
        slug = slugify_value(text)
        assert not slug or VALUE_ID_RE.match(slug), slug


def test_facet_helpers() -> None:
    facet = Facet(
        id="d",
        label="D",
        cardinality="multi",
        values=(FacetValue(id="a", label="A", aliases=("alpha",)),),
    )
    assert facet.is_multi
    assert facet.value_ids == ("a",)
    assert facet.value("a") is not None and facet.value("zz") is None
    assert facet.resolve_alias("alpha") == "a"
    assert facet.resolve_alias("a") == "a"
    assert facet.resolve_alias("unknown") == "unknown"


# --- the ReportTemplate.tags schema change ----------------------------------


def test_a_template_with_no_tags_still_loads() -> None:
    template = _template()
    assert template.tags == {}
    assert template.description == ""
    assert template.status is TemplateStatus.DRAFT


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, {}),
        ("", {}),
        ([], {}),
        ({}, {}),
        ({"domain": "dmpk"}, {"domain": ["dmpk"]}),
        ({"modality": ["a", "b", "a"]}, {"modality": ["a", "b"]}),
        (["domain:dmpk", "compliance:non_gxp"], {"domain": ["dmpk"], "compliance": ["non_gxp"]}),
        (["orphan"], {}),
        ({"domain": []}, {"domain": []}),
        ({"domain": "DMPK"}, {"domain": ["dmpk"]}),
    ],
)
def test_tags_shape_normalisation(raw: object, expected: dict) -> None:
    assert _template(tags=raw).tags == expected


@pytest.mark.parametrize(
    "raw",
    [{"domain": "DMPK / ADME"}, {"Bad Facet": "x"}, {"domain": "a/b"}, 7, {"9bad": "x"}],
)
def test_a_genuinely_malformed_tag_map_is_rejected(raw: object) -> None:
    with pytest.raises(ValidationError):
        _template(tags=raw)


def test_tag_helpers_on_the_model() -> None:
    template = _template(tags={"domain": "dmpk", "modality": ["a", "b"]})
    assert template.tag_values("domain") == ["dmpk"]
    assert template.tag_values("absent") == []
    assert template.tag_tokens() == ["domain:dmpk", "modality:a", "modality:b"]


def test_extra_forbid_still_forbids_undeclared_keys() -> None:
    with pytest.raises(ValidationError):
        _template(nonsense=1)


def test_tags_survive_a_json_round_trip() -> None:
    template = _template(tags={"domain": "dmpk"}, description="A description.")
    assert ReportTemplate.model_validate_json(template.model_dump_json()) == template


def test_the_shipped_json_library_still_validates() -> None:
    """These payloads predate `tags`/`description`; `extra='forbid'` only ever
    rejected *undeclared* keys, so a declared-and-defaulted field is additive."""
    paths = sorted((REPO_ROOT / "templates" / "library").glob("*.json"))
    assert paths
    for path in paths:
        template = ReportTemplate.model_validate_json(path.read_text(encoding="utf-8"))
        assert template.tags == {}
        assert template.description == ""


# --- the six migrated templates ---------------------------------------------


def test_the_six_migrated_templates_are_all_present() -> None:
    missing = [p.name for p in MIGRATED_TEMPLATES if not p.exists()]
    assert not missing, f"migrated templates missing from disk: {missing}"


@pytest.mark.parametrize("path", REAL_TEMPLATES, ids=lambda p: p.stem)
def test_every_real_template_is_tagged_and_conforms(path: Path, taxonomy: Taxonomy) -> None:
    tags = load_report_doc(path).tags
    assert tags, f"{path.name} carries no tags"
    assert taxonomy.validate(tags) == [], f"{path.name} does not conform"


@pytest.mark.parametrize("path", MIGRATED_TEMPLATES, ids=lambda p: p.stem)
def test_the_tags_block_sits_between_owner_and_inputs(path: Path) -> None:
    """The migration inserts a block and changes nothing else.

    Scoped to the six migrated files: the serialiser additionally emits an
    `updated:` key between `owner:` and `tags:`, so this exact layout is a
    fact about the hand-migrated files, not about every template. The
    serialiser's own layout is pinned by the round-trip tests instead.
    """
    text = path.read_text(encoding="utf-8")
    assert re.search(r"\nowner: .*\n\ntags:\n(?:  \S.*\n)+\ninputs:\n", text), path.name


@pytest.mark.parametrize(
    "name, expected_domain, expected_discipline",
    [
        (
            "candidate_selection_dossier",
            "pre_clinical",
            ["pharmacology", "dmpk", "nonclinical_safety", "developability"],
        ),
        (
            "compound_profile_onepager",
            "pre_clinical",
            ["pharmacology", "dmpk", "nonclinical_safety"],
        ),
        ("dmpk_adme_summary", "pre_clinical", ["dmpk"]),
        ("nonclinical_safety_summary", "pre_clinical", ["nonclinical_safety"]),
        (
            "ib_nonclinical_sections",
            "pre_clinical",
            ["pharmacology", "dmpk", "nonclinical_safety"],
        ),
        ("target_assessment", "discovery", ["target_sciences"]),
    ],
)
def test_the_agreed_domain_per_template(
    name: str, expected_domain: str, expected_discipline: list[str]
) -> None:
    """`domain` is the broad bucket; the fine-grained area is on `discipline`.

    Both halves are pinned here: with five of the six filed under Pre-Clinical,
    `domain` alone no longer tells these templates apart — `discipline` is what
    carries the distinction now.
    """
    tags = load_report_doc(TEMPLATES_DIR / f"{name}.md").tags
    assert tags["domain"] == [expected_domain]
    assert tags["discipline"] == expected_discipline


def test_therapeutic_area_is_left_unset_on_the_migrated_templates() -> None:
    """A facet every template shares is useless as a filter on day one.

    This is a decision about the MIGRATION. Templates authored in the app are
    free to set a therapeutic area — that is what the facet is for.
    """
    for path in MIGRATED_TEMPLATES:
        assert "therapeutic_area" not in load_report_doc(path).tags


@pytest.mark.parametrize("name", ["README", "SKILL.template"])
def test_the_non_templates_were_left_alone(name: str) -> None:
    text = (TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")
    assert "\ntags:\n" not in text


# --- GxP policy (the configuration half) ------------------------------------


def test_non_gxp_is_the_default_and_is_listed_first(taxonomy: Taxonomy) -> None:
    compliance = taxonomy.facet("compliance")
    assert compliance is not None
    assert compliance.default == "non_gxp"
    assert compliance.value_ids[0] == "non_gxp"
    assert taxonomy.defaults_for_new() == {"compliance": ["non_gxp"]}


def test_gxp_is_selectable(taxonomy: Taxonomy) -> None:
    """Other GSK teams do work under GxP; the label has to be available."""
    compliance = taxonomy.facet("compliance")
    assert compliance is not None
    assert "gxp" in compliance.value_ids
    assert taxonomy.label_for("compliance", "gxp") == "GxP"


@pytest.mark.parametrize("path", REAL_TEMPLATES, ids=lambda p: p.stem)
def test_no_template_is_ever_auto_tagged_gxp(path: Path) -> None:
    assert load_report_doc(path).tags.get("compliance") == ["non_gxp"]


def test_the_gxp_value_is_inert_in_the_taxonomy(taxonomy: Taxonomy) -> None:
    """Selecting GxP is a label. It must change nothing: same normalisation,
    same issue list, same chip treatment, same ordering weight as Non-GxP."""
    base = {"domain": ["pre_clinical"], "document_class": ["technical_summary"]}
    non_gxp, issues_non = taxonomy.normalize({**base, "compliance": ["non_gxp"]})
    gxp, issues_gxp = taxonomy.normalize({**base, "compliance": ["gxp"]})

    assert issues_non == issues_gxp == []
    assert {k: v for k, v in non_gxp.items() if k != "compliance"} == {
        k: v for k, v in gxp.items() if k != "compliance"
    }
    assert taxonomy.validate(non_gxp) == taxonomy.validate(gxp) == []

    compliance = taxonomy.facet("compliance")
    assert compliance is not None
    a, b = compliance.value("non_gxp"), compliance.value("gxp")
    assert a is not None and b is not None
    assert a.deprecated == b.deprecated
    # Neither value carries any weight beyond its position in the list.
    assert taxonomy.value_sort_key("compliance", "non_gxp")[0] == 0
    assert taxonomy.value_sort_key("compliance", "gxp")[0] == 1


def test_notable_is_the_only_place_gxp_appears_in_logic(taxonomy: Taxonomy) -> None:
    """`notable` is a generically-compared list of value ids; it controls
    nothing but whether a neutral chip renders."""
    compliance = taxonomy.facet("compliance")
    assert compliance is not None
    assert compliance.card_chip == "notable"
    assert compliance.notable == ("gxp",)


def test_no_gxp_branch_in_the_files_this_engineer_owns() -> None:
    banned_logic = re.compile(r"""["']gxp["']\s*(==|!=|in\b)|(==|!=)\s*["']gxp["']""")
    banned_words = re.compile(
        r"\b(21 ?CFR|Part ?11|change control|computer system validation"
        r"|\bCSV\b|revalidat|\bvalidated mode\b)",
        re.I,
    )
    owned = [
        REPO_ROOT / "services" / "template_service" / "taxonomy.py",
        REPO_ROOT / "report-templates" / "taxonomy.yaml",
        REPO_ROOT / "shared" / "schemas" / "template.py",
        *REAL_TEMPLATES,
    ]
    for path in owned:
        text = path.read_text(encoding="utf-8")
        assert not banned_logic.search(text), path
        assert not banned_words.search(text), path


def test_the_compliance_facet_description_stays_neutral(taxonomy: Taxonomy) -> None:
    compliance = taxonomy.facet("compliance")
    assert compliance is not None
    assert compliance.description == (
        "How this report template is labelled. This is a label only; it does "
        "not change how the application behaves."
    )
