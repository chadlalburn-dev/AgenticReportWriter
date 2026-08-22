"""A full report drafted through ClaudeCliLlmClient, with the CLI faked.

The engine cannot be proved on this machine: signing the Claude Code CLI in is
an interactive browser flow, so until someone runs `claude` and `/login` the app
correctly falls back to the stub and every other test exercises the stub path.
That leaves the question this file answers — is the CLI actually wired into the
pipeline, or does it merely construct?

So the fake sits at exactly one seam: `subprocess.run`. Everything above it is
the real thing — binding resolution, retrieval, prompt composition, JSON
parsing, claim anchoring, citation enforcement, the safety gate, the audit chain
and the draft view. If this passes, the only untested link left is whether the
real CLI returns JSON, and the recorded prompts prove we asked it for JSON in
the way the schema requires.

The fake reads the schema out of the prompt it is handed and answers that,
rather than returning a fixed blob. A canned response would pass even if the
pipeline sent entirely the wrong prompt.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app
from shared.llm import ClaudeCliLlmClient

TEMPLATE = "nonclinical_safety_summary"
RUN_TIMEOUT_S = 180.0

#: The sentence the fake engine returns. Asserted on the rendered page, because
#: a silent fallback to the stub would still produce a draft that passes every
#: other check in this file.
FAKE_PROSE = (
    "In the pivotal 28-day study the no-observed-adverse-effect level was "
    "established, and exposure margins were calculated against the anticipated "
    "clinical dose."
)


@dataclass
class _Completed:
    stdout: str
    stderr: str = ""
    returncode: int = 0


class FakeCli:
    """Answers whichever schema the prompt asks for; remembers every prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    #: cmd.exe's command-line ceiling. The npm shim is a `.cmd`, so every real
    #: invocation goes through it.
    _CMD_LINE_MAX = 8191

    def __call__(self, argv, **kwargs):  # noqa: ANN001, ANN003
        self.argvs.append(list(argv))
        prompt = kwargs.get("input")
        assert prompt is not None, (
            "the prompt must be sent on stdin; passing it in argv truncates at "
            "cmd.exe's 8191-character limit and the CLI answers the fragment"
        )
        # Enforce the real platform limit the earlier fake ignored. That
        # omission is exactly why this suite passed while the first live run
        # failed: a fake that accepts any argv length cannot see the ceiling
        # the shim imposes.
        joined = " ".join(str(a) for a in argv)
        assert len(joined) <= self._CMD_LINE_MAX, (
            f"command line is {len(joined)} chars, over cmd.exe's "
            f"{self._CMD_LINE_MAX}; the real call would fail"
        )
        self.prompts.append(prompt)
        return _Completed(stdout=json.dumps(self._answer(prompt)))

    def _answer(self, prompt: str) -> dict:
        """The real schemas, not approximations of them.

        Getting these wrong is instructive rather than harmless: the first
        version of this fake returned {"outline": [], "notes": []} for the plan
        and the run failed with four Pydantic validation errors, which is the
        pipeline doing its job. Both shapes are asserted below so a schema
        change breaks this file loudly instead of silently weakening it.
        """
        if "PlanOutput" in prompt:
            return {
                "overall_summary": "Draft the nonclinical safety package from "
                "the retrieved study data.",
                "section_plans": [
                    {"section_id": section_id, "intended_assertions": []}
                    for section_id in self._section_ids(prompt)
                ],
            }
        if "CritiqueOutput" in prompt:
            return {"verdict": "pass", "issues": []}
        offered = self._citation_ids(prompt)
        claims = [{"text": FAKE_PROSE, "citation_ids": offered[:1]}] if offered else []
        return {"paragraphs": [{"text": FAKE_PROSE, "claims": claims}]}

    @staticmethod
    def _section_ids(prompt: str) -> list[str]:
        found: list[str] = []
        for value in re.findall(r"section_id[=:]\s*\"?([A-Za-z0-9_.:-]+)", prompt):
            if value not in found:
                found.append(value)
        return found

    @staticmethod
    def _citation_ids(prompt: str) -> list[str]:
        """The citation ids the prompt actually offered.

        Inventing one is the thing the pipeline is built to reject, so the fake
        must not do it — otherwise a passing test would prove nothing.
        """
        found: list[str] = []
        # The filler renders each chunk and table as "[citation_id=X] ...",
        # so that is the form to read back.
        for pattern in (
            r"\[citation_id=([^\]\s]+)\]",
            r'"citation_id"\s*:\s*"([^"]+)"',
        ):
            for value in re.findall(pattern, prompt):
                value = value.strip()
                if value and value not in found:
                    found.append(value)
        return found


@pytest.fixture
def cli_engine(monkeypatch) -> FakeCli:
    """Force the app onto the CLI engine, with the subprocess boundary faked."""
    fake = FakeCli()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setenv("REPORTGEN_CLAUDE_BIN", __file__)
    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    # Cold path. This fake patches `subprocess.run`, and the warm pool uses
    # `subprocess.Popen` — different functions on purpose, so patching one does
    # not silence the other. Without this the app reaches real Popen and tries
    # to execute this .py file. The pool has its own suite in test_cli_pool.py.
    monkeypatch.setenv(runs_module.CLI_POOL_ENV, "0")
    runs_module.shutdown_llm_client()
    runs_module.reset_engine_cache()
    # check() would otherwise consume a prompt that is not a generation prompt
    monkeypatch.setattr(ClaudeCliLlmClient, "check", lambda self: None)
    yield fake
    runs_module.reset_engine_cache()


def _start(client: TestClient) -> str:
    """Start a run the way a browser does: submit the page's own form."""
    page = client.get(f"/new/{TEMPLATE}")
    assert page.status_code == 200, page.text[:800]
    body = page.text
    action = re.search(r'<form method="post" action="([^"]+)"', body).group(1)
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", body[body.index(action) :]):
        name = re.search(r'name="([^"]+)"', tag)
        if not name or 'type="submit"' in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        fields[name.group(1)] = value.group(1) if value else ""
    response = client.post(action, data=fields, follow_redirects=False)
    assert response.status_code == 303, response.text[:800]
    return response.headers["location"].rstrip("/").rsplit("/", 1)[-1]


def _await_terminal(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    payload: dict = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/api/runs/{run_id}/progress").json()
        if payload["terminal"]:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} never finished: {payload}")


@pytest.fixture
def finished_run(cli_engine: FakeCli):
    """One run, drafted through the CLI engine, shared by the assertions below."""
    client = TestClient(app)
    run_id = _start(client)
    payload = _await_terminal(client, run_id)
    return client, run_id, payload, cli_engine


# --- wiring ----------------------------------------------------------------


def test_the_engine_the_app_picks_is_the_cli(cli_engine: FakeCli):
    engine = runs_module.resolve_engine()
    assert engine.kind == "cli"
    assert engine.real is True, "the CLI engine must not describe itself as placeholder"
    assert isinstance(runs_module.build_llm_client(), ClaudeCliLlmClient)


def test_a_whole_report_drafts_through_the_cli(finished_run):
    client, run_id, payload, fake = finished_run
    # `completed`, not "completed or failed". Accepting a failure here would
    # have let the first version of this file pass while the run died on a
    # schema mismatch.
    assert payload["status"] == "completed", payload
    assert fake.prompts, "the run finished without ever invoking the CLI"
    view = runs_module.get_store().draft_view(run_id)
    assert view is not None and not view.hollow, "no draft was produced"
    assert len(view.sections) > 1, "a multi-section template drafted one section"
    assert view.trust.n_claims_cited == view.trust.n_claims, (
        f"only {view.trust.n_claims_cited} of {view.trust.n_claims} claims "
        "carry a citation, and the fake only ever cites ids the prompt offered"
    )


def test_the_cli_wrote_the_prose_not_the_stub(finished_run):
    """The link that matters. A silent fallback to the stub would still render a
    draft and still pass every other assertion in this file."""
    client, run_id, _payload, _fake = finished_run
    body = client.get(f"/runs/{run_id}?tab=draft").text
    assert "no-observed-adverse-effect level" in body, (
        "the engine's own words are not on the page — the run fell back"
    )
    assert "local dev-server stub" not in body, "stub prose leaked into a CLI run"


def test_citations_survive_the_cli_path(finished_run):
    """Provenance is the product; changing the engine must not cost it."""
    _client, run_id, _payload, _fake = finished_run
    view = runs_module.get_store().draft_view(run_id)
    assert view.trust.n_claims, "no claims were extracted from the CLI's output"
    assert view.ledger, "no source ledger for a CLI run"


def test_the_page_says_the_cli_wrote_it(finished_run):
    """Disclosure follows the engine, not a default."""
    client, run_id, _payload, _fake = finished_run
    body = client.get(f"/runs/{run_id}?tab=draft").text
    assert "local Claude" in body
    assert "PLACEHOLDER" not in body, "a real run described itself as placeholder"


# --- what we actually send -------------------------------------------------


def test_the_prompt_asks_for_the_schema_the_pipeline_will_validate(finished_run):
    """The CLI has no structured-output flag, so the schema goes in as text.

    Omit it and the real CLI returns prose, every section fails validation and
    the run produces nothing — a failure the fake makes invisible unless it is
    asserted here.
    """
    _client, _run_id, _payload, fake = finished_run
    fills = [p for p in fake.prompts if "FillOutput" in p]
    assert fills, "no section was ever asked to fill"
    for prompt in fills:
        assert "JSON" in prompt
        assert "citation_id" in prompt
        assert "invent" in prompt.lower(), (
            "the no-invented-citations instruction is missing from the prompt"
        )


def test_dangerous_tools_are_disabled_on_every_call(finished_run):
    """This subprocess runs on the analyst's own machine, and drafting a report
    never needs to edit a file or run a command."""
    _client, _run_id, _payload, fake = finished_run
    assert fake.argvs, "no invocations to inspect"
    for argv in fake.argvs:
        flat = " ".join(str(a) for a in argv)
        assert "--disallowed-tools" in flat
        for tool in ("Bash", "Edit", "Write"):
            assert tool in flat, f"{tool} was not disallowed on one call"
        assert "--permission-mode dontAsk" in flat
