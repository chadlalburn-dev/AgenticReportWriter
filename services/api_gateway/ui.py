"""HTTP surface for the Report Generator Agent UI.  OWNER: ENG-6.

One `APIRouter` carrying every HTML and JSON route in the implementation
contract's route table (§2).  This module is deliberately thin:

  * it imports NOTHING from the engine packages — only `services.api_gateway.runs`;
  * it builds no view models (that is `runs.py`'s job);
  * it renders Jinja templates with exactly the context described in §3.

Everything state-changing is a POST that answers `303 See Other`, so a browser
refresh on a run page can never resubmit.
"""

from __future__ import annotations

import csv  # noqa: F401  (kept for parity with the export contract; runs.py serialises)
import io
import json
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.api_gateway import runs as runs_module
from services.api_gateway.runs import RunNotTerminal, get_store

# ---------------------------------------------------------------------------
# Jinja
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
TEMPLATES_PATH = _HERE / "templates"
STATIC_PATH = _HERE / "static"

TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_PATH))

APP_VERSION = "0.2.0"

router = APIRouter()

_VALID_TABS = ("draft", "sources", "log")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(
    request: Request,
    name: str,
    context: Mapping[str, Any],
    *,
    nav_active: str,
    status_code: int = 200,
) -> HTMLResponse:
    payload: dict[str, Any] = {
        "nav_active": nav_active,
        "app_version": APP_VERSION,
    }
    payload.update(context)
    return TEMPLATES.TemplateResponse(request, name, payload, status_code=status_code)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _see_other(url: Any) -> RedirectResponse:
    return RedirectResponse(str(url), status_code=303)


def _run_url(request: Request, run_id: str, tab: str | None = None) -> str:
    base = str(request.url_for("run_detail", run_id=run_id))
    if tab:
        base = f"{base}?tab={tab}"
    return base


def _tab_items(request: Request, run_id: str) -> list[tuple[str, str, str]]:
    base = str(request.url_for("run_detail", run_id=run_id))
    return [
        ("Draft", f"{base}?tab=draft", "draft"),
        ("Sources", f"{base}?tab=sources", "sources"),
        ("Run log", f"{base}?tab=log", "log"),
    ]


def _haystack(summary: Any) -> str:
    parts = [
        summary.title,
        summary.template_title,
        summary.primary_input,
        summary.status_label,
        summary.run_id,
        summary.created_human,
    ]
    return " ".join(str(p or "") for p in parts).lower()


def _gallery_context(**overrides: Any) -> dict[str, Any]:
    """Every key `gallery.html` can name, always present.

    `/runs` renders the same file, so the classification keys need real
    defaults or the run-history view raises `UndefinedError` (contract §5.2).
    """
    ctx: dict[str, Any] = {
        "view": "gallery",
        "cards": [],
        "unavailable": [],
        "templates_dir": str(runs_module.TEMPLATES_DIR),
        "recent": [],
        "runs": [],
        "filter_q": "",
        "group_by_compound": False,
        # --- classification / grouping (contract §5.2)
        "q": "",
        "group": runs_module.GROUP_NONE,
        "sort": "name",
        "facets": [],
        "groups": [],
        "n_shown": 0,
        "n_total": 0,
        "n_active": 0,
        "active_summary": [],
        "clear_url": "/",
        "sort_options": [],
        "group_options": [],
        "filtering": False,
        "flash": None,
        "new_template_url": "/templates/new",
        "taxonomy_ok": True,
    }
    ctx.update(overrides)
    return ctx


_FLASH_PARAMS: tuple[tuple[str, str], ...] = (
    ("saved", "ok"),
    ("restored", "ok"),
    ("deleted", "neutral"),
)


def _flash_for(store: Any, params: Any) -> dict[str, str] | None:
    """Turn `?saved=` / `?deleted=` / `?restored=` into one banner.

    The value is a template key, so it is checked against the key charset
    before it goes anywhere near the page.
    """
    for name, state in _FLASH_PARAMS:
        raw = str(params.get(name) or "").strip()
        if not raw or not runs_module.TEMPLATE_KEY_RE.match(raw):
            continue
        title = raw
        try:
            title = store.get_template(raw).title or raw
        except Exception:  # noqa: BLE001 - a deleted key has no card any more
            pass
        if name == "saved":
            return {
                "state": state,
                "title": f"Saved “{title}”",
                "body": "The report template file was written. It is in the list below.",
                "href": f"/new/{quote(raw)}",
                "href_label": "Set up a run",
            }
        if name == "restored":
            return {
                "state": state,
                "title": f"Put “{title}” back",
                "body": "The file was restored from the trash folder.",
                "href": f"/new/{quote(raw)}",
                "href_label": "Set up a run",
            }
        return {
            "state": state,
            "title": f"Deleted “{raw}”",
            "body": (
                "The file was moved to the trash folder, not erased. "
                f"It is at {store.trash_dir}."
            ),
            "href": "",
            "href_label": "",
        }
    return None


def _key_or_404(template_key: str) -> str:
    if not runs_module.TEMPLATE_KEY_RE.match(str(template_key or "")):
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {template_key!r}."
        )
    return template_key


def _render_editor(
    request: Request, context: Mapping[str, Any], *, status_code: int = 200
) -> HTMLResponse:
    try:
        return _render(
            request,
            "template_editor.html",
            context,
            nav_active="gallery",
            status_code=status_code,
        )
    except TemplateNotFound as exc:  # pragma: no cover - build-integrity guard
        raise StarletteHTTPException(
            status_code=503,
            detail="The template editor screen is not part of this build.",
        ) from exc


def _new_run_context(
    store: Any,
    template_key: str,
    *,
    values: Mapping[str, str] | None = None,
    field_errors: Mapping[str, str] | None = None,
    evidence_folder: str = "",
    from_run_id: str | None = None,
) -> dict[str, Any]:
    """Build the whole §3.2 context.  Never raises for a broken template."""
    card = store.get_template(template_key)  # KeyError -> caller turns it into 404

    resolved, label, evidence_error = store.describe_evidence(evidence_folder)
    default_folder = str(runs_module.CORPUS_DIR)

    if not card.ok:
        return {
            "card": card,
            "fields": [],
            "values": {},
            "field_errors": {},
            "evidence_folder": evidence_folder or default_folder,
            "evidence_default": default_folder,
            "evidence_label": label,
            "evidence_error": evidence_error,
            "preflight": store.preflight(template_key, {}, evidence_folder or None),
            "sections": [],
            "from_run_id": from_run_id,
        }

    fields = card.form_fields
    effective: dict[str, str] = {f.binding_id: f.default for f in fields}
    if values:
        for f in fields:
            if f.binding_id in values:
                effective[f.binding_id] = str(values[f.binding_id] or "")

    return {
        "card": card,
        "fields": fields,
        "values": effective,
        "field_errors": dict(field_errors or {}),
        "evidence_folder": evidence_folder or default_folder,
        "evidence_default": default_folder,
        "evidence_label": label,
        "evidence_error": evidence_error,
        "preflight": store.preflight(template_key, effective, evidence_folder or None),
        "sections": store.template_outline(template_key),
        "from_run_id": from_run_id,
    }


def _blocker_field_errors(preflight: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for issue in getattr(preflight, "issues", []) or []:
        if issue.severity == "blocker" and issue.binding_id:
            out.setdefault(issue.binding_id, issue.message)
    return out


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, name="gallery", include_in_schema=False)
def gallery(request: Request) -> HTMLResponse:
    """The report-type gallery, grouped / sorted / filtered SERVER-SIDE.

    Query params (contract §4.1): `group`, `sort`, repeatable `tag`, `q`, and
    the three redirect flags. Every one of them is canonicalised in
    `RunStore.gallery_view` — an unknown facet, value, group or sort widens
    the result set instead of erroring, so a stale bookmark still works.
    """
    store = get_store()
    params = request.query_params
    view = store.gallery_view(
        group=params.get("group"),
        sort=params.get("sort"),
        tags=params.getlist("tag"),
        q=params.get("q") or "",
    )
    return _render(
        request,
        "gallery.html",
        _gallery_context(
            view="gallery",
            cards=view.cards,
            unavailable=view.unavailable,
            recent=store.recent(3),
            q=view.q,
            group=view.group,
            sort=view.sort,
            facets=view.facets,
            groups=view.groups,
            n_shown=view.n_shown,
            n_total=view.n_total,
            n_active=view.n_active,
            active_summary=view.active_summary,
            clear_url=view.clear_url,
            sort_options=view.sort_options,
            group_options=view.group_options,
            filtering=view.filtering,
            flash=_flash_for(store, params),
            taxonomy_ok=view.taxonomy_ok,
        ),
        nav_active="gallery",
    )


# ---------------------------------------------------------------------------
# Template authoring (contract §4.2)
#
# `POST /templates` and `POST /templates/{key}` are the ONLY two routes in the
# application that write into `report-templates/`. Every other op re-renders
# the submitted draft at 200 without touching the disk, so the undo for any
# mistake before Save is "do not press Save".
#
# `/templates/new` is registered before `/templates/{key}/...` deliberately.
# ---------------------------------------------------------------------------


@router.get(
    "/templates/new",
    response_class=HTMLResponse,
    name="template_new",
    include_in_schema=False,
)
def template_new(request: Request) -> HTMLResponse:
    store = get_store()
    taxonomy = runs_module.load_taxonomy()
    from_key = str(request.query_params.get("from") or "").strip()

    draft = None
    banner: dict[str, str] | None = None
    if from_key:
        try:
            source = store.draft_for(from_key)
        except (KeyError, ValueError, OSError):
            banner = {
                "state": "neutral",
                "title": "That report type could not be copied",
                "body": (
                    f"There is no report template named “{from_key}” that can "
                    "be copied. This is a blank one instead."
                ),
            }
        else:
            draft = runs_module.clone_draft(
                source, report_type=store.suggest_template_key(from_key)
            )
            banner = {
                "state": "neutral",
                "title": f"Copied from “{source.title or from_key}”",
                "body": (
                    "The key and the version were reset. Everything else, "
                    "including the tags, came across unchanged."
                ),
            }
    if draft is None:
        draft = runs_module.blank_draft(tags=taxonomy.defaults_for_new())

    return _render_editor(
        request,
        runs_module.editor_context(draft, mode="new", banner=banner, taxonomy=taxonomy),
    )


@router.get(
    "/templates/{template_key}/edit",
    response_class=HTMLResponse,
    name="template_edit",
    include_in_schema=False,
)
def template_edit(request: Request, template_key: str) -> HTMLResponse:
    store = get_store()
    key = _key_or_404(template_key)
    taxonomy = runs_module.load_taxonomy()

    try:
        draft = store.draft_for(key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {key!r}."
        )
    except (ValueError, OSError) as exc:
        raise StarletteHTTPException(
            status_code=422,
            detail=f"{key} could not be opened in the editor. {exc}",
        )

    path = store.template_path(key)
    banner: dict[str, str] | None = None
    generated = runs_module.preview_text(draft, taxonomy=taxonomy)
    try:
        on_disk = path.read_text(encoding="utf-8")
    except OSError:
        on_disk = generated
    if generated and generated != on_disk:
        banner = {
            "state": "neutral",
            "title": "Saving rewrites this file",
            "body": (
                "This file was written by hand. Saving rewrites the front "
                "matter in the app's own layout, so any comments in it are "
                "lost. Press Check to see exactly what would be written — and "
                "a copy of the current file is kept in the backups folder "
                "every time you save."
            ),
        }

    return _render_editor(
        request,
        runs_module.editor_context(
            draft,
            mode="edit",
            key=key,
            base_sha=runs_module.read_sha256(path),
            banner=banner,
            taxonomy=taxonomy,
        ),
    )


async def _post_template(request: Request, key: str | None) -> Response:
    """The `op` dispatch shared by both POST routes (contract §4.3, §8.3)."""
    store = get_store()
    taxonomy = runs_module.load_taxonomy()
    create = key is None
    mode = "new" if create else "edit"

    if not create and not store.template_exists(key or ""):
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {key!r}."
        )

    form = await request.form()
    op = str(form.get("op") or "check").strip() or "check"
    base_sha = str(form.get("base_sha") or "")
    force = str(form.get("force") or "").strip().lower() in ("1", "true", "yes", "on")

    try:
        draft = runs_module.draft_from_form(form, taxonomy=taxonomy)
    except Exception as exc:  # noqa: BLE001 - a malformed body is a 422, never a 500
        raise StarletteHTTPException(
            status_code=422, detail=f"That form could not be read. {exc}"
        )
    if not create:
        draft.report_type = key or ""  # the key is locked while editing

    if runs_module.is_structural_op(op):
        notes = runs_module.apply_structural_op(
            draft, op, add_source_kind=str(form.get("add_source_kind") or "bigquery")
        )
        return _render_editor(
            request,
            runs_module.editor_context(
                draft,
                mode=mode,
                key=key or "",
                base_sha=base_sha,
                cascade_notes=notes,
                taxonomy=taxonomy,
            ),
            status_code=200,
        )

    base_draft = None
    if not create:
        try:
            base_draft = store.draft_for(key or "")
        except (KeyError, ValueError, OSError):
            base_draft = None

    issues = runs_module.validate_template_draft(
        draft,
        existing_keys=store.template_keys(),
        is_new=create,
        taxonomy=taxonomy,
        base_draft=base_draft,
    )
    preview = runs_module.preview_text(draft, taxonomy=taxonomy)

    if op != "save" or runs_module.draft_has_errors(issues):
        blocked = op == "save"
        return _render_editor(
            request,
            runs_module.editor_context(
                draft,
                mode=mode,
                key=key or "",
                base_sha=base_sha,
                issues=issues,
                preview=preview,
                checked=not blocked,
                banner=(
                    {
                        "state": "error",
                        "title": "This template was not saved",
                        "body": (
                            "Nothing has been written. Fix the problems listed "
                            "below and press Save again."
                        ),
                    }
                    if blocked
                    else None
                ),
                taxonomy=taxonomy,
            ),
            status_code=422 if blocked else 200,
        )

    try:
        store.save_draft(
            draft,
            create=create,
            expected_sha256=(None if (create or force) else (base_sha or None)),
        )
    except runs_module.TemplateConflict:
        return _render_editor(
            request,
            runs_module.editor_context(
                draft,
                mode=mode,
                key=key or "",
                base_sha=base_sha,
                issues=issues,
                preview=preview,
                conflict=True,
                banner={
                    "state": "warn",
                    "title": "This file changed on disk after you opened it",
                    "body": (
                        "Something else edited this file — an editor, or a "
                        "pull. Nothing has been written. Choose which version "
                        "to keep."
                    ),
                },
                taxonomy=taxonomy,
            ),
            status_code=409,
        )
    except (runs_module.TemplateWriteError, ValueError, KeyError) as exc:
        # KeyError is unreachable while `key_charset` validation runs first —
        # it is here so a future validation gap can never become a 500.
        return _render_editor(
            request,
            runs_module.editor_context(
                draft,
                mode=mode,
                key=key or "",
                base_sha=base_sha,
                issues=issues,
                preview=preview,
                banner={
                    "state": "error",
                    "title": "This template could not be saved",
                    "body": str(exc),
                },
                taxonomy=taxonomy,
            ),
            status_code=422,
        )
    except OSError as exc:
        return _render_editor(
            request,
            runs_module.editor_context(
                draft,
                mode=mode,
                key=key or "",
                base_sha=base_sha,
                issues=issues,
                preview=preview,
                banner={
                    "state": "error",
                    "title": "This template could not be written",
                    "body": (
                        f"{draft.report_type}.md could not be written to "
                        f"{store.templates_dir}: {exc.strerror or exc}. Check "
                        "the folder is not read-only and that the file is not "
                        "open in something else."
                    ),
                },
                taxonomy=taxonomy,
            ),
            status_code=422,
        )

    return _see_other(f"/?saved={quote(draft.report_type, safe='')}")


@router.post("/templates", name="template_create", include_in_schema=False)
async def template_create(request: Request) -> Response:
    return await _post_template(request, None)


@router.post("/templates/{template_key}", name="template_save", include_in_schema=False)
async def template_save(request: Request, template_key: str) -> Response:
    return await _post_template(request, _key_or_404(template_key))


@router.get(
    "/templates/{template_key}/delete",
    response_class=HTMLResponse,
    name="template_delete_confirm",
    include_in_schema=False,
)
def template_delete_confirm(request: Request, template_key: str) -> HTMLResponse:
    store = get_store()
    key = _key_or_404(template_key)
    if not store.template_exists(key):
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {key!r}."
        )
    try:
        draft = store.draft_for(key)
    except (KeyError, ValueError, OSError):
        # A file that will not parse is still a file someone may want gone.
        draft = runs_module.blank_draft()
        draft.report_type = key
        draft.title = key

    return _render_editor(
        request,
        runs_module.editor_context(
            draft,
            mode="delete",
            key=key,
            delete_info={
                "title": draft.title or key,
                "key": key,
                "path": str(store.template_path(key)),
                "n_runs": store.runs_using_template(key),
                "trash_dir": str(store.trash_dir),
            },
        ),
    )


@router.post(
    "/templates/{template_key}/delete", name="template_delete", include_in_schema=False
)
def template_delete(request: Request, template_key: str) -> Response:
    store = get_store()
    key = _key_or_404(template_key)
    try:
        store.delete_template(key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {key!r}."
        )
    except (ValueError, OSError) as exc:
        raise StarletteHTTPException(
            status_code=422,
            detail=(
                f"{key} could not be moved to the trash folder: {exc}. "
                "Nothing was deleted."
            ),
        )
    return _see_other(f"/?deleted={quote(key, safe='')}")


@router.post(
    "/templates/{template_key}/undelete",
    name="template_undelete",
    include_in_schema=False,
)
def template_undelete(request: Request, template_key: str) -> Response:
    store = get_store()
    key = _key_or_404(template_key)
    try:
        store.undelete_template(key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404,
            detail=f"There is nothing in the trash folder for {key!r}.",
        )
    except (ValueError, OSError) as exc:
        raise StarletteHTTPException(
            status_code=409,
            detail=f"{key} could not be put back: {exc}",
        )
    return _see_other(f"/?restored={quote(key, safe='')}")


@router.get("/templates/{template_key}/raw", name="template_raw", include_in_schema=False)
def template_raw(request: Request, template_key: str) -> Response:
    store = get_store()
    key = _key_or_404(template_key)
    try:
        text = store.raw_template_text(key)
    except (KeyError, OSError):
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {key!r}."
        )
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/runs", response_class=HTMLResponse, name="run_list", include_in_schema=False)
def run_list(request: Request) -> HTMLResponse:
    store = get_store()
    filter_q = (request.query_params.get("q") or "").strip()
    group = request.query_params.get("group") in ("1", "true", "yes", "on")

    summaries = store.list_runs(limit=200)
    if filter_q:
        needle = filter_q.lower()
        summaries = [s for s in summaries if needle in _haystack(s)]
    if group:
        summaries = sorted(
            summaries,
            key=lambda s: ((s.primary_input or "￿").lower(), s.created_at),
            reverse=False,
        )
        # newest first inside each group
        grouped: list[Any] = []
        bucket: list[Any] = []
        current: str | None = None
        for s in summaries:
            key = s.primary_input or ""
            if key != current:
                grouped.extend(sorted(bucket, key=lambda x: x.created_at, reverse=True))
                bucket = []
                current = key
            bucket.append(s)
        grouped.extend(sorted(bucket, key=lambda x: x.created_at, reverse=True))
        summaries = grouped

    return _render(
        request,
        "gallery.html",
        _gallery_context(
            view="runs",
            runs=summaries,
            filter_q=filter_q,
            group_by_compound=group,
        ),
        nav_active="runs",
    )


@router.get(
    "/new/{template_key}",
    response_class=HTMLResponse,
    name="new_run",
    include_in_schema=False,
)
def new_run(request: Request, template_key: str) -> HTMLResponse:
    store = get_store()
    try:
        card = store.get_template(template_key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {template_key!r}."
        )

    from_run_id = (request.query_params.get("from") or "").strip() or None
    values: dict[str, str] = {}
    evidence_folder = ""
    if from_run_id:
        try:
            prior = store.get(from_run_id)
        except KeyError:
            from_run_id = None
        else:
            values = dict(prior.inputs)
            evidence_folder = prior.evidence_folder

    # any query params that name a real field win (the preflight shim uses them)
    for f in card.form_fields if card.ok else []:
        if f.binding_id in request.query_params:
            values[f.binding_id] = request.query_params[f.binding_id]
    if "evidence_folder" in request.query_params:
        evidence_folder = request.query_params["evidence_folder"]

    ctx = _new_run_context(
        store,
        template_key,
        values=values,
        evidence_folder=evidence_folder,
        from_run_id=from_run_id,
    )
    status = 200 if card.ok else 422
    return _render(request, "new_run.html", ctx, nav_active="gallery", status_code=status)


@router.post("/runs", name="create_run", include_in_schema=False)
async def create_run(request: Request) -> Response:
    store = get_store()
    form = await request.form()
    template_key = str(form.get("template_key") or "").strip()

    try:
        card = store.get_template(template_key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {template_key!r}."
        )

    evidence_folder = str(form.get("evidence_folder") or "").strip()
    raw = {f.binding_id: str(form.get(f.binding_id) or "") for f in card.form_fields}

    cleaned, field_errors = store.validate_inputs(template_key, raw)

    if not field_errors:
        try:
            record = store.create(
                template_key, cleaned, evidence_folder or None
            )
        except ValueError as exc:
            report = store.preflight(template_key, cleaned, evidence_folder or None)
            field_errors = _blocker_field_errors(report)
            if not field_errors:
                field_errors = {"__run__": str(exc)}
        else:
            return _see_other(request.url_for("run_detail", run_id=record.run_id))

    ctx = _new_run_context(
        store,
        template_key,
        values=raw,
        field_errors=field_errors,
        evidence_folder=evidence_folder,
    )
    return _render(
        request, "new_run.html", ctx, nav_active="gallery", status_code=422
    )


@router.post(
    "/runs/{run_id}/preflight", name="rerun_preflight", include_in_schema=False
)
def rerun_preflight(request: Request, run_id: str) -> Response:
    store = get_store()
    try:
        record = store.get(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")
    params = {"from": run_id}
    params.update(record.inputs)
    if record.evidence_folder:
        params["evidence_folder"] = record.evidence_folder
    target = str(request.url_for("new_run", template_key=record.template_key))
    return _see_other(f"{target}?{urlencode(params)}")


@router.get(
    "/runs/{run_id}",
    response_class=HTMLResponse,
    name="run_detail",
    include_in_schema=False,
)
def run_detail(request: Request, run_id: str) -> HTMLResponse:
    store = get_store()
    try:
        record = store.get(run_id)
        summary = store.summary(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")

    tab = (request.query_params.get("tab") or "draft").lower()
    if tab not in _VALID_TABS:
        tab = "draft"

    draft = None
    if summary.terminal:
        draft = store.draft_view(run_id)
        # draft_view() bumps the record version + recomputes the summary metrics
        summary = store.summary(run_id)
    else:
        tab = "sections"

    partial = bool(
        summary.status in ("failed", "cancelled", "interrupted")
        and any(s.status in ("passed", "failed") for s in record.sections)
    )

    response = _render(
        request,
        "run.html",
        {
            "run": summary,
            "record": record,
            "tab": tab,
            "draft": draft,
            "poll_url": str(request.url_for("api_run_progress", run_id=run_id)),
            "tabs": _tab_items(request, run_id),
            "partial": partial,
        },
        nav_active="runs",
    )
    if not summary.terminal:
        _no_store(response)
    return response


@router.post("/runs/{run_id}/cancel", name="cancel_run", include_in_schema=False)
def cancel_run(request: Request, run_id: str) -> Response:
    store = get_store()
    try:
        store.get(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")
    store.cancel(run_id)
    return _see_other(request.url_for("run_detail", run_id=run_id))


@router.post("/runs/{run_id}/delete", name="delete_run", include_in_schema=False)
def delete_run(request: Request, run_id: str) -> Response:
    get_store().delete(run_id)
    return _see_other(request.url_for("run_list"))


@router.get(
    "/runs/{run_id}/source/{doc_id:path}", name="run_source", include_in_schema=False
)
def run_source(request: Request, run_id: str, doc_id: str) -> Response:
    path = get_store().source_path(run_id, doc_id)
    if path is None:
        raise StarletteHTTPException(
            status_code=404,
            detail="That source file is not inside this run's evidence folder.",
        )
    return FileResponse(str(path), filename=path.name)


@router.get("/runs/{run_id}/export.md", name="export_md", include_in_schema=False)
def export_md(request: Request, run_id: str) -> Response:
    store = get_store()
    try:
        text = store.markdown_export(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="report-{run_id}.md"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/runs/{run_id}/citations.csv",
    name="export_citations_csv",
    include_in_schema=False,
)
def export_citations_csv(request: Request, run_id: str) -> Response:
    store = get_store()
    try:
        text = store.citations_csv(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")
    return PlainTextResponse(
        text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="citations-{run_id}.csv"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@router.get("/api/templates", name="api_templates")
def api_templates() -> JSONResponse:
    runnable, unavailable = get_store().list_templates()
    return JSONResponse(
        {
            "count": len(runnable),
            "templates": [c.to_dict() for c in runnable],
            "unavailable": [c.to_dict() for c in unavailable],
        }
    )


@router.get("/api/templates/{template_key}", name="api_template_detail")
def api_template_detail(template_key: str) -> JSONResponse:
    store = get_store()
    try:
        card = store.get_template(template_key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {template_key!r}."
        )
    payload = card.to_dict()
    payload["preflight"] = store.preflight(
        template_key, store.default_inputs(template_key) if card.ok else {}
    ).to_dict()
    return JSONResponse(payload, status_code=200 if card.ok else 422)


@router.get("/api/runs", name="api_run_list")
def api_run_list() -> JSONResponse:
    summaries = get_store().list_runs(limit=200)
    return _no_store(
        JSONResponse(
            {"count": len(summaries), "runs": [s.to_dict() for s in summaries]}
        )
    )


@router.post("/api/runs", name="api_create_run", status_code=201)
async def api_create_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    template_key = str(body.get("template_key") or "").strip()
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        inputs = {}
    inputs = {str(k): str(v) for k, v in inputs.items()}
    evidence_folder = body.get("evidence_folder") or None

    store = get_store()
    try:
        store.get_template(template_key)
    except KeyError:
        raise StarletteHTTPException(
            status_code=404, detail=f"No report template named {template_key!r}."
        )
    try:
        record = store.create(template_key, inputs, evidence_folder)
    except ValueError as exc:
        raise StarletteHTTPException(status_code=422, detail=str(exc))

    return _no_store(
        JSONResponse(
            {
                "run_id": record.run_id,
                "status": record.status,
                "poll_url": f"/api/runs/{record.run_id}/progress",
                "html_url": f"/runs/{record.run_id}",
            },
            status_code=201,
        )
    )


@router.get("/api/runs/{run_id}/progress", name="api_run_progress")
def api_run_progress(request: Request, run_id: str) -> Response:
    try:
        payload = get_store().progress_payload(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")

    etag = '"{}"'.format(payload.get("version", 0))
    if request.headers.get("if-none-match") == etag:
        return _no_store(Response(status_code=304, headers={"ETag": etag}))
    return _no_store(JSONResponse(payload, headers={"ETag": etag}))


@router.get("/api/runs/{run_id}", name="api_run_result")
def api_run_result(run_id: str) -> JSONResponse:
    store = get_store()
    try:
        payload = store.result_payload(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")
    except RunNotTerminal as exc:
        return _no_store(
            JSONResponse(
                {
                    "status": str(exc),
                    "poll_url": f"/api/runs/{run_id}/progress",
                },
                status_code=409,
            )
        )
    return _no_store(JSONResponse(payload))


@router.post("/api/runs/{run_id}/cancel", name="api_cancel_run", status_code=202)
def api_cancel_run(run_id: str) -> JSONResponse:
    store = get_store()
    try:
        store.get(run_id)
    except KeyError:
        raise StarletteHTTPException(status_code=404, detail=f"Unknown run {run_id!r}.")
    store.cancel(run_id)
    return _no_store(
        JSONResponse(
            {"run_id": run_id, "status": store.get(run_id).status}, status_code=202
        )
    )


@router.post("/api/generate/ib-demo", name="api_ib_demo")
def api_ib_demo() -> dict[str, object]:
    """Run the synthetic Investigator's Brochure pipeline end-to-end with the
    stub LLM.  Same response shape the dev server has always returned."""
    return get_store().ib_demo()


# ---------------------------------------------------------------------------
# legacy shims (§C16) — 307 preserves method and body
# ---------------------------------------------------------------------------


@router.get(
    "/templates", name="legacy_templates", deprecated=True, include_in_schema=False
)
def legacy_templates() -> RedirectResponse:
    return RedirectResponse("/api/templates", status_code=307)


@router.post(
    "/generate/ib-demo",
    name="legacy_ib_demo",
    deprecated=True,
    include_in_schema=False,
)
def legacy_ib_demo() -> RedirectResponse:
    return RedirectResponse("/api/generate/ib-demo", status_code=307)


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------

_ERROR_TITLES = {
    404: "That page is not here",
    405: "That action is not allowed here",
    409: "That could not be done right now",
    422: "That request could not be used",
    500: "Something went wrong on this machine",
    503: "That screen is not part of this build",
}


def _error_page(
    request: Request, status_code: int, message: str, detail: str = ""
) -> Response:
    error = {
        "code": status_code,
        "title": _ERROR_TITLES.get(status_code, "Something went wrong"),
        "message": message,
        "detail": detail,
        "links": [("Back to report types", "/"), ("Run history", "/runs")],
    }
    try:
        return _render(
            request,
            "base.html",
            {"error": error},
            nav_active="",
            status_code=status_code,
        )
    except Exception:  # pragma: no cover - the error page must never fail
        return PlainTextResponse(
            f"{status_code} — {message}", status_code=status_code
        )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    message = str(exc.detail) if exc.detail else ""
    if _wants_json(request):
        return JSONResponse(
            {"error": {"code": exc.status_code, "message": message}},
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store"},
        )
    return _error_page(request, exc.status_code, message)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    detail = "".join(traceback.format_exception(exc))[-4000:]
    if _wants_json(request):
        return JSONResponse(
            {
                "error": {
                    "code": 500,
                    "kind": type(exc).__name__,
                    "message": str(exc),
                    "detail": detail,
                }
            },
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )
    return _error_page(request, 500, str(exc) or type(exc).__name__, detail)


def install_error_handlers(app: Any) -> None:
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
