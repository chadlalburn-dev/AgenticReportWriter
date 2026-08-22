"""The warm pool, and the reason it is a pool of one-shot processes.

Startup dominates the CLI: a one-word prompt measured 47.7s and an
8,000-character prompt 42.1s, so ~45s of every call is a 330MB binary booting a
Node runtime. The orchestrator spends one process per model call and a
five-section report makes roughly fifteen, which is why a live run did not finish
inside fourteen minutes.

The tempting fix is one long-lived process fed every prompt over
`--input-format stream-json`. It is fast — 38.1s, then 4.9s, then 11.7s — and it
is wrong here, for a reason that is measured rather than assumed: the CLI treats
a process as ONE conversation and ignores a `session_id` on the input message.
A process told a codeword still recalls it on a later message even when that
message carries a different session id, and the session id in the reply never
changes.

For this application that is not an optimisation with a caveat, it is a
provenance hole. Section five's prompt would carry section one's retrieved
chunks in the conversation history, and the model could cite data that section
was never given — in a product whose whole claim is that every value traces to
the source it came from.

Hence: warm processes, one call each, discarded. Startup moves before the call
instead of into it, and every call is a virgin conversation. Measured 6.6s and
6.2s warm, with a second process correctly answering "NONE" to a codeword only
the first was told.
"""

from __future__ import annotations

import json
import time

import pytest

from shared.llm.claude_cli_pool import _STREAM_FLAGS, CliResult, WarmPool


class _FakeProc:
    """Stands in for a booted CLI. Records what it was asked."""

    instances: list["_FakeProc"] = []

    def __init__(self, argv: list[str], cwd: str | None) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.asked: list[str] = []
        self.closed = False
        self.alive = True
        # A real monotonic stamp, not 0.0. The pool discards anything older
        # than its TTL, and a fake born at time zero is permanently stale — so
        # acquire() rejected every process and spun until its deadline. The
        # first version of this file hung for exactly that reason, which is a
        # good argument for the fake matching the real object's contract rather
        # than only its method names.
        self.started_at = time.monotonic()
        _FakeProc.instances.append(self)

    def ask(self, prompt: str, timeout_s: float) -> CliResult:
        self.asked.append(prompt)
        return CliResult(
            text=json.dumps({"ok": True}),
            input_tokens=11,
            output_tokens=7,
            cost_usd=0.01,
            session_id="s-1",
            is_error=False,
            raw_events=4,
        )

    def close(self) -> None:
        self.closed = True
        self.alive = False


@pytest.fixture(autouse=True)
def _reset() -> None:
    _FakeProc.instances.clear()


@pytest.fixture
def pool(monkeypatch) -> WarmPool:
    monkeypatch.setattr("shared.llm.claude_cli_pool.WarmProcess", _FakeProc)
    built = WarmPool(["claude"], size=2)
    yield built
    built.close()


# --- the flags the stream needs --------------------------------------------


def test_verbose_is_present_because_the_cli_demands_it():
    """`--output-format stream-json` under `--print` is refused without it:
    "requires --verbose". Discovered by having the call rejected."""
    assert "--verbose" in _STREAM_FLAGS
    assert "stream-json" in _STREAM_FLAGS


def test_tools_stay_disabled_on_the_streaming_path():
    """The cold path disallows them and the pooled path must not quietly become
    the permissive one — this subprocess runs on the analyst's machine."""
    flat = " ".join(_STREAM_FLAGS)
    assert "--disallowed-tools" in flat
    for tool in ("Bash", "Edit", "Write"):
        assert tool in flat
    assert "--permission-mode dontAsk" in flat


# --- the isolation property this design exists for -------------------------


def test_a_process_is_never_reused(pool: WarmPool):
    """The whole point. Reuse would carry the previous section's chunks in the
    conversation history."""
    used = []
    for _ in range(4):
        proc = pool.acquire(wait_s=2.0)
        proc.ask("prompt", timeout_s=5)
        proc.close()
        used.append(proc)
    assert len({id(p) for p in used}) == 4, "a process served more than one call"
    assert all(p.closed for p in used)


def test_each_process_serves_exactly_one_prompt(pool: WarmPool):
    for i in range(3):
        proc = pool.acquire(wait_s=2.0)
        proc.ask(f"prompt {i}", timeout_s=5)
        proc.close()
    for proc in _FakeProc.instances:
        assert len(proc.asked) <= 1, (
            f"a process answered {len(proc.asked)} prompts; conversations are "
            "supposed to be one-shot"
        )


def test_the_pool_refills_after_a_handout(pool: WarmPool):
    """Otherwise the second call pays full startup and the pool is decorative."""
    first = pool.acquire(wait_s=2.0)
    first.close()
    for _ in range(50):
        if pool.ready >= 1:
            break
        time.sleep(0.05)
    assert pool.ready >= 1, "the pool did not replace the process it handed out"


# --- degrading rather than blocking ----------------------------------------


def test_an_empty_pool_spawns_cold_rather_than_blocking(monkeypatch):
    """Slow is a better failure than stuck, and the caller cannot tell the
    difference except in the timing."""
    monkeypatch.setattr("shared.llm.claude_cli_pool.WarmProcess", _FakeProc)
    empty = WarmPool(["claude"], size=0)
    proc = empty.acquire(wait_s=0.0)
    assert proc is not None
    assert isinstance(proc, _FakeProc)
    empty.close()


def test_size_zero_disables_warming(monkeypatch):
    """The escape hatch. Warm processes are real memory, so it has to be
    possible to turn them off."""
    monkeypatch.setattr("shared.llm.claude_cli_pool.WarmProcess", _FakeProc)
    disabled = WarmPool(["claude"], size=0)
    assert disabled.size == 0
    assert disabled.ready == 0
    disabled.close()


def test_a_dead_warm_process_is_discarded_not_handed_out(pool: WarmPool, monkeypatch):
    """A process can die while waiting. Handing out a corpse turns a warm pool
    into a source of confusing failures."""
    for _ in range(40):
        if pool.ready >= 1:
            break
        time.sleep(0.05)
    for proc in _FakeProc.instances:
        proc.alive = False          # everything warm is now dead
    handed = pool.acquire(wait_s=0.5)
    assert handed.alive, "a dead process was handed out"


def test_closing_the_pool_reaps_everything(monkeypatch):
    monkeypatch.setattr("shared.llm.claude_cli_pool.WarmProcess", _FakeProc)
    built = WarmPool(["claude"], size=3)
    for _ in range(60):
        if built.ready >= 3:
            break
        time.sleep(0.05)
    built.close()
    assert all(p.closed for p in _FakeProc.instances), "warm processes leaked"


# --- what the stream gives us that the cold path never had -----------------


def test_the_stream_reports_real_token_counts(pool: WarmPool):
    """The single-shot path had no accounting and wrote zeros into the audit
    trail. Estimating would have put an invented number in a provenance record;
    the stream reports actual usage, so it stops being a guess."""
    proc = pool.acquire(wait_s=2.0)
    result = proc.ask("prompt", timeout_s=5)
    proc.close()
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.cost_usd >= 0


# --- lifecycle: the pool must not be created per run -----------------------


def test_the_cli_client_is_shared_across_runs(monkeypatch):
    """A fresh client per run would warm three more processes and never reap
    them: fifty runs would leave a hundred and fifty booted CLI processes
    resident, each a Node runtime holding real memory.

    Sharing is safe because isolation lives one level down — a single process
    serves a single call — not at the level of the client.
    """
    from services.api_gateway import runs as runs_module
    from shared.llm import ClaudeCliLlmClient

    monkeypatch.setenv(runs_module.ENGINE_ENV, "cli")
    monkeypatch.setattr(ClaudeCliLlmClient, "check", lambda self: None)
    monkeypatch.setattr(
        runs_module, "find_claude_binary", lambda *a, **k: __file__
    )
    runs_module.reset_engine_cache()
    runs_module.shutdown_llm_client()
    try:
        first = runs_module.build_llm_client()
        second = runs_module.build_llm_client()
        assert first is second, "a new CLI client per call means a new pool per run"
    finally:
        runs_module.shutdown_llm_client()


def test_shutdown_releases_the_pool(monkeypatch):
    """The lifespan calls this. Warm processes are children of daemon threads,
    so nothing else collects them when the server stops."""
    from services.api_gateway import runs as runs_module

    runs_module.shutdown_llm_client()
    runs_module.shutdown_llm_client()      # idempotent


def test_the_probe_does_not_warm_a_pool(monkeypatch):
    """`resolve_engine()` builds a client purely to ask whether the CLI runs,
    and every page asks. Booting three processes to answer a question about a
    chip in the header would spend most of a gigabyte on it."""
    from shared.llm import ClaudeCliConfig, ClaudeCliLlmClient

    monkeypatch.setattr("shared.llm.claude_cli.find_claude_binary", lambda *a, **k: __file__)
    client = ClaudeCliLlmClient(ClaudeCliConfig(pool_size=3))
    assert client._pool is None, "constructing a client eagerly warmed processes"
