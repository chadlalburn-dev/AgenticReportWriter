"""Compound-first view models for the Titanium redesign.  OWNER: ENG-6.

The redesign inverts the app's information architecture: the compound is the
front door and report types are actions against it, not peers of it. This
module builds the compound page's view model from the same primitives the
run pipeline already uses — it introduces no new engine dependency.

## The handoff's open question, answered

> "Is source readiness knowable per-compound independent of report type, or is
> it template-dependent? This page assumes the former — one readiness ledger
> for the compound."

Measured against this codebase the answer is **both, and the split matters**:

* Bindings are *declared* per template. There is no compound-level binding
  registry to read.
* But readiness of a binding is a property of its *target*, not of the
  template that declares it: a named query either exists in the registry or
  does not; a connector operation is either allow-listed or is not; an
  evidence tag either matches documents or does not. None of those checks
  consult the compound.

So a compound-level ledger is well defined and is what this module builds: the
union of every binding declared by any template, deduped by `binding_id`, with
`used_by` counting the templates that declare it. That is exactly the "used by
· 3 reports" column in the design. Per-template coverage is then a projection
of the same ledger, which is why the two always agree.

One consequence worth stating plainly: because readiness is resolved from the
binding target and not from data, the *row counts* a query would return are not
knowable before the query runs. The design's `returns` column shows sample
values like `2 pages` / `1 row`. We can honour that shape truthfully for
evidence documents (the folder scan is real) but for queries and connectors we
report the readiness fact we actually have rather than inventing a row count.
See `_returns_text`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from services.api_gateway import runs as runs_module
from services.api_gateway.runs import RunStore, SourceSpec, _Dict

# ---------------------------------------------------------------------------
# repo layout
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYNTH = _REPO_ROOT / "samples" / "synthetic_compound"
_METADATA = _SYNTH / "metadata.json"
QUERIES_REL = "samples/synthetic_compound/queries/"


# ---------------------------------------------------------------------------
# view models
# ---------------------------------------------------------------------------


@dataclass
class BindingRow(_Dict):
    """One row of the compound's evidence ledger."""

    binding_id: str
    system: str          # Confluence / ChEMBL / BigQuery / Evidence folder …
    resolve_target: str  # human-readable "resolves to" string
    resolved: bool
    returns_text: str    # short mono string for the `returns` column
    used_by_count: int
    used_by_text: str
    fix_hint: str


@dataclass
class ReportOption(_Dict):
    """A report type offered against this compound, ranked by coverage."""

    template_key: str
    name: str
    kicker: str
    sources_ready: int
    sources_total: int
    ratio_text: str
    tick_flags: list[bool]
    url: str
    lead: bool = False


@dataclass
class RunRow(_Dict):
    run_id: str
    report_name: str
    evidenced_text: str
    when_text: str
    url: str


@dataclass
class RelatedChip(_Dict):
    label: str
    url: str


@dataclass
class CompoundView(_Dict):
    compound_id: str
    subtitle: str
    subtitle_full: str
    programme: str
    modality: str
    document_count: int
    run_count: int
    last_run_text: str
    meta_items: list[str]

    bindings: list[BindingRow] = field(default_factory=list)
    reports: list[ReportOption] = field(default_factory=list)
    runs: list[RunRow] = field(default_factory=list)
    related: list[RelatedChip] = field(default_factory=list)

    bindings_ready: int = 0
    bindings_total: int = 0
    gaps_count: int = 0
    missing_query_count: int = 0

    @property
    def coverage_label(self) -> str:
        return f"of {self.bindings_total} bindings resolve"

    @property
    def primary_report(self) -> ReportOption | None:
        return self.reports[0] if self.reports else None


@dataclass
class CompoundCard(_Dict):
    """A row on the compounds list (the front page).

    Every field here is a REAL per-compound fact, derived from that compound's
    own runs. That constraint is deliberate: the obvious thing to show is
    source coverage ("13 of 29 bindings ready"), but preflight resolves
    bindings against the local corpus and the mock connectors, so it returns
    the SAME number for every compound. Putting it on the row would look like
    per-compound signal and carry none — worse than showing nothing.

    What is genuinely per-compound: which reports have been drafted, how well
    evidenced those drafts were, who worked on it, and when.
    """

    compound_id: str
    name: str
    meta_text: str
    url: str
    run_count: int = 0
    mine: bool = False          # current user has run at least one report here
    owners: list[str] = field(default_factory=list)
    owners_text: str = ""       # "you" / "you +2 others" / "3 people"

    # --- evidence rollup across this compound's own runs -------------------
    reports_drafted: int = 0        # distinct report types drafted here
    claims_cited: int = 0           # summed across runs
    claims_total: int = 0
    cited_text: str = ""            # "18/19 claims cited" — or "" when no runs
    last_report: str = ""           # the most recent report type drafted
    last_run_id: str = ""
    last_run_url: str = ""
    has_gaps: bool = False          # at least one uncited claim somewhere


# ---------------------------------------------------------------------------
# compound catalogue
# ---------------------------------------------------------------------------


def _load_metadata() -> dict[str, Any]:
    try:
        return json.loads(_METADATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _seed_compound() -> dict[str, str]:
    """The synthetic corpus's compound, if the sample data is present."""
    meta = _load_metadata().get("compound") or {}
    code = str(meta.get("research_code") or "").strip()
    if not code:
        return {}
    chem = str(meta.get("chemical_class") or "").strip()
    # "Small-molecule selective inhibitor of Kinase Z" -> subtitle + modality
    full = chem or str(meta.get("indication") or "")
    subtitle = _short_subtitle(full) if chem else full
    modality = "small molecule" if "small-molecule" in chem.lower() else ""
    return {
        "compound_id": code,
        "subtitle": subtitle,
        "subtitle_full": full,
        "modality": modality,
        "programme": "PSS",
        "target": _target_from_class(chem),
    }


def _target_from_class(chemical_class: str) -> str:
    """Pull a target name out of '… inhibitor of Kinase Z'."""
    low = chemical_class.lower()
    for marker in (" of ", " against "):
        if marker in low:
            return chemical_class[low.index(marker) + len(marker):].strip(" .")
    return ""


_MODE_WORDS = (
    "inhibitor", "agonist", "antagonist", "modulator", "degrader",
    "activator", "blocker", "inducer",
)


def _short_subtitle(chemical_class: str) -> str:
    """'Small-molecule selective inhibitor of Kinase Z' -> 'Kinase Z inhibitor'.

    The identity band puts id + subtitle + metadata on one baseline, which is
    most of its vertical saving over the original. The full chemical class runs
    364px and pushes the metadata onto a second line, costing 32px of band
    height. The design's own example subtitle is the short form, so this
    derives it rather than truncating mid-word. Falls back to the full string
    when the pattern does not match; the full text is kept as a title
    attribute either way.
    """
    target = _target_from_class(chemical_class)
    if not target:
        return chemical_class
    low = chemical_class.lower()
    for word in _MODE_WORDS:
        if word in low:
            return f"{target} {word}"
    return target


def _short_date(human: str) -> str:
    """'19 Aug 2026, 12:20' -> '19 Aug'. The band's metadata row is a scan
    line, not an audit trail — the run rows carry full timestamps."""
    parts = (human or "").replace(",", " ").split()
    if len(parts) >= 2 and parts[0].rstrip(".").isdigit():
        return f"{parts[0]} {parts[1]}"
    return human


def looks_like_compound_id(value: str) -> bool:
    """Heuristic gate on values harvested from past runs' `primary_input`.

    That field is whatever the template's primary input happened to be, so the
    run history also contains target names ("Kinase Z") and scratch values from
    testing ("CRASH", "FINAL-1"). A compound identifier carries a digit, has no
    spaces and is short. Without this filter the Related rail fills with noise.
    """
    v = (value or "").strip()
    if not v or len(v) > 24 or " " in v:
        return False
    return any(ch.isdigit() for ch in v) and any(ch.isalpha() for ch in v)


def _run_primary_inputs(store: RunStore, limit: int = 400) -> list[str]:
    seen: list[str] = []
    for summary in store.list_runs(limit=limit):
        value = (summary.primary_input or "").strip()
        if value and value not in seen and looks_like_compound_id(value):
            seen.append(value)
    return seen


def known_compounds(store: RunStore | None = None) -> list[str]:
    """Every compound id the app can currently show a page for."""
    store = store or runs_module.get_store()
    ids: list[str] = []
    seed = _seed_compound()
    if seed:
        ids.append(seed["compound_id"])
    for value in _run_primary_inputs(store):
        if value not in ids:
            ids.append(value)
    return ids


def _owners_text(owners: list[str], current_user: str) -> str:
    """'you' / 'you +2 others' / '3 people' / 'unattributed'.

    Runs created before attribution existed carry no owner, so a compound can
    legitimately have runs and no owners — say so rather than implying nobody
    touched it.
    """
    real = [o for o in owners if o]
    if not real:
        return "unattributed"
    mine = current_user in real
    others = len([o for o in real if o != current_user])
    if mine and not others:
        return "you"
    if mine:
        return f"you +{others} other" + ("s" if others != 1 else "")
    if others == 1:
        return _display_name(real[0])
    return f"{others} people"


def _display_name(user_id: str) -> str:
    return (user_id or "").split("@", 1)[0] or "unknown"


def compound_cards(
    store: RunStore | None = None,
    limit: int = 60,
    *,
    current_user: str = "",
    scope: str = "all",
) -> list[CompoundCard]:
    """The compounds list, most-recently-run first.

    `scope='mine'` keeps only compounds the current user has actually run a
    report against — attribution comes from `RunSummary.owner`. `scope='all'`
    shows every compound with a report across all users, which is the point of
    having the split: work is discoverable across the team without each person
    re-deriving what has already been evidenced.
    """
    store = store or runs_module.get_store()
    by_id: dict[str, list[Any]] = {}
    for summary in store.list_runs(limit=800):
        key = (summary.primary_input or "").strip()
        if key:
            by_id.setdefault(key, []).append(summary)

    ordered: list[str] = [k for k in by_id if looks_like_compound_id(k)]
    seed = _seed_compound()
    if seed and seed["compound_id"] not in ordered:
        ordered.insert(0, seed["compound_id"])

    cards: list[CompoundCard] = []
    for cid in ordered:
        runs = by_id.get(cid, [])
        owners = sorted({(getattr(r, "owner", "") or "") for r in runs} - {""})
        mine = bool(current_user) and current_user in owners
        if scope == "mine" and not mine:
            continue

        subtitle = seed["subtitle"] if (seed and cid == seed["compound_id"]) else ""
        n = len(runs)
        if n:
            # Design specifies "3 runs · 19 Aug" — day + month, no year/time.
            last = _short_date(runs[0].created_human)
            meta = f"{n} {'run' if n == 1 else 'runs'} · {last}"
        else:
            meta = "no runs yet"

        # Evidence rollup, from this compound's own runs. Free: these summaries
        # are already in hand.
        cited = sum(int(getattr(r, "n_claims_cited", 0) or 0) for r in runs)
        claims = sum(int(getattr(r, "n_claims", 0) or 0) for r in runs)
        titles = [t for t in ((getattr(r, "template_title", "") or "") for r in runs) if t]
        newest = runs[0] if runs else None

        cards.append(
            CompoundCard(
                compound_id=cid,
                name=subtitle,
                meta_text=meta,
                url=f"/compound/{cid}",
                run_count=n,
                mine=mine,
                owners=owners,
                owners_text=_owners_text(owners, current_user),
                reports_drafted=len(set(titles)),
                claims_cited=cited,
                claims_total=claims,
                cited_text=(f"{cited}/{claims} claims cited" if claims else ""),
                last_report=(titles[0] if titles else ""),
                last_run_id=(getattr(newest, "run_id", "") if newest else ""),
                last_run_url=(
                    f"/runs/{getattr(newest, 'run_id', '')}" if newest else ""
                ),
                has_gaps=bool(claims and cited < claims),
            )
        )
        if len(cards) >= limit:
            break
    return cards


def compound_scope_counts(
    store: RunStore | None = None, *, current_user: str = ""
) -> dict[str, int]:
    """Counts for the My / All segmented control labels."""
    store = store or runs_module.get_store()
    all_cards = compound_cards(store, current_user=current_user, scope="all")
    return {
        "all": len(all_cards),
        "mine": sum(1 for c in all_cards if c.mine),
    }


# ---------------------------------------------------------------------------
# ledger construction
# ---------------------------------------------------------------------------

_SYSTEM_BY_KIND = {
    "named_query": "BigQuery",
    "sql_query": "Inline SQL",
    "file_set": "Evidence folder",
    "file_ref": "Evidence folder",
    "computed_metric": "Computed metric",
}

_CONNECTOR_LABELS = {
    "confluence": "Confluence",
    "mock_confluence": "Confluence",
    "chembl": "ChEMBL",
    "mock_chembl": "ChEMBL",
    "clinicaltrials": "ClinicalTrials.gov",
    "mock_clinicaltrials": "ClinicalTrials.gov",
}


def _system_for(spec: SourceSpec) -> str:
    if spec.kind == "api_call":
        connector = spec.label.split(".", 1)[0].strip()
        return _CONNECTOR_LABELS.get(connector.lower(), connector or "API")
    return _SYSTEM_BY_KIND.get(spec.kind, spec.kind.replace("_", " "))


def _resolve_target(spec: SourceSpec) -> str:
    """The human-readable 'resolves to' string, system prefix stripped.

    Design shows e.g. `Confluence · space PSS · target rationale pages` and
    `preclinical_assays.assay_potency_selectivity_v1`.
    """
    system = _system_for(spec)
    if spec.kind == "named_query":
        # label is "Registered query <source>.<query_id>"
        tail = spec.label.replace("Registered query", "").strip()
        return tail or spec.label
    if spec.kind == "api_call":
        endpoint = spec.label.split(".", 1)[1] if "." in spec.label else spec.label
        detail = (spec.detail or "").strip()
        parts = [system, endpoint.replace("_", " ")]
        if detail and detail.lower() not in ("no parameters", ""):
            parts.append(detail)
        return " · ".join(p for p in parts if p)
    if spec.kind in ("file_set", "file_ref"):
        detail = (spec.detail or "").replace("matches any of:", "").strip()
        return f"{system} · {detail}" if detail else system
    return f"{system} · {spec.detail}".strip(" ·") or spec.label


def _returns_text(spec: SourceSpec) -> str:
    """The short `returns` string.

    Truthful about what is knowable before a run: the evidence-folder scan
    yields a real document count, so we show it. A registered query's row count
    is NOT knowable without executing the query, so we report the readiness
    fact instead of inventing a number. Gap reasons are compressed to fit the
    76px column.
    """
    text = (spec.status_text or "").lower()
    if spec.status == "ready":
        if spec.kind in ("file_set", "file_ref"):
            # "Ready — 6 matching documents"
            for token in text.replace("—", " ").split():
                if token.isdigit():
                    n = int(token)
                    return f"{n} doc" if n == 1 else f"{n} docs"
            return "found"
        if spec.kind == "named_query":
            return "registered"
        if spec.kind == "api_call":
            return "reachable"
        return "ready"

    if spec.kind == "named_query":
        return "not reg."
    if spec.kind == "api_call":
        return "no conn." if "not registered" in text else "not allowed"
    if spec.kind in ("file_set", "file_ref"):
        return "no match"
    if spec.kind == "sql_query":
        return "inline"
    return "not avail."


def _template_inputs(store: RunStore, key: str, compound_id: str) -> dict[str, str]:
    """Fill a template's inputs for this compound.

    Every template's primary input is the compound/programme identifier, but
    templates name it differently (`compound_id`, `target_name`, …). We seed
    the defaults then overwrite anything that looks like the primary handle.
    """
    values = dict(store.default_inputs(key))
    seed = _seed_compound()
    target = seed.get("target", "") if seed else ""
    for field_name in list(values):
        low = field_name.lower()
        if "compound" in low or low in ("product_name", "programme", "program"):
            values[field_name] = compound_id
        elif "target" in low and target:
            values[field_name] = target
        elif "indication" in low and target:
            values[field_name] = target
    return values


def _kicker_for(card: Any) -> str:
    """A one-word category above the report name.

    Uses the template's own taxonomy tags — document_class first (it is the
    most decision-relevant facet), then domain.
    """
    tags = getattr(card, "tags", None) or {}
    if isinstance(tags, Mapping):
        for facet in ("document_class", "domain", "discipline"):
            values = tags.get(facet) or []
            if isinstance(values, str):
                values = [values]
            if values:
                return str(values[0]).replace("_", " ").title()
    return "Report"


def build_compound_view(
    compound_id: str,
    *,
    store: RunStore | None = None,
    evidence_folder: str | None = None,
) -> CompoundView:
    """Assemble the whole compound page.

    Readiness is computed once per template via the same `preflight` the run
    setup screen uses, then unioned into a compound-level ledger. This is why
    the ledger and the per-report ratios can never disagree.
    """
    store = store or runs_module.get_store()
    published, _drafts = store.list_templates()

    # --- per template: coverage + contribution to the ledger --------------
    ledger: dict[str, SourceSpec] = {}
    used_by: dict[str, set[str]] = {}
    options: list[ReportOption] = []

    for card in published:
        key = card.key
        inputs = _template_inputs(store, key, compound_id)
        try:
            report = store.preflight(key, inputs, evidence_folder)
        except Exception:
            # A template that cannot be parsed must not take the page down.
            continue
        specs = [s for s in report.sources]
        if not specs:
            continue

        flags: list[bool] = []
        for spec in specs:
            ready = spec.status == "ready"
            flags.append(ready)
            used_by.setdefault(spec.binding_id, set()).add(key)
            # First declaration wins; readiness is target-derived so any
            # template that declares the binding computes the same answer.
            ledger.setdefault(spec.binding_id, spec)

        ready_n = sum(1 for f in flags if f)
        options.append(
            ReportOption(
                template_key=key,
                name=card.title,
                kicker=_kicker_for(card),
                sources_ready=ready_n,
                sources_total=len(flags),
                ratio_text=f"{ready_n}/{len(flags)}",
                tick_flags=flags,
                url=f"/new/{key}",
            )
        )

    # Sort by coverage descending; the top row is the lead and takes accent.
    options.sort(
        key=lambda o: (
            -(o.sources_ready / o.sources_total if o.sources_total else 0),
            -o.sources_ready,
            o.name.lower(),
        )
    )
    if options:
        options[0].lead = True

    # --- ledger rows, resolved first then gaps, stable within each -------
    rows: list[BindingRow] = []
    for binding_id, spec in ledger.items():
        n_used = len(used_by.get(binding_id, ()))
        rows.append(
            BindingRow(
                binding_id=binding_id,
                system=_system_for(spec),
                resolve_target=_resolve_target(spec),
                resolved=spec.status == "ready",
                returns_text=_returns_text(spec),
                used_by_count=n_used,
                used_by_text=f"{n_used} report" if n_used == 1 else f"{n_used} reports",
                fix_hint=spec.fix_hint or "",
            )
        )
    rows.sort(key=lambda r: (0 if r.resolved else 1, r.binding_id))

    ready_total = sum(1 for r in rows if r.resolved)
    missing_queries = sum(
        1 for r in rows if not r.resolved and r.returns_text == "not reg."
    )

    # --- runs for this compound -----------------------------------------
    run_rows: list[RunRow] = []
    all_runs = store.list_runs(limit=400)
    mine = [s for s in all_runs if (s.primary_input or "").strip() == compound_id]
    for summary in mine[:3]:
        run_rows.append(
            RunRow(
                run_id=summary.run_id,
                report_name=summary.template_title,
                evidenced_text=f"{summary.n_claims_cited}/{summary.n_claims}",
                when_text=summary.created_human,
                url=f"/runs/{summary.run_id}",
            )
        )

    # --- identity --------------------------------------------------------
    seed = _seed_compound()
    is_seed = bool(seed) and seed["compound_id"] == compound_id
    subtitle = seed["subtitle"] if is_seed else ""
    subtitle_full = seed.get("subtitle_full", "") if is_seed else ""
    programme = seed["programme"] if is_seed else ""
    modality = seed["modality"] if is_seed else ""
    target = seed.get("target", "") if is_seed else ""

    doc_count = 0
    try:
        folder, _issue = store._resolve_evidence(evidence_folder)  # noqa: SLF001
        doc_count = len(runs_module.scan_evidence_folder(folder))
    except Exception:
        doc_count = 0

    meta_items = [x for x in (programme, modality) if x]
    if doc_count:
        meta_items.append(f"{doc_count} docs")
    n_runs = len(mine)
    meta_items.append(f"{n_runs} {'run' if n_runs == 1 else 'runs'}")
    if mine:
        meta_items.append(f"last {_short_date(mine[0].created_human)}")

    # --- related ---------------------------------------------------------
    related: list[RelatedChip] = []
    for other in known_compounds(store):
        if other != compound_id and len(related) < 4:
            related.append(RelatedChip(label=other, url=f"/compound/{other}"))
    if target:
        related.append(RelatedChip(label=f"{target} · target", url=f"/?q={target}"))

    return CompoundView(
        compound_id=compound_id,
        subtitle=subtitle,
        subtitle_full=subtitle_full,
        programme=programme,
        modality=modality,
        document_count=doc_count,
        run_count=n_runs,
        last_run_text=mine[0].created_human if mine else "",
        meta_items=meta_items,
        bindings=rows,
        reports=options,
        runs=run_rows,
        related=related,
        bindings_ready=ready_total,
        bindings_total=len(rows),
        gaps_count=len(rows) - ready_total,
        missing_query_count=missing_queries,
    )


def resolve_query(query: str, store: RunStore | None = None) -> str | None:
    """Match a search string to a compound id.

    Matches, in order: exact (case-insensitive), prefix, then substring.
    Returns None when nothing matches so the caller can show the home screen
    with a 'no match' note rather than guessing.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return None
    ids = known_compounds(store)
    for cid in ids:
        if cid.lower() == needle:
            return cid
    for cid in ids:
        if cid.lower().startswith(needle):
            return cid
    for cid in ids:
        if needle in cid.lower():
            return cid
    return None
