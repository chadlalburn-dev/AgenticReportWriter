"""The local-Claude generation engine and its disclosure in the UI.

Two things are load-bearing here and both were found by probing the real CLI
rather than by reading its docs:

  * An unauthenticated `claude -p` prints "Not logged in" and **exits 0**, so
    a client that trusts the exit code reports success on total failure.
  * The CLI waits ~3s for piped stdin and emits a warning into stdout unless
    stdin is closed explicitly.

The engine is also the one thing a reader must never have to guess about: if
placeholder prose can be mistaken for a model's words, the product's provenance
claim is void. So the disclosure is tested as a contract, not as decoration.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from services.api_gateway import runs as runs_module
from services.api_gateway.main import app
from shared.llm import (
    ClaudeCliConfig,
    ClaudeCliLlmClient,
    ClaudeCliUnavailable,
    LlmMessage,
    LlmRequest,
    LlmRole,
    ModelTier,
    StubLlmClient,
    StructuredOutputError,
    find_claude_binary,
)
from shared.llm.claude_cli import _looks_like_real_data, _strip_fence


@dataclass
class _Proc:
    """Stand-in for CompletedProcess."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _request(schema: str | None = "FillOutput", text: str = "draft a section") -> LlmRequest:
    return LlmRequest(
        tier=ModelTier.FILL,
        system="you are a medical writer",
        messages=[LlmMessage(role=LlmRole.USER, content=text)],
        response_schema_name=schema,
        response_schema_json={"type": "object"} if schema else None,
    )


@pytest.fixture
def client(monkeypatch) -> ClaudeCliLlmClient:
    """A client wired to a fake binary path so no real CLI is spawned."""
    monkeypatch.setenv("REPORTGEN_CLAUDE_BIN", __file__)  # any existing file
    return ClaudeCliLlmClient(ClaudeCliConfig(binary=__file__))


# --- JSON extraction --------------------------------------------------------


def test_fence_is_stripped_so_a_good_generation_is_not_failed_on_formatting():
    assert _strip_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_fence('```\n{"a":1}\n```') == '{"a":1}'


def test_prose_around_the_object_is_discarded():
    assert _strip_fence('Sure! {"a":1} Hope that helps.') == '{"a":1}'


def test_bare_json_is_returned_unchanged():
    assert _strip_fence('{"a":1}') == '{"a":1}'


# --- the exit-0-on-auth-failure trap ---------------------------------------


def test_not_logged_in_raises_even_though_the_cli_exits_zero(client, monkeypatch):
    """The trap this whole module exists for."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Proc(stdout="Not logged in · Please run /login", returncode=0),
    )
    with pytest.raises(ClaudeCliUnavailable, match="not signed in"):
        client.generate(_request())


def test_the_login_error_tells_the_user_what_to_do(client, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Proc(stdout="Not logged in · Please run /login", returncode=0),
    )
    with pytest.raises(ClaudeCliUnavailable) as exc:
        client.check()
    message = str(exc.value)
    assert "/login" in message, "the fix must be in the message"
    assert "claude" in message


@pytest.mark.parametrize(
    "output",
    ["Invalid API key", "authentication_error: bad token", "Credit balance is too low"],
)
def test_other_zero_exit_failures_are_caught(client, monkeypatch, output):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(stdout=output))
    with pytest.raises(ClaudeCliUnavailable):
        client.generate(_request())


def test_a_nonzero_exit_is_reported_with_its_output(client, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(stdout="boom", returncode=2)
    )
    with pytest.raises(ClaudeCliUnavailable, match="exited 2"):
        client.generate(_request())


# --- stdin must be closed --------------------------------------------------


def test_the_prompt_travels_on_stdin_not_in_argv(client, monkeypatch):
    """The regression that killed the first real run.

    A report prompt is the section instruction plus every retrieved chunk and
    table — tens of thousands of characters. The npm shim is a `.cmd`, so the
    call routes through cmd.exe, which caps a command line at 8191 characters.
    Past that it either dies with "The command line is too long" or arrives
    truncated, and the CLI politely answers the fragment: the first live run
    failed with "your message may have been cut off — I only received the
    template title".

    This also subsumes the old reason stdin was DEVNULL. The CLI waits ~3s for
    piped input and warns into the captured output if the pipe is left open;
    `input=` writes the prompt and closes it, which satisfies both.
    """
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return _Proc(stdout='{"paragraphs":[]}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.generate(_request(text="x" * 20000))

    assert isinstance(seen.get("input"), str), "the prompt is not on stdin"
    assert len(str(seen["input"])) > 8191, "this test is not exercising the ceiling"
    argv = " ".join(str(a) for a in seen["argv"])
    assert len(argv) <= 8191, f"argv is {len(argv)} chars, over cmd.exe's limit"
    assert "x" * 100 not in argv, "the prompt leaked into the command line"


def test_dangerous_tools_are_disallowed(client, monkeypatch):
    """Drafting a report never needs to edit files or run commands."""
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Proc(stdout='{"paragraphs":[]}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.generate(_request())
    argv = " ".join(str(a) for a in seen["argv"])
    assert "--disallowed-tools" in argv
    for tool in ("Bash", "Edit", "Write"):
        assert tool in argv


# --- structured output -----------------------------------------------------


def test_valid_json_is_parsed(client, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Proc(stdout='{"paragraphs":[{"text":"x","claims":[]}]}'),
    )
    response = client.generate(_request())
    assert response.parsed_json == {"paragraphs": [{"text": "x", "claims": []}]}


def test_prose_where_json_was_required_fails_the_section(client, monkeypatch):
    """Better to fail one section than to let unparsed prose into the draft."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(stdout="Here is your section!")
    )
    with pytest.raises(StructuredOutputError):
        client.generate(_request())


def test_the_schema_and_the_no_invented_citations_rule_reach_the_prompt(client, monkeypatch):
    seen: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        # stdin, not argv — see test_the_prompt_travels_on_stdin_not_in_argv
        seen["prompt"] = kwargs.get("input") or ""
        return _Proc(stdout='{"paragraphs":[]}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.generate(_request())
    assert "FillOutput" in seen["prompt"]
    assert "citation_id" in seen["prompt"]
    assert "invent" in seen["prompt"].lower()


# --- the data-governance guard --------------------------------------------


def test_real_data_is_refused_by_default(client, monkeypatch):
    """The local CLI leaves the sanctioned Onyx path, so real GSK data is a
    human decision, not a default."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(stdout="{}"))
    marked = _request(text="data-classification: real\n\nsome study data")
    with pytest.raises(ClaudeCliUnavailable, match="real GSK data"):
        client.generate(marked)


def test_real_data_runs_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _Proc(stdout='{"paragraphs":[]}')
    )
    permissive = ClaudeCliLlmClient(
        ClaudeCliConfig(binary=__file__, allow_real_data=True)
    )
    assert permissive.generate(
        _request(text="data-classification: real")
    ).parsed_json == {"paragraphs": []}


def test_the_guard_is_honest_about_being_a_tripwire():
    """It catches an explicit marker and nothing else — not a classifier."""
    assert _looks_like_real_data(_request(text="data-classification: real"))
    assert not _looks_like_real_data(_request(text="patient 4 had a NOAEL of 30"))


# --- binary discovery ------------------------------------------------------


def test_an_explicit_path_wins(monkeypatch):
    monkeypatch.delenv("REPORTGEN_CLAUDE_BIN", raising=False)
    assert find_claude_binary(__file__) == __file__


def test_the_env_override_is_honoured(monkeypatch):
    monkeypatch.setenv("REPORTGEN_CLAUDE_BIN", __file__)
    assert find_claude_binary() == __file__


def test_a_missing_binary_raises_with_the_env_var_named(monkeypatch, tmp_path):
    monkeypatch.delenv("REPORTGEN_CLAUDE_BIN", raising=False)
    monkeypatch.setattr("shared.llm.claude_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("shared.llm.claude_cli.Path.home", staticmethod(lambda: tmp_path))
    with pytest.raises(ClaudeCliUnavailable, match="REPORTGEN_CLAUDE_BIN"):
        ClaudeCliLlmClient()


# --- engine resolution + disclosure ---------------------------------------


def test_engine_resolution_always_yields_a_usable_engine():
    engine = runs_module.resolve_engine()
    assert engine.kind in ("cli", "stub")
    assert engine.label and engine.detail
    assert isinstance(engine.real, bool)


def test_forcing_the_stub_is_honest_about_being_placeholder(monkeypatch):
    monkeypatch.setenv(runs_module.ENGINE_ENV, "stub")
    engine = runs_module.resolve_engine()
    assert engine.kind == "stub"
    assert engine.real is False
    assert "PLACEHOLDER" in engine.detail.upper(), (
        "the stub must announce itself as placeholder, not sound like a model"
    )


def test_forcing_the_cli_surfaces_the_reason_when_it_cannot_run(monkeypatch):
    """`auto` may fall back silently; `cli` must not — an explicit request that
    cannot be honoured has to say why."""
    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    monkeypatch.setattr(
        "services.api_gateway.runs.ClaudeCliLlmClient",
        lambda *a, **k: (_ for _ in ()).throw(ClaudeCliUnavailable("no login")),
    )
    with pytest.raises(ClaudeCliUnavailable):
        runs_module.resolve_engine()


def test_the_stub_engine_is_disclosed_on_every_page(monkeypatch):
    """The trust contract: placeholder prose is never presented as a model's."""
    monkeypatch.setenv(runs_module.ENGINE_ENV, "stub")
    http = TestClient(app)
    for url in ("/", "/runs", "/templates"):
        body = http.get(url).text
        assert "stub text" in body, url
        assert "is-stub" in body, f"{url} does not mark the stub state"


# --- resolving the engine must never block a page render ------------------


def test_a_page_render_never_waits_for_the_cli(monkeypatch):
    """The regression this cache exists for.

    Every page names the engine, and the only way to ask the CLI whether it is
    signed in is to run it — a ~27s round trip on this machine. Resolving per
    request made the whole app that slow, and the test suite went from seconds
    to minutes before it was caught.
    """
    import time as _time

    monkeypatch.delenv(runs_module.ENGINE_ENV, raising=False)
    runs_module.reset_engine_cache()

    def slow_check(self):
        _time.sleep(5)
        raise ClaudeCliUnavailable("not signed in")

    monkeypatch.setattr(ClaudeCliLlmClient, "check", slow_check)

    started = _time.perf_counter()
    engine = runs_module.resolve_engine()
    elapsed = _time.perf_counter() - started

    assert elapsed < 1.0, f"resolve_engine() blocked for {elapsed:.1f}s"
    assert engine.kind == "stub", (
        "until the probe lands the engine must claim the stub — over-claiming "
        "real prose is the unsafe direction of error"
    )


def test_the_second_call_is_served_from_cache(monkeypatch):
    monkeypatch.setenv(runs_module.ENGINE_ENV, "stub")
    runs_module.reset_engine_cache()
    calls: list[str] = []
    real_probe = runs_module._probe_engine
    monkeypatch.setattr(
        runs_module,
        "_probe_engine",
        lambda choice: (calls.append(choice), real_probe(choice))[1],
    )
    runs_module.resolve_engine()
    runs_module.resolve_engine()
    runs_module.resolve_engine()
    assert len(calls) == 1, f"probed {len(calls)} times for three renders"


def test_flipping_the_env_var_takes_effect_immediately(monkeypatch):
    """The choice is part of the cache key, so a forced engine is never stale."""
    runs_module.reset_engine_cache()
    monkeypatch.setenv(runs_module.ENGINE_ENV, "stub")
    assert runs_module.resolve_engine().kind == "stub"
    monkeypatch.setattr(
        ClaudeCliLlmClient, "check", lambda self: None
    )
    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    assert runs_module.resolve_engine().kind == "cli"


def test_starting_a_run_gets_a_definite_answer(monkeypatch):
    """`resolve_engine_now()` probes: a run takes minutes and its entire output
    depends on the answer, so this is the one place worth waiting for."""
    monkeypatch.delenv(runs_module.ENGINE_ENV, raising=False)
    runs_module.reset_engine_cache()
    probed: list[str] = []
    monkeypatch.setattr(
        ClaudeCliLlmClient,
        "check",
        lambda self: probed.append("yes"),
    )
    assert runs_module.resolve_engine_now().kind == "cli"
    assert probed, "resolve_engine_now() returned without probing"


# --- disclosure on the screen where it changes a decision ----------------


def _setup_url(http: TestClient) -> str:
    """The run-setup URL for whichever template the library lists first."""
    match = re.search(r'href="(/new/[^"]+)"', http.get("/templates").text)
    assert match, "no template to set up a run for"
    return match.group(1)


def test_the_setup_screen_states_which_engine_will_write_the_prose(monkeypatch):
    monkeypatch.setenv(runs_module.ENGINE_ENV, "stub")
    runs_module.reset_engine_cache()
    http = TestClient(app)
    body = http.get(_setup_url(http)).text
    assert "ti-enginebox" in body
    assert "Prose will be placeholder text" in body, (
        "the setup screen must say this before the run, not after"
    )


def test_the_stub_state_carries_the_one_fix_only_the_user_can_apply(monkeypatch):
    """The hint is the whole point: the app cannot log a CLI in itself."""
    monkeypatch.delenv(runs_module.ENGINE_ENV, raising=False)
    runs_module.reset_engine_cache()
    monkeypatch.setattr(
        ClaudeCliLlmClient,
        "check",
        lambda self: (_ for _ in ()).throw(ClaudeCliUnavailable("not signed in")),
    )
    engine = runs_module.resolve_engine_now()
    assert engine.kind == "stub" and engine.hint
    http = TestClient(app)
    body = http.get(_setup_url(http)).text
    assert "/login" in body, "the fix must be on the screen, not only in a log"


def test_starting_a_run_reuses_a_fresh_probe(monkeypatch):
    """The probe costs ~27s. Paying it again per run buys nothing.

    The startup prime has almost always already answered the question, and if
    the CLI died in the last minute, section one fails with the same actionable
    message anyway.
    """
    monkeypatch.delenv(runs_module.ENGINE_ENV, raising=False)
    runs_module.reset_engine_cache()
    probes: list[int] = []
    monkeypatch.setattr(
        ClaudeCliLlmClient, "check", lambda self: probes.append(1)
    )
    first = runs_module.resolve_engine_now()
    second = runs_module.resolve_engine_now()
    assert first.kind == second.kind == "cli"
    assert len(probes) == 1, f"probed {len(probes)} times for two run starts"


def test_the_readiness_probe_has_its_own_ceiling():
    """A section can legitimately take minutes; a one-word prompt cannot.

    They shared one timeout, so a wedged CLI held a run start for 90s.
    """
    config = ClaudeCliConfig()
    assert config.check_timeout_s < config.timeout_s
    assert config.check_timeout_s >= 45, (
        "the observed not-signed-in path takes ~27s, so a tighter ceiling "
        "would report a false timeout"
    )


def test_the_probe_timeout_is_named_in_its_own_error(monkeypatch, tmp_path):
    """A timeout message stating the wrong number sends someone hunting a
    setting that does not exist."""
    client = ClaudeCliLlmClient(ClaudeCliConfig(binary=__file__, check_timeout_s=7.0))

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=7.0)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ClaudeCliUnavailable, match="within 7s"):
        client.check()


# --- discovery must cover how the CLI is actually installed ---------------


def test_the_npm_global_shim_is_discoverable(monkeypatch, tmp_path):
    """`npm install -g @anthropic-ai/claude-code` writes shims to the npm global
    bin, and that directory is not on the user PATH on this machine — it holds
    only Python and WindowsApps. So `shutil.which("claude")` finds nothing with
    the CLI correctly installed, and discovery has to know the layout.
    """
    monkeypatch.delenv("REPORTGEN_CLAUDE_BIN", raising=False)
    monkeypatch.setattr("shared.llm.claude_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("shared.llm.claude_cli.Path.home", staticmethod(lambda: tmp_path))
    shim = tmp_path / "AppData" / "Roaming" / "npm" / "claude.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    assert find_claude_binary() == str(shim)


def test_the_desktop_bundle_is_still_discoverable(monkeypatch, tmp_path):
    """The other real layout, and the newest version wins."""
    monkeypatch.delenv("REPORTGEN_CLAUDE_BIN", raising=False)
    monkeypatch.setattr("shared.llm.claude_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("shared.llm.claude_cli.Path.home", staticmethod(lambda: tmp_path))
    root = tmp_path / "AppData" / "Roaming" / "Claude" / "claude-code"
    for version in ("2.1.100", "2.1.239"):
        exe = root / version / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("x", encoding="utf-8")
    assert find_claude_binary() == str(root / "2.1.239" / "claude.exe")


def test_a_cmd_shim_can_actually_be_executed():
    """CreateProcess routes .cmd through the command interpreter, so subprocess
    drives the shim with no shell=True — which would be an injection risk with
    a path this code did not choose."""
    import subprocess as sp

    from shared.llm.claude_cli import find_claude_binary as find

    binary = find()
    if not binary or not binary.endswith(".cmd"):
        pytest.skip("no .cmd shim installed on this machine")
    result = sp.run(
        [binary, "--version"], capture_output=True, text=True, timeout=90,
        stdin=sp.DEVNULL,
    )
    assert result.returncode == 0
    assert "Claude Code" in (result.stdout or "")


# --- the instruction has to be runnable ----------------------------------


def test_the_login_instruction_names_a_real_command(monkeypatch, tmp_path):
    """It said "run `claude`" unconditionally. That is wrong whenever the CLI is
    off PATH, which is the normal case after an npm global install here — so the
    one instruction the user must follow sent them to a
    CommandNotFoundException. Twice, in practice.
    """
    monkeypatch.delenv(runs_module.ENGINE_ENV, raising=False)
    monkeypatch.setattr("shared.llm.claude_cli.shutil.which", lambda _n: None)
    monkeypatch.setattr("services.api_gateway.runs.shutil.which", lambda _n: None)
    shim = tmp_path / "AppData" / "Roaming" / "npm" / "claude.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr("shared.llm.claude_cli.Path.home", staticmethod(lambda: tmp_path))

    fix = runs_module._fix_for(choice="auto", hint="not signed in")
    assert str(shim) in fix, f"the instruction does not name the found binary: {fix}"
    assert "/login" in fix


def test_the_short_form_is_used_when_the_cli_is_on_path(monkeypatch):
    """Pasting an absolute path when `claude` would do is noise."""
    monkeypatch.setattr("services.api_gateway.runs.shutil.which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr(
        "services.api_gateway.runs.find_claude_binary", lambda: "/opt/weird/claude.cmd"
    )
    fix = runs_module._fix_for(choice="auto", hint="not signed in")
    assert "`claude`" in fix, fix
    assert "/opt/weird" not in fix


def test_the_suite_does_not_depend_on_a_local_cli(monkeypatch):
    """The default engine for tests is the stub, and it must not be accidental.

    `REPORTGEN_ENGINE` defaults to `auto`, which prefers the local CLI. So on
    the day someone installs and authenticates that CLI, the whole suite
    silently starts making live model calls — non-deterministic prose, real
    cost, a network dependency, and a ~23s readiness probe per resolution. That
    is exactly what happened: six tests in test_ui.py errored and one failed on
    a machine where nothing had changed except a successful `claude auth login`.

    conftest pins the stub. This asserts the pin is in force, so removing it
    fails here rather than surfacing as unrelated timeouts somewhere else.
    """
    assert os.environ.get("REPORTGEN_ENGINE") == "stub", (
        "the suite is not pinned to the stub engine; it will use whatever "
        "engine happens to be installed on this machine"
    )
    assert runs_module.resolve_engine().kind == "stub"
    assert isinstance(runs_module.build_llm_client(), StubLlmClient)


def test_a_test_can_still_opt_into_the_real_path(monkeypatch):
    """The pin is a default, not a lock. The CLI suites override it, which is
    how the end-to-end test drives ClaudeCliLlmClient at all."""
    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    monkeypatch.setattr(ClaudeCliLlmClient, "check", lambda self: None)
    runs_module.reset_engine_cache()
    assert runs_module.resolve_engine().kind == "cli"
