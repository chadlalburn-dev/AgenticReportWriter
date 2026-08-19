"""Faceted tag taxonomy for report templates.

A *facet* is one independent axis of classification (domain area, compliance
label, document class, ...). A template carries a map of facet id -> ordered
value ids; the meaning of those ids lives entirely in
``report-templates/taxonomy.yaml``, which is edited by hand and never written
by the application.

Nothing in this module — and nothing in the application that uses it — names a
particular facet or a particular value. Adding a facet or a value is a config
edit, not a code change.

Two forms of the same thing appear throughout:

* **storage form** ``{"domain": ["dmpk"], "compliance": ["non_gxp"]}`` — what a
  template persists.
* **token form** ``"domain:dmpk"`` — derived, used in query strings and markup
  only. Never persisted. ``UNTAGGED`` (``"~none"``) is the sentinel value id for
  "this facet is not set on this template".

Imports: stdlib + pyyaml ONLY. This module never imports ``shared.schemas``,
``services.api_gateway``, or anything that touches the filesystem beyond the
taxonomy file itself.
"""

from __future__ import annotations

import dataclasses
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

# --- module constants -------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
TAXONOMY_PATH: Path = REPO_ROOT / "report-templates" / "taxonomy.yaml"
TAXONOMY_ENV_VAR: str = "RG_TAG_TAXONOMY"

#: Value-id sentinel meaning "this facet is not set". `~` is RFC 3986
#: unreserved (so it needs no escaping in a URL) and cannot collide with a
#: real value id, which must start with [a-z0-9].
UNTAGGED: str = "~none"

#: Facet ids the taxonomy file may not claim. `owner` and `readiness` are
#: derived facets built from template metadata; `none` means "do not group".
RESERVED_FACET_IDS: frozenset[str] = frozenset({"owner", "readiness", "none"})

FACET_ID_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
VALUE_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

Cardinality = Literal["single", "multi"]
FacetMode = Literal["closed", "open"]
CardChip = Literal["always", "notable", "never"]
IssueCode = Literal[
    "unknown_facet",
    "unknown_value",
    "missing_required",
    "cardinality",
    "bad_id",
    "deprecated_value",
]

SORT_KEYS: tuple[str, ...] = ("name", "updated", "sections", "ready", "owner")

_CARDINALITIES: tuple[str, ...] = ("single", "multi")
_MODES: tuple[str, ...] = ("closed", "open")
_CARD_CHIPS: tuple[str, ...] = ("always", "notable", "never")

_TOP_KEYS = frozenset({"taxonomy_version", "updated", "defaults", "facets"})
_DEFAULTS_KEYS = frozenset({"group_by", "sort"})
_FACET_KEYS = frozenset(
    {
        "id",
        "label",
        "description",
        "cardinality",
        "required",
        "mode",
        "default",
        "groupable",
        "card_chip",
        "notable",
        "untagged_label",
        "values",
    }
)
_VALUE_KEYS = frozenset({"id", "label", "description", "deprecated", "aliases"})

#: Sort index given to a value that is not in the config at all.
_UNCONFIGURED_SORT_INDEX = 9999


# --- data -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FacetValue:
    """One selectable value within a facet."""

    id: str
    label: str
    description: str = ""
    deprecated: bool = False
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Facet:
    """One axis of classification."""

    id: str
    label: str
    description: str = ""
    cardinality: Cardinality = "single"
    required: bool = False
    mode: FacetMode = "closed"
    default: str | None = None
    groupable: bool = True
    card_chip: CardChip = "never"
    notable: tuple[str, ...] = ()
    untagged_label: str = ""
    values: tuple[FacetValue, ...] = ()

    @property
    def is_multi(self) -> bool:
        return self.cardinality == "multi"

    @property
    def value_ids(self) -> tuple[str, ...]:
        return tuple(v.id for v in self.values)

    def value(self, value_id: str) -> FacetValue | None:
        """The configured value, or None when this facet does not declare it."""
        for v in self.values:
            if v.id == value_id:
                return v
        return None

    def resolve_alias(self, value_id: str) -> str:
        """Map an inbound alias onto its canonical id.

        Returns the input unchanged when it is already a configured id, or when
        no configured value claims it as an alias.
        """
        for v in self.values:
            if v.id == value_id:
                return v.id
        for v in self.values:
            if value_id in v.aliases:
                return v.id
        return value_id


@dataclass(frozen=True, slots=True)
class TagIssue:
    """Something worth telling a human about one template's tags."""

    severity: Literal["warn", "error"]
    code: IssueCode
    facet: str
    value: str | None
    message: str


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """The loaded taxonomy configuration, plus every rule that reads it."""

    version: int
    default_group: str
    default_sort: str
    facets: tuple[Facet, ...]
    load_errors: tuple[str, ...] = ()
    source: str = ""

    # --- lookup -------------------------------------------------------------

    def facet(self, facet_id: str) -> Facet | None:
        for f in self.facets:
            if f.id == facet_id:
                return f
        return None

    @property
    def facet_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.facets)

    def groupable_facets(self) -> tuple[Facet, ...]:
        return tuple(f for f in self.facets if f.groupable)

    def facet_label(self, facet_id: str) -> str:
        """The configured label, else the facet id verbatim."""
        f = self.facet(facet_id)
        return f.label if f is not None else facet_id

    def label_for(self, facet_id: str, value_id: str) -> str:
        """Configured label, else the raw value id VERBATIM (never prettified).

        The `UNTAGGED` sentinel resolves to the facet's untagged label.
        """
        if value_id == UNTAGGED:
            return self.untagged_label_for(facet_id)
        f = self.facet(facet_id)
        if f is not None:
            v = f.value(f.resolve_alias(value_id))
            if v is not None:
                return v.label
        return value_id

    def untagged_label_for(self, facet_id: str) -> str:
        f = self.facet(facet_id)
        if f is None:
            return f"No {facet_id} set"
        if f.untagged_label:
            return f.untagged_label
        return f"No {f.label.lower()} set"

    # --- normalisation / validation ----------------------------------------

    def normalize(self, raw: object) -> tuple[dict[str, list[str]], list[TagIssue]]:
        """Shape-normalise, then apply this taxonomy's rules.

        Shape normalisation follows the same rules as the schema's
        `_coerce_tag_map` (mapping or bare token list in, facet -> value-list
        out) except that a malformed id is slugified rather than rejected —
        this function NEVER raises.

        Taxonomy rules applied afterwards: alias resolution, single-cardinality
        truncation, and in-memory application of a facet `default` when a
        required facet is unset.

        Unknown facets and unknown values are PRESERVED verbatim. An admin who
        deletes a facet from the config does not make tagged templates vanish.
        """
        shaped, issues = self._shape(raw)

        resolved: dict[str, list[str]] = {}
        for facet_id, value_ids in shaped.items():
            f = self.facet(facet_id)
            if f is None:
                resolved[facet_id] = list(value_ids)
                issues.append(
                    TagIssue(
                        "warn",
                        "unknown_facet",
                        facet_id,
                        None,
                        f"{facet_id!r} is not a facet in the taxonomy. Its values "
                        f"are kept on the template but cannot be filtered on.",
                    )
                )
                continue

            kept: list[str] = []
            for value_id in value_ids:
                canonical = f.resolve_alias(value_id)
                if canonical not in kept:
                    kept.append(canonical)

            if not f.is_multi and len(kept) > 1:
                discarded = kept[1:]
                issues.append(
                    TagIssue(
                        "warn",
                        "cardinality",
                        f.id,
                        kept[0],
                        f"{f.label} takes a single value. Kept "
                        f"{self.label_for(f.id, kept[0])!r}; ignored "
                        + ", ".join(repr(self.label_for(f.id, d)) for d in discarded)
                        + ".",
                    )
                )
                kept = kept[:1]

            for value_id in kept:
                v = f.value(value_id)
                if v is None:
                    if f.mode == "closed":
                        issues.append(
                            TagIssue(
                                "warn",
                                "unknown_value",
                                f.id,
                                value_id,
                                f"{value_id!r} is not one of the values listed for "
                                f"{f.label}. It is kept on the template.",
                            )
                        )
                elif v.deprecated:
                    issues.append(
                        TagIssue(
                            "warn",
                            "deprecated_value",
                            f.id,
                            value_id,
                            f"{v.label} is no longer offered for {f.label}. It is "
                            f"kept on the template.",
                        )
                    )

            resolved[facet_id] = kept

        # Required facets: apply the configured default in memory only.
        for f in self.facets:
            if not f.required or resolved.get(f.id):
                continue
            if f.default:
                resolved[f.id] = [f.default]
                issues.append(
                    TagIssue(
                        "warn",
                        "missing_required",
                        f.id,
                        f.default,
                        f"{f.label} was not set; showing "
                        f"{self.label_for(f.id, f.default)!r} for now.",
                    )
                )
            else:
                issues.append(
                    TagIssue(
                        "warn",
                        "missing_required",
                        f.id,
                        None,
                        f"{f.label} has not been set.",
                    )
                )

        return self._in_config_order(resolved), issues

    def validate(self, tags: dict[str, list[str]]) -> list[TagIssue]:
        """Report issues without changing anything. NEVER raises."""
        issues: list[TagIssue] = []
        tags = tags or {}

        for facet_id, value_ids in tags.items():
            values = list(value_ids or ())
            if not FACET_ID_RE.match(str(facet_id)):
                issues.append(
                    TagIssue(
                        "warn",
                        "bad_id",
                        str(facet_id),
                        None,
                        f"{facet_id!r} is not a usable facet name.",
                    )
                )
                continue
            f = self.facet(facet_id)
            if f is None:
                issues.append(
                    TagIssue(
                        "warn",
                        "unknown_facet",
                        facet_id,
                        None,
                        f"{facet_id!r} is not a facet in the taxonomy. Its values "
                        f"are kept on the template but cannot be filtered on.",
                    )
                )
                continue

            if not f.is_multi and len(values) > 1:
                issues.append(
                    TagIssue(
                        "warn",
                        "cardinality",
                        f.id,
                        values[0],
                        f"{f.label} takes a single value, but "
                        f"{len(values)} are set: "
                        + ", ".join(repr(self.label_for(f.id, v)) for v in values)
                        + ".",
                    )
                )

            for value_id in values:
                if not VALUE_ID_RE.match(str(value_id)):
                    issues.append(
                        TagIssue(
                            "warn",
                            "bad_id",
                            f.id,
                            str(value_id),
                            f"{value_id!r} is not a usable value name for {f.label}.",
                        )
                    )
                    continue
                canonical = f.resolve_alias(value_id)
                v = f.value(canonical)
                if v is None:
                    if f.mode == "closed":
                        issues.append(
                            TagIssue(
                                "warn",
                                "unknown_value",
                                f.id,
                                value_id,
                                f"{value_id!r} is not one of the values listed for "
                                f"{f.label}. It is kept on the template.",
                            )
                        )
                elif v.deprecated:
                    issues.append(
                        TagIssue(
                            "warn",
                            "deprecated_value",
                            f.id,
                            canonical,
                            f"{v.label} is no longer offered for {f.label}. It is "
                            f"kept on the template.",
                        )
                    )

        for f in self.facets:
            if f.required and not tags.get(f.id):
                issues.append(
                    TagIssue(
                        "warn",
                        "missing_required",
                        f.id,
                        None,
                        f"{f.label} has not been set.",
                    )
                )

        return issues

    def defaults_for_new(self) -> dict[str, list[str]]:
        """The tag map a brand-new template starts from."""
        out: dict[str, list[str]] = {}
        for f in self.facets:
            if f.default:
                out[f.id] = [f.default]
        return out

    # --- tokens -------------------------------------------------------------

    def token(self, facet_id: str, value_id: str) -> str:
        return f"{facet_id}:{value_id}"

    def tokens_for(self, tags: dict[str, list[str]]) -> list[str]:
        """Every `facet:value` token, plus a `facet:~none` sentinel for every
        configured facet the map leaves empty.

        Order: configured facets in config order first, then unknown facets in
        map order.
        """
        tags = tags or {}
        out: list[str] = []
        for f in self.facets:
            values = [v for v in (tags.get(f.id) or ()) if v]
            if values:
                out.extend(self.token(f.id, v) for v in values)
            else:
                out.append(self.token(f.id, UNTAGGED))
        configured = set(self.facet_ids)
        for facet_id, values in tags.items():
            if facet_id in configured:
                continue
            out.extend(self.token(facet_id, v) for v in (values or ()) if v)
        return out

    # --- ordering -----------------------------------------------------------

    def facet_order_ids(self) -> tuple[str, ...]:
        """Config order. This is what the writer serialises `tags:` in."""
        return self.facet_ids

    def value_sort_key(self, facet_id: str, value_id: str) -> tuple[int, str]:
        """(config index, casefolded label).

        Values that are not in the config sort after those that are, then
        alphabetically. The untagged sentinel always sorts last.
        """
        if value_id == UNTAGGED:
            return (_UNCONFIGURED_SORT_INDEX + 1, "")
        f = self.facet(facet_id)
        if f is not None:
            canonical = f.resolve_alias(value_id)
            for index, v in enumerate(f.values):
                if v.id == canonical:
                    return (index, v.label.casefold())
        return (_UNCONFIGURED_SORT_INDEX, str(value_id).casefold())

    def sort_values(self, facet_id: str, value_ids: Iterable[str]) -> list[str]:
        return sorted(value_ids, key=lambda v: self.value_sort_key(facet_id, v))

    # --- internals ----------------------------------------------------------

    def _in_config_order(self, tags: dict[str, list[str]]) -> dict[str, list[str]]:
        """Configured facets in config order, then unknown facets in map order."""
        out: dict[str, list[str]] = {}
        for f in self.facets:
            if f.id in tags:
                out[f.id] = tags[f.id]
        for facet_id, values in tags.items():
            if facet_id not in out:
                out[facet_id] = values
        return out

    def _shape(self, raw: object) -> tuple[dict[str, list[str]], list[TagIssue]]:
        """Permissive shape normalisation. Never raises; slugifies bad ids."""
        issues: list[TagIssue] = []
        if raw is None or raw == "" or raw == [] or raw == {}:
            return {}, issues

        pairs: list[tuple[Any, list[Any]]] = []
        if isinstance(raw, (list, tuple)):
            acc: dict[str, list[Any]] = {}
            for element in raw:
                token = str(element).strip()
                facet, sep, val = token.partition(":")
                if not sep:
                    continue
                acc.setdefault(facet.strip(), []).append(val.strip())
            pairs = list(acc.items())
        elif isinstance(raw, dict):
            for key, value in raw.items():
                items = list(value) if isinstance(value, (list, tuple)) else [value]
                pairs.append((key, items))
        else:
            issues.append(
                TagIssue(
                    "warn",
                    "bad_id",
                    "",
                    None,
                    "Tags could not be read: expected a list of values per facet.",
                )
            )
            return {}, issues

        out: dict[str, list[str]] = {}
        for facet_key, items in pairs:
            facet = str(facet_key).strip().lower()
            if not FACET_ID_RE.match(facet):
                slug = _slugify_facet(facet)
                if not slug:
                    issues.append(
                        TagIssue(
                            "warn",
                            "bad_id",
                            facet,
                            None,
                            f"{facet_key!r} is not a usable facet name; it was dropped.",
                        )
                    )
                    continue
                issues.append(
                    TagIssue(
                        "warn",
                        "bad_id",
                        slug,
                        None,
                        f"{facet_key!r} is not a usable facet name; read as {slug!r}.",
                    )
                )
                facet = slug

            bucket = out.setdefault(facet, [])
            for item in items:
                if item is None:
                    continue
                value = str(item).strip().lower()
                if not value:
                    continue
                if not VALUE_ID_RE.match(value):
                    slug = slugify_value(value)
                    if not slug:
                        issues.append(
                            TagIssue(
                                "warn",
                                "bad_id",
                                facet,
                                value,
                                f"{item!r} is not a usable value name; it was dropped.",
                            )
                        )
                        continue
                    issues.append(
                        TagIssue(
                            "warn",
                            "bad_id",
                            facet,
                            slug,
                            f"{item!r} is not a usable value name; read as {slug!r}.",
                        )
                    )
                    value = slug
                if value not in bucket:
                    bucket.append(value)

        return out, issues


# --- loading ----------------------------------------------------------------


def _fallback_taxonomy() -> Taxonomy:
    """The taxonomy used when report-templates/taxonomy.yaml is missing or
    unparseable. It carries exactly the facets and values that the shipped file
    does, so behaviour is identical either way (pinned by a test).
    """
    return Taxonomy(
        version=1,
        default_group="domain",
        default_sort="name",
        source="<fallback>",
        facets=(
            Facet(
                id="domain",
                label="Domain area",
                description="The broad area this report is filed under.",
                cardinality="single",
                required=True,
                mode="closed",
                groupable=True,
                card_chip="always",
                untagged_label="No domain set",
                values=(
                    FacetValue(id="discovery", label="Discovery"),
                    FacetValue(
                        id="pre_clinical",
                        label="Pre-Clinical",
                        description="Preclinical / nonclinical research reports.",
                    ),
                    FacetValue(id="clinical", label="Clinical"),
                    FacetValue(id="regulatory_writing", label="Regulatory Writing"),
                    FacetValue(id="cmc", label="CMC / Pharmaceutical Development"),
                    FacetValue(id="data_science", label="Data Science"),
                ),
            ),
            Facet(
                id="discipline",
                label="Discipline",
                description="The scientific sub-area(s) this report covers.",
                cardinality="multi",
                required=False,
                mode="open",
                groupable=True,
                card_chip="always",
                untagged_label="No discipline set",
                values=(
                    FacetValue(id="target_sciences", label="Target Sciences"),
                    FacetValue(id="pharmacology", label="Pharmacology"),
                    FacetValue(id="dmpk", label="DMPK / ADME"),
                    FacetValue(id="nonclinical_safety", label="Nonclinical Safety"),
                    FacetValue(id="developability", label="Developability"),
                    FacetValue(id="biomarkers", label="Biomarkers"),
                    FacetValue(id="translational", label="Translational"),
                ),
            ),
            Facet(
                id="compliance",
                label="Compliance",
                description=(
                    "How this report template is labelled. This is a label only; it "
                    "does not change how the application behaves."
                ),
                cardinality="single",
                required=True,
                mode="closed",
                default="non_gxp",
                groupable=True,
                card_chip="notable",
                notable=("gxp",),
                untagged_label="No compliance label",
                values=(
                    FacetValue(
                        id="non_gxp",
                        label="Non-GxP",
                        description="Research and discovery work outside GxP scope.",
                    ),
                    FacetValue(
                        id="gxp",
                        label="GxP",
                        description="Work a team runs under GxP.",
                    ),
                ),
            ),
            Facet(
                id="document_class",
                label="Document class",
                description="What kind of document this is and who reads it.",
                cardinality="single",
                required=False,
                mode="closed",
                groupable=True,
                card_chip="never",
                untagged_label="No document class set",
                values=(
                    FacetValue(id="internal_decision", label="Internal decision document"),
                    FacetValue(id="technical_summary", label="Technical summary"),
                    FacetValue(
                        id="regulatory_component", label="Regulatory submission component"
                    ),
                    FacetValue(id="share_out", label="Share-out / one-pager"),
                    FacetValue(id="exploratory", label="Exploratory analysis"),
                ),
            ),
            Facet(
                id="therapeutic_area",
                label="Therapeutic area",
                description="Therapeutic area(s) this template is written for.",
                cardinality="multi",
                required=False,
                mode="open",
                groupable=True,
                card_chip="always",
                untagged_label="No therapeutic area set",
                values=(
                    FacetValue(id="respiratory", label="Respiratory"),
                    FacetValue(
                        id="immunology_inflammation", label="Immunology & Inflammation"
                    ),
                    FacetValue(id="oncology", label="Oncology"),
                    FacetValue(id="infectious_diseases", label="Infectious Diseases"),
                    FacetValue(id="vaccines", label="Vaccines"),
                    FacetValue(id="hepatology", label="Hepatology"),
                    FacetValue(id="neurology", label="Neurology"),
                    FacetValue(id="cross_ta", label="Cross-TA / platform"),
                ),
            ),
            Facet(
                id="modality",
                label="Modality",
                description="Molecule type(s) this template's content assumes.",
                cardinality="multi",
                required=False,
                mode="open",
                groupable=True,
                card_chip="never",
                untagged_label="No modality set",
                values=(
                    FacetValue(id="small_molecule", label="Small molecule"),
                    FacetValue(id="oligonucleotide", label="Oligonucleotide"),
                    FacetValue(id="biologic", label="Biologic / mAb"),
                    FacetValue(id="vaccine", label="Vaccine"),
                    FacetValue(id="cell_gene_therapy", label="Cell & gene therapy"),
                ),
            ),
        ),
    )


FALLBACK_TAXONOMY: Taxonomy = _fallback_taxonomy()

_LOCK = threading.RLock()

#: resolved path -> (stamp, taxonomy). Keyed by path rather than by stamp so
#: repeated edits to one file cannot grow the cache without bound.
_CACHE: dict[str, tuple[tuple[int, int] | None, Taxonomy]] = {}


def load_taxonomy(path: str | Path | None = None, *, refresh: bool = False) -> Taxonomy:
    """Load the taxonomy configuration.

    Resolution order:
      1. the `path` argument
      2. os.environ["RG_TAG_TAXONOMY"]
      3. TAXONOMY_PATH (report-templates/taxonomy.yaml)
      4. FALLBACK_TAXONOMY

    Cached on (resolved path, mtime, size); `refresh=True` bypasses the cache.
    Thread-safe. NEVER raises.
    """
    if path is not None:
        resolved = Path(path)
    else:
        env_value = os.environ.get(TAXONOMY_ENV_VAR, "").strip()
        resolved = Path(env_value) if env_value else TAXONOMY_PATH

    key = str(resolved)
    try:
        stat = resolved.stat()
        stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None

    with _LOCK:
        if not refresh:
            cached = _CACHE.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        taxonomy = _load_uncached(resolved)
        _CACHE[key] = (stamp, taxonomy)
        return taxonomy


def _load_uncached(path: Path) -> Taxonomy:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return dataclasses.replace(
            FALLBACK_TAXONOMY,
            load_errors=(
                f"Could not read the tag taxonomy at {path} ({exc.strerror or exc}); "
                f"using the built-in one.",
            ),
        )
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return dataclasses.replace(
            FALLBACK_TAXONOMY,
            load_errors=(
                f"The tag taxonomy at {path} could not be read as YAML ({exc}); "
                f"using the built-in one.",
            ),
        )
    if not isinstance(raw, dict):
        return dataclasses.replace(
            FALLBACK_TAXONOMY,
            load_errors=(
                f"The tag taxonomy at {path} is not a mapping; using the built-in one.",
            ),
        )
    return _parse_taxonomy(raw, source=str(path))


def _parse_taxonomy(raw: dict[str, Any], *, source: str) -> Taxonomy:
    errors: list[str] = []

    for key in raw:
        if key not in _TOP_KEYS:
            errors.append(f"Ignored unknown taxonomy setting {key!r}.")

    version = raw.get("taxonomy_version", 1)
    try:
        version = int(version)
    except (TypeError, ValueError):
        errors.append(f"taxonomy_version {version!r} is not a number; assumed 1.")
        version = 1

    facets: list[Facet] = []
    seen_facet_ids: set[str] = set()
    raw_facets = raw.get("facets") or []
    if not isinstance(raw_facets, (list, tuple)):
        errors.append("'facets' is not a list; no facets were loaded.")
        raw_facets = []

    for entry in raw_facets:
        facet = _parse_facet(entry, seen_facet_ids, errors)
        if facet is not None:
            facets.append(facet)
            seen_facet_ids.add(facet.id)

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append("'defaults' is not a mapping; using the built-in defaults.")
        defaults = {}
    for key in defaults:
        if key not in _DEFAULTS_KEYS:
            errors.append(f"Ignored unknown setting defaults.{key}.")

    group_by = str(defaults.get("group_by", "none") or "none").strip().lower()
    if group_by != "none":
        match = next((f for f in facets if f.id == group_by), None)
        if match is None:
            errors.append(
                f"defaults.group_by {group_by!r} is not a facet; grouping is off by default."
            )
            group_by = "none"
        elif not match.groupable:
            errors.append(
                f"defaults.group_by {group_by!r} is not groupable; grouping is off by default."
            )
            group_by = "none"

    sort = str(defaults.get("sort", "name") or "name").strip().lower()
    if sort not in SORT_KEYS:
        errors.append(f"defaults.sort {sort!r} is not a sort option; using 'name'.")
        sort = "name"

    return Taxonomy(
        version=version,
        default_group=group_by,
        default_sort=sort,
        facets=tuple(facets),
        load_errors=tuple(errors),
        source=source,
    )


def _parse_facet(entry: Any, seen: set[str], errors: list[str]) -> Facet | None:
    if not isinstance(entry, dict):
        errors.append("Ignored a facet that is not a mapping.")
        return None

    for key in entry:
        if key not in _FACET_KEYS:
            errors.append(f"Ignored unknown facet setting {key!r}.")

    facet_id = str(entry.get("id", "") or "").strip().lower()
    label = str(entry.get("label", "") or "").strip()
    if not facet_id or not FACET_ID_RE.match(facet_id):
        errors.append(f"Dropped a facet with an unusable id {entry.get('id')!r}.")
        return None
    if not label:
        errors.append(f"Dropped facet {facet_id!r}: it has no label.")
        return None
    if facet_id in RESERVED_FACET_IDS:
        errors.append(
            f"Dropped facet {facet_id!r}: that name is reserved by the application."
        )
        return None
    if facet_id in seen:
        errors.append(f"Dropped a second facet named {facet_id!r}.")
        return None

    cardinality = str(entry.get("cardinality", "single") or "single").strip().lower()
    if cardinality not in _CARDINALITIES:
        errors.append(
            f"Facet {facet_id!r}: cardinality {cardinality!r} is not understood; "
            f"treated as 'single'."
        )
        cardinality = "single"

    mode = str(entry.get("mode", "closed") or "closed").strip().lower()
    if mode not in _MODES:
        errors.append(
            f"Facet {facet_id!r}: mode {mode!r} is not understood; treated as 'closed'."
        )
        mode = "closed"

    card_chip = str(entry.get("card_chip", "never") or "never").strip().lower()
    if card_chip not in _CARD_CHIPS:
        errors.append(
            f"Facet {facet_id!r}: card_chip {card_chip!r} is not understood; "
            f"treated as 'never'."
        )
        card_chip = "never"

    values: list[FacetValue] = []
    seen_values: set[str] = set()
    raw_values = entry.get("values") or []
    if not isinstance(raw_values, (list, tuple)):
        errors.append(f"Facet {facet_id!r}: 'values' is not a list; it has no values.")
        raw_values = []
    for raw_value in raw_values:
        value = _parse_value(raw_value, facet_id, seen_values, errors)
        if value is not None:
            values.append(value)
            seen_values.add(value.id)

    default = entry.get("default")
    default_id = str(default).strip().lower() if default not in (None, "") else None
    if default_id is not None and default_id not in seen_values:
        errors.append(
            f"Facet {facet_id!r}: default {default!r} is not one of its values; ignored."
        )
        default_id = None

    notable_raw = entry.get("notable") or []
    if not isinstance(notable_raw, (list, tuple)):
        errors.append(f"Facet {facet_id!r}: 'notable' is not a list; ignored.")
        notable_raw = []
    notable = tuple(
        str(n).strip().lower() for n in notable_raw if str(n).strip()
    )

    return Facet(
        id=facet_id,
        label=label,
        description=_flatten(entry.get("description", "")),
        cardinality=cardinality,  # type: ignore[arg-type]
        required=bool(entry.get("required", False)),
        mode=mode,  # type: ignore[arg-type]
        default=default_id,
        groupable=bool(entry.get("groupable", True)),
        card_chip=card_chip,  # type: ignore[arg-type]
        notable=notable,
        untagged_label=str(entry.get("untagged_label", "") or "").strip(),
        values=tuple(values),
    )


def _parse_value(
    entry: Any, facet_id: str, seen: set[str], errors: list[str]
) -> FacetValue | None:
    if not isinstance(entry, dict):
        errors.append(f"Facet {facet_id!r}: ignored a value that is not a mapping.")
        return None

    for key in entry:
        if key not in _VALUE_KEYS:
            errors.append(f"Facet {facet_id!r}: ignored unknown value setting {key!r}.")

    value_id = str(entry.get("id", "") or "").strip().lower()
    label = str(entry.get("label", "") or "").strip()
    if not value_id or not VALUE_ID_RE.match(value_id):
        errors.append(
            f"Facet {facet_id!r}: dropped a value with an unusable id {entry.get('id')!r}."
        )
        return None
    if not label:
        errors.append(f"Facet {facet_id!r}: dropped value {value_id!r}; it has no label.")
        return None
    if value_id in seen:
        errors.append(f"Facet {facet_id!r}: dropped a second value named {value_id!r}.")
        return None

    aliases_raw = entry.get("aliases") or []
    if not isinstance(aliases_raw, (list, tuple)):
        errors.append(f"Facet {facet_id!r}: value {value_id!r} has a bad 'aliases'; ignored.")
        aliases_raw = []
    aliases = tuple(
        str(a).strip().lower() for a in aliases_raw if str(a).strip()
    )

    return FacetValue(
        id=value_id,
        label=label,
        description=_flatten(entry.get("description", "")),
        deprecated=bool(entry.get("deprecated", False)),
        aliases=aliases,
    )


# --- helpers ----------------------------------------------------------------


def parse_token(token: str) -> tuple[str, str] | None:
    """`'domain:dmpk'` -> `('domain', 'dmpk')`; `'domain:~none'` ->
    `('domain', '~none')`.

    Splits on the FIRST ':'. Returns None when there is no ':', when either
    half is empty, or when the facet id is not a usable facet name.
    """
    facet, sep, value = str(token).strip().partition(":")
    if not sep:
        return None
    facet = facet.strip()
    value = value.strip()
    if not facet or not value:
        return None
    if not FACET_ID_RE.match(facet):
        return None
    return facet, value


def slugify_value(text: str) -> str:
    """`'Small Molecule'` -> `'small_molecule'`; `'DMPK / ADME'` -> `'dmpk_adme'`.

    Lowercase, non-`[a-z0-9_.-]` -> `'_'`, collapse runs, strip leading and
    trailing `'_.-'`, truncate to 64. Returns `''` when nothing survives.
    """
    slug = re.sub(r"[^a-z0-9_.-]+", "_", str(text).strip().lower())
    slug = re.sub(r"_{2,}", "_", slug).strip("_.-")[:64].strip("_.-")
    return slug


def _slugify_facet(text: str) -> str:
    """Like `slugify_value`, but conformed to the tighter facet-id charset."""
    slug = re.sub(r"[.-]+", "_", slugify_value(text))
    slug = re.sub(r"_{2,}", "_", slug).strip("_")[:32].strip("_")
    return slug if FACET_ID_RE.match(slug) else ""


def _flatten(value: object) -> str:
    """Collapse a YAML folded/literal block into a single whitespace-run line."""
    return " ".join(str(value or "").split())
