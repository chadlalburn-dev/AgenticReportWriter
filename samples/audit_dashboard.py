"""Audit dashboard CLI — reviewer/QA view over an audit SQLite + anchor.

Reads an audit ledger produced by the generator, verifies the
hash-chain integrity, optionally verifies a saved AnchorRecord against
the chain + a public-key PEM, and prints a Markdown-style summary.

Use cases this is meant to support:
- A QA reviewer wants to confirm an anchored run's chain is intact
  before signing off
- An incident-response engineer wants to find which event broke a
  chain
- An internal-audit team wants a per-project event distribution
  (counts by action, by actor, by time)

Usage:
    python samples/audit_dashboard.py --db PATH [--project-id ID]
        [--anchor PATH] [--public-key PATH] [--markdown PATH] [--since ISO]

Examples:
    python samples/audit_dashboard.py \
      --db samples/synthetic_compound/output/audit.sqlite \
      --project-id "ib-pilot/XYZ-001" \
      --anchor samples/synthetic_compound/output/anchor.json \
      --public-key samples/synthetic_compound/output/anchor.pub.pem
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.audit import (  # noqa: E402
    AnchorRecord,
    AnchorVerificationFailure,
    AnchorVerifier,
    AuditAction,
    AuditQuery,
    HashChainViolation,
    LocalRsaVerifier,
    SqliteAuditStore,
    verify_chain,
)


def _print_kv(label: str, value: object) -> None:
    print(f"{label:.<32s} {value}")


def _summarize_event_counts(events: list) -> None:
    counts: Counter[str] = Counter(e.action.value for e in events)
    print("\n## Events by action\n")
    for action, n in sorted(counts.items()):
        print(f"  {action:.<36s} {n}")


def _summarize_by_actor(events: list) -> None:
    counts: Counter[str] = Counter(e.actor_id for e in events)
    print("\n## Events by actor\n")
    for actor, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {actor:.<36s} {n}")


def _summarize_by_mode(events: list) -> None:
    counts: Counter[str] = Counter(e.mode for e in events)
    print("\n## Events by compliance mode\n")
    for mode, n in sorted(counts.items()):
        print(f"  {mode:.<36s} {n}")


def _print_first_and_last(events: list) -> None:
    if not events:
        return
    first = events[0]
    last = events[-1]
    print("\n## Time bounds\n")
    _print_kv("first event", first.timestamp_utc.isoformat())
    _print_kv("last event", last.timestamp_utc.isoformat())
    _print_kv("first event id", first.event_id)
    _print_kv("last event id", last.event_id)


def _verify_anchor(
    anchor_path: Path,
    public_key_path: Path | None,
    events: list,
) -> bool:
    anchor = AnchorRecord.model_validate_json(anchor_path.read_text(encoding="utf-8"))
    print("\n## Anchor verification\n")
    _print_kv("anchor_id", anchor.anchor_id)
    _print_kv("project_id", anchor.project_id)
    _print_kv("period_start", anchor.period_start.isoformat())
    _print_kv("period_end", anchor.period_end.isoformat())
    _print_kv("event_count", anchor.event_count)
    _print_kv("merkle_root (head 16)", anchor.merkle_root_hex[:16] + "...")
    _print_kv("signer_id", anchor.signer_id)
    _print_kv("public_key_fingerprint", anchor.public_key_fingerprint[:16] + "...")

    if public_key_path is None:
        print(
            "\n  (no public key supplied; signature check skipped — anchor metadata "
            "shown above)"
        )
        return False

    public_pem = public_key_path.read_bytes()
    verifier = AnchorVerifier(LocalRsaVerifier(public_pem))
    # Scope events to anchor's project + period window
    scoped = [
        e
        for e in events
        if e.project_id == anchor.project_id
        and anchor.period_start <= e.timestamp_utc <= anchor.period_end
    ]
    try:
        verifier.verify(anchor, scoped)
    except AnchorVerificationFailure as exc:
        print(f"\n  FAIL: {exc.reason} — {exc.detail}")
        return False
    print("\n  OK: anchor verified against the chain + supplied public key")
    return True


def _write_markdown_report(
    path: Path,
    events: list,
    chain_ok: bool,
    chain_error: str | None,
    anchor_ok: bool | None,
    project_id: str | None,
) -> None:
    counts = Counter(e.action.value for e in events)
    lines: list[str] = []
    lines.append("# Audit dashboard report\n")
    lines.append(f"- generated_at: {datetime.utcnow().isoformat()}Z")
    lines.append(f"- project_id_filter: {project_id or '(none)'}")
    lines.append(f"- total_events: {len(events)}")
    lines.append(f"- chain_verified: {chain_ok}")
    if chain_error:
        lines.append(f"- chain_error: {chain_error}")
    if anchor_ok is not None:
        lines.append(f"- anchor_verified: {anchor_ok}")
    lines.append("\n## Action counts\n")
    for action, n in sorted(counts.items()):
        lines.append(f"- `{action}`: {n}")
    if events:
        lines.append("\n## First/last events\n")
        lines.append(
            f"- first: `{events[0].event_id}` at {events[0].timestamp_utc.isoformat()}"
        )
        lines.append(
            f"- last:  `{events[-1].event_id}` at {events[-1].timestamp_utc.isoformat()}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Path to audit SQLite")
    parser.add_argument(
        "--project-id",
        help="Restrict to one project's chain (default: all projects)",
    )
    parser.add_argument(
        "--anchor",
        type=Path,
        help="Optional AnchorRecord JSON to verify against the chain",
    )
    parser.add_argument(
        "--public-key",
        type=Path,
        help="Optional public-key PEM for verifying the anchor's signature",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        help="Optional path to write a Markdown report of the run",
    )
    parser.add_argument(
        "--since",
        help="Optional ISO-8601 lower bound on timestamp_utc",
    )
    parser.add_argument(
        "--until",
        help="Optional ISO-8601 upper bound on timestamp_utc",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: audit DB does not exist: {args.db}", file=sys.stderr)
        return 2

    since = datetime.fromisoformat(args.since) if args.since else None
    until = datetime.fromisoformat(args.until) if args.until else None
    query = AuditQuery(
        project_id=args.project_id, since=since, until=until
    )

    with SqliteAuditStore(args.db) as store:
        events = list(store.query(query))

    print(f"# Audit dashboard\n")
    _print_kv("source", args.db)
    _print_kv("project_id filter", args.project_id or "(all projects)")
    _print_kv("total events", len(events))

    if not events:
        print("\nNo events match the filter. Nothing to verify.")
        if args.markdown:
            _write_markdown_report(args.markdown, [], True, None, None, args.project_id)
            print(f"\nMarkdown report: {args.markdown}")
        return 0

    _summarize_event_counts(events)
    _summarize_by_actor(events)
    _summarize_by_mode(events)
    _print_first_and_last(events)

    # Chain verification — only meaningful when scoped to a project.
    print("\n## Chain verification\n")
    chain_ok = True
    chain_error: str | None = None
    if args.project_id:
        try:
            n = verify_chain(events)
            _print_kv("scoped events", n)
            _print_kv("status", "OK")
        except HashChainViolation as exc:
            chain_ok = False
            chain_error = str(exc)
            _print_kv("status", "FAIL")
            _print_kv("detail", chain_error)
    else:
        # Without project_id, events from multiple chains interleave; verify
        # each project's chain separately.
        by_project: dict[str, list] = defaultdict(list)
        for e in events:
            by_project[e.project_id].append(e)
        any_fail = False
        for project_id, project_events in sorted(by_project.items()):
            try:
                verify_chain(project_events)
                _print_kv(project_id, f"OK ({len(project_events)} events)")
            except HashChainViolation as exc:
                any_fail = True
                _print_kv(project_id, f"FAIL — {exc}")
        chain_ok = not any_fail

    # Anchor verification (optional).
    anchor_ok: bool | None = None
    if args.anchor:
        if not args.anchor.exists():
            print(f"\nWARN: anchor file not found at {args.anchor}", file=sys.stderr)
        else:
            anchor_ok = _verify_anchor(args.anchor, args.public_key, events)

    if args.markdown:
        _write_markdown_report(
            args.markdown, events, chain_ok, chain_error, anchor_ok, args.project_id
        )
        print(f"\nMarkdown report: {args.markdown}")

    return 0 if chain_ok and (anchor_ok is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
