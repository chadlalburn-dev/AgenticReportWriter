"""CLI: import a Word .docx file as a draft JSON template.

Usage:
    python samples/import_word_template.py PATH_TO_DOCX [--template-id ID]
        [--out PATH] [--title "Display Title"]

Output: a JSON file (one ReportTemplate object) ready for human review.
After review, the author edits the prompts/bindings, sets status to
"approved", and adds the template to templates/library/.

This is the first of four template-authoring entry points described in
the architecture plan; the others (sample-reports, library, from-scratch)
plug into the same TemplateBuilder façade as they're built.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.template_service import TemplateBuilder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx_path", type=Path, help="Path to the .docx file")
    parser.add_argument(
        "--template-id", required=True, help="Stable identifier (snake_case)"
    )
    parser.add_argument(
        "--title", help="Display title (defaults to the doc's Title style or filename)"
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output JSON path. Defaults to <docx_basename>.template.json.",
    )
    args = parser.parse_args()

    if not args.docx_path.exists():
        print(f"ERROR: {args.docx_path} does not exist", file=sys.stderr)
        return 2

    out_path = args.out or args.docx_path.with_suffix(".template.json")

    print(f"Reading: {args.docx_path}")
    result = TemplateBuilder().from_docx(
        args.docx_path,
        template_id=args.template_id,
        title=args.title,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        result.template.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    print(f"Wrote draft template: {out_path}")
    print(f"  template_id: {result.template.template_id}")
    print(f"  sections:    {len(result.template.all_sections())}")

    if result.warnings:
        print("\nWarnings (review before approving):")
        for w in result.warnings:
            print(f"  - {w}")

    print(
        "\nNext steps:\n"
        "  1. Open the JSON, review each section's prompt_template + data_bindings\n"
        "  2. Tighten validation_rules (citation_policy) per section\n"
        "  3. Move to templates/library/ and set status to 'approved'\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
