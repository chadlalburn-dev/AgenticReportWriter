"""ClaudeCliLlmClient — generate with the Claude Code CLI already on this machine.

Why this exists
---------------
The engine's `LlmClient` protocol had two implementations: `StubLlmClient`
(canned, offline, used by every test) and `VertexLlmClient` (needs a GCP
project, ADC and an approved Vertex endpoint — none of which are provisioned
yet). So every draft this app produced was placeholder text.

The Claude Code CLI is installed and authenticated on the analyst's own
machine. Shelling out to it in headless mode (`claude -p`) turns that existing
entitlement into the app's generation engine, with no cloud provisioning, no
API key and no new network path. Swap the client and the identical pipeline —
retrieval, citation enforcement, the safety gate, the audit chain — produces
real prose instead of stubs.

Data-governance boundary (read before pointing this at real data)
-----------------------------------------------------------------
This routes prompt content to Anthropic through the CLI's own session, NOT
through GSK's sanctioned Onyx LLM path (WI VQD-WI-063019). For the synthetic
XYZ-001 corpus that is fine — the data is fictional. Sending real GSK
preclinical data this way is a governance decision that belongs to a human,
so `allow_real_data=False` is the default and the client refuses to run when
the caller flags the corpus as real. Nothing here silently widens scope.

Implementation notes that cost real debugging
---------------------------------------------
* **`max_tokens` on the request is not honoured on this path.** The CLI has no
  `--max-tokens` flag — `--max-budget-usd` caps spend, not output length, and
  the two are not interchangeable. So the limit the callers declare
  (`critic.py` asks for 1024, the filler and planner for 4096) is silently
  dropped here, and a probe confirmed it: a critique-shaped request came back
  at 1,366 output tokens, comfortably past the 1,024 it had asked for. Nothing
  is broken by this — the pipeline validates what it gets rather than trusting
  a length — but it is worth knowing before treating `max_tokens` as a control
  that works everywhere. `VertexLlmClient` does honour it.
* **An unauthenticated CLI exits 0.** `claude -p` prints
  "Not logged in · Please run /login" and returns status 0, so exit code alone
  reports success on a total failure. The output is inspected instead.
* **The prompt goes on stdin, not in argv.** A report prompt carries the
  section instruction plus every retrieved chunk and table — tens of thousands
  of characters — and the npm shim is a `.cmd`, so the call routes through
  cmd.exe, which caps a command line at 8191 characters. Past that the call
  either dies with "The command line is too long" or, worse, arrives truncated:
  the first real run failed with the CLI answering "your message may have been
  cut off — I only received the template title", because that is all that fit.
  Measured: 12,859 characters fails as an argument and succeeds on stdin with a
  marker at the very end. Writing the prompt and closing the pipe also settles
  why stdin used to be DEVNULL — the CLI waits ~3s for piped input and warns
  into the captured output if the pipe is left open.
* Tools are disabled (`--disallowed-tools`) and permission mode is `dontAsk`:
  this is a text-generation call, and a subprocess that could edit files or
  run commands on the analyst's machine is not something a report draft needs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from shared.llm.claude_cli_pool import CliResult, WarmPool
from shared.llm.client import (
    LlmClient,
    LlmRequest,
    LlmResponse,
    LlmUsage,
    ModelTier,
    StructuredOutputError,
)

#: Where the CLI lives when it is not on PATH, most specific first.
#:
#: Two real layouts, and the npm one was missing. `npm install -g` writes its
#: shims to the npm global bin, which on this machine is NOT on the user PATH —
#: that PATH holds only Python and WindowsApps — so `shutil.which("claude")`
#: finds nothing even with the CLI correctly installed. The shims are a `.cmd`,
#: a `.ps1` and an extensionless script; subprocess drives the `.cmd` directly
#: (verified), because CreateProcess routes .cmd through the command interpreter.
_WINDOWS_GLOBS = (
    # `npm install -g @anthropic-ai/claude-code`
    "AppData/Roaming/npm/claude.cmd",
    # bundled by the desktop app, versioned directory
    "AppData/Roaming/Claude/claude-code/*/claude.exe",
)

#: Substrings that mean "the CLI ran but produced nothing usable". Matched
#: case-insensitively against stdout, because the CLI exits 0 for these.
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "credit balance is too low",
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ClaudeCliUnavailable(RuntimeError):
    """The CLI is missing, unauthenticated, or refused to run.

    Carries an actionable message: the caller surfaces it to the user, who is
    the only one who can log a CLI in.
    """


@dataclass(frozen=True)
class ClaudeCliConfig:
    binary: str | None = None
    timeout_s: float = 240.0
    #: Ceiling on the readiness probe. Separate from `timeout_s` because they
    #: guard different things: a section can legitimately take minutes, while a
    #: one-word prompt that has not answered in this long is not going to.
    #: Measured at ~27s here for the not-signed-in path, which is the slow one.
    check_timeout_s: float = 60.0
    #: Per-tier model override. Left empty, the CLI's own default is used —
    #: which is the right default, since the analyst's config already picked it.
    models: dict[ModelTier, str] = field(default_factory=dict)
    #: Refuse to run when the caller says the corpus holds real data. See the
    #: governance note in the module docstring.
    allow_real_data: bool = False
    cwd: str | None = None
    #: How many CLI processes to keep booted. Startup is ~45s and is pure
    #: overhead, so warming hides it; 0 disables the pool and every call pays
    #: the cold cost. Each warm process is real RSS and makes no API call until
    #: used, so the trade is memory, not money. See claude_cli_pool.
    pool_size: int = 3
    #: Deadline for a warm process before falling back to a cold spawn. Slow is
    #: a better failure than stuck.
    pool_wait_s: float = 8.0


def find_claude_binary(explicit: str | None = None) -> str | None:
    """Locate the CLI: explicit path, then env, then PATH, then install dir."""
    for candidate in (explicit, os.environ.get("REPORTGEN_CLAUDE_BIN")):
        if candidate and Path(candidate).exists():
            return candidate
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    for pattern in _WINDOWS_GLOBS:
        # reverse=True so a versioned directory yields the newest install
        matches = sorted(Path.home().glob(pattern), reverse=True)
        if matches:
            return str(matches[0])
    return None


def _strip_fence(text: str) -> str:
    """Return the JSON body, whether or not the model fenced it.

    Asking for "only JSON" gets JSON most of the time and a fenced block the
    rest, so both are accepted rather than failing a good generation on
    formatting.
    """
    fenced = _FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def _reask_for_valid_json(
    original: str, broken: str, exc: json.JSONDecodeError
) -> str:
    """A self-contained second attempt.

    Self-contained is not a style choice. A warm process serves exactly one
    call and is then discarded, precisely so that one section's retrieved
    chunks can never leak into another's context — so the correction lands on a
    brand-new process that has never seen the original prompt or its own broken
    reply. Everything it needs to fix the mistake has to travel in this string.

    The parser's own `msg` and `pos` go in verbatim rather than being
    paraphrased. "Expecting ',' delimiter at position 1307" tells the model
    exactly which byte to look at; "your JSON was invalid" invites it to
    rewrite the content instead of the punctuation, and the content was fine.
    """
    window = broken[max(0, exc.pos - 90) : exc.pos + 90]
    return (
        f"{original}\n\n"
        "--- CORRECTION REQUIRED ---\n"
        "A previous attempt at this exact task returned JSON that could not "
        f"be parsed: {exc.msg}, at character {exc.pos} of {len(broken)}.\n\n"
        f"The text around that position was:\n{window!r}\n\n"
        "Redo the task and return the corrected JSON object. Keep the same "
        "findings and the same wording; the analysis was not the problem. Fix "
        "only the JSON syntax. The most common cause is an unescaped double "
        "quote or a literal newline inside a string value: when quoting a "
        "phrase from the draft, escape the quotation marks or use single "
        "quotes instead. Return the JSON object and nothing else - no prose "
        "before it, no code fence around it."
    )


class ClaudeCliLlmClient(LlmClient):
    def __init__(self, config: ClaudeCliConfig | None = None) -> None:
        self._config = config or ClaudeCliConfig()
        binary = find_claude_binary(self._config.binary)
        if not binary:
            raise ClaudeCliUnavailable(
                "The Claude Code CLI was not found. Install it, or set "
                "REPORTGEN_CLAUDE_BIN to its full path."
            )
        self._binary = binary
        self._pool: WarmPool | None = None
        self._pool_lock = threading.Lock()

    @property
    def binary(self) -> str:
        return self._binary

    def _get_pool(self) -> WarmPool | None:
        """Built on first use, not in __init__.

        `resolve_engine()` constructs a client just to ask whether the CLI can
        run, and on many pages. Booting three processes for that would spend
        ~1GB answering a question about a chip in the header.
        """
        if self._config.pool_size <= 0:
            return None
        with self._pool_lock:
            if self._pool is None:
                self._pool = WarmPool(
                    [self._binary],
                    size=self._config.pool_size,
                    cwd=self._config.cwd,
                )
            return self._pool

    def close(self) -> None:
        """Release warm processes. Safe to call more than once."""
        with self._pool_lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    # -- preflight ----------------------------------------------------------

    def check(self) -> None:
        """Raise `ClaudeCliUnavailable` unless a real generation would work.

        Called before a run so the failure lands on the setup screen with an
        instruction, instead of 20 sections in with a stack trace.
        """
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, no shell
                [self._binary, "-p", "Reply with the single word: ready"],
                capture_output=True,
                text=True,
                encoding="utf-8",   # UTF-8, not cp1252 — see the note on the generate path
                timeout=self._config.check_timeout_s,
                stdin=subprocess.DEVNULL,
                cwd=self._config.cwd,
            )
        except FileNotFoundError as exc:
            raise ClaudeCliUnavailable(f"{self._binary} could not be executed.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCliUnavailable(
                "The Claude CLI did not answer a one-word prompt within "
                f"{self._config.check_timeout_s:.0f}s."
            ) from exc

        combined = f"{proc.stdout}\n{proc.stderr}"
        self._raise_if_unusable(combined, proc.returncode)

    @staticmethod
    def _raise_if_unusable(output: str, returncode: int) -> None:
        low = output.lower()
        for marker in _AUTH_MARKERS:
            if marker in low:
                raise ClaudeCliUnavailable(
                    "The Claude Code CLI is installed but not signed in, so it "
                    "cannot generate. Open a terminal, run `claude`, then "
                    "`/login`, and start the run again. "
                    f"(The CLI said: {output.strip().splitlines()[-1][:120]!r})"
                )
        if returncode != 0:
            raise ClaudeCliUnavailable(
                f"The Claude CLI exited {returncode}: {output.strip()[:300]}"
            )

    # -- LlmClient ----------------------------------------------------------

    def generate(self, request: LlmRequest) -> LlmResponse:
        if not self._config.allow_real_data and _looks_like_real_data(request):
            raise ClaudeCliUnavailable(
                "This run is flagged as holding real GSK data. The local CLI "
                "routes prompts outside the sanctioned Onyx LLM path, so it is "
                "refused. Use synthetic data, or run through the approved "
                "Vertex endpoint."
            )

        prompt = self._compose_prompt(request)
        raw, usage, parsed = self._ask_for_json(prompt, request)

        return LlmResponse(
            text=raw,
            parsed_json=parsed,
            model_version=self._config.models.get(request.tier) or "claude-code-cli",
            # The real value when the stream reports one. Hardcoding "end_turn"
            # told the pipeline every generation completed normally, including
            # the ones cut off at a limit — so a truncated section was
            # indistinguishable from a finished one.
            stop_reason=(usage.stop_reason if usage and usage.stop_reason else "end_turn"),
            usage=LlmUsage(
                # Real numbers when the stream reports them. The single-shot
                # path has no token accounting at all, and writing an estimate
                # into an audit trail would be inventing a figure, so it stays
                # at zero and the record carries the model_version instead.
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
            ),
        )

    def _ask(self, prompt: str, request: LlmRequest) -> tuple[str, CliResult | None]:
        """One exchange, pooled if a pool exists and cold otherwise."""
        pool = self._get_pool()
        if pool is None:
            return self._generate_cold(prompt, request), None
        return self._generate_pooled(pool, prompt, request)

    def _ask_for_json(
        self, prompt: str, request: LlmRequest
    ) -> tuple[str, CliResult | None, dict[str, object] | None]:
        """Ask, and if a schema was requested, insist on parseable JSON —
        allowing the model exactly one correction.

        Why a re-ask and not a repair
        -----------------------------
        The first live failure of this kind was a 1,308-character critique that
        parsed cleanly for 1,307 of those characters and then hit `Expecting ','
        delimiter`. The model had written good JSON and fumbled one escape,
        almost certainly a quotation mark inside a phrase it was quoting back
        from the draft — a critique's whole job is to quote the text it objects
        to. Losing an entire five-section report to one stray byte is a poor
        trade.

        The tempting fix is to patch the string: balance the quote, strip the
        control character, close the brace. That is forbidden here. Repairing
        JSON means guessing what the model meant to say and then presenting the
        guess as the model's own output — inside an application whose only
        claim is that every value traces to a real source. A repaired critique
        is invented content wearing a provenance badge.

        Re-asking has none of that problem. The model is shown its own broken
        output and the parser's exact complaint, and writes fresh JSON; whatever
        comes back is genuinely its own. The cost is one extra call on a rare
        path, which is why it is capped at one and never becomes a loop that
        hides a systematically wrong schema.
        """
        attempt_prompt = prompt
        first_failure: tuple[str, json.JSONDecodeError] | None = None

        while True:
            raw, usage = self._ask(attempt_prompt, request)
            if not raw:
                raise StructuredOutputError("The Claude CLI returned no output.")
            if not request.response_schema_name:
                return raw, usage, None

            try:
                loaded = json.loads(_strip_fence(raw))
            except json.JSONDecodeError as exc:
                if first_failure is None:
                    first_failure = (raw, exc)
                    attempt_prompt = _reask_for_valid_json(prompt, raw, exc)
                    continue
                raise StructuredOutputError(
                    self._json_failure_report(request, raw, exc, usage, first_failure)
                ) from exc

            if not isinstance(loaded, dict):
                raise StructuredOutputError(
                    f"Expected a JSON object for {request.response_schema_name!r}, "
                    f"got {type(loaded).__name__}."
                )
            return raw, usage, loaded

    @staticmethod
    def _json_failure_report(
        request: LlmRequest,
        raw: str,
        exc: json.JSONDecodeError,
        usage: CliResult | None,
        first: tuple[str, json.JSONDecodeError],
    ) -> str:
        """What went wrong, both times, in enough detail to act on.

        Three things, each earned. The **length and offset**, because "could not
        parse" alone cannot distinguish a reply that was cut off from one that
        was complete and malformed, and those need opposite fixes. The **window
        around the failing character**, because that is where the defect
        literally is — the first live failure of this kind broke at character
        1,307 of 1,308 and the record kept only the first 200, which made it
        undiagnosable. And the **head**, because it is the one thing the window
        cannot tell you: whether the model returned JSON at all or opened with
        prose.

        Both attempts are reported. Whether the correction changed anything is
        the first question a reader has: an identical second failure points at
        the prompt or the schema, a different one points at the model.
        """
        first_raw, first_exc = first
        stop = usage.stop_reason if usage else ""

        def at(text: str, err: json.JSONDecodeError) -> str:
            return repr(text[max(0, err.pos - 100) : err.pos + 100])

        return (
            f"Expected JSON for {request.response_schema_name!r} and could not "
            f"parse it, on the original reply or on the correction. "
            f"First: {len(first_raw)} chars, {first_exc.msg} at position "
            f"{first_exc.pos}, around it: {at(first_raw, first_exc)}. "
            f"Retry: {len(raw)} chars, {exc.msg} at position {exc.pos}"
            f"{f', stop_reason={stop!r}' if stop else ''}"
            f", around it: {at(raw, exc)}. "
            f"Retry began: {raw[:160]!r}"
        )


    def _generate_pooled(
        self, pool: WarmPool, prompt: str, request: LlmRequest
    ) -> tuple[str, CliResult]:
        """One warm process, one call, then discarded.

        Discarded rather than reused, and that is the whole design: the CLI
        treats a process as a single conversation and ignores a `session_id` on
        the input message, so a reused process would carry the previous
        section's retrieved chunks in its history. In a report whose claim is
        that every value traces to a source it was given, that is a provenance
        hole, not a saving.
        """
        proc = pool.acquire(wait_s=self._config.pool_wait_s)
        try:
            result = proc.ask(prompt, timeout_s=self._config.timeout_s)
        except TimeoutError as exc:
            raise ClaudeCliUnavailable(
                f"The Claude CLI did not finish within {self._config.timeout_s:.0f}s."
            ) from exc
        except RuntimeError as exc:
            raise ClaudeCliUnavailable(str(exc)) from exc
        finally:
            proc.close()

        self._raise_if_unusable(result.text, 1 if result.is_error else 0)
        return result.text.strip(), result

    def _generate_cold(self, prompt: str, request: LlmRequest) -> str:
        """One process per call, no warming. The fallback when the pool is
        disabled, and the path every test exercises."""
        argv = [
            self._binary,
            "-p",
            # This is a text-generation call. A subprocess that can edit files
            # or run commands is not something drafting a report needs.
            "--disallowed-tools",
            "Bash,Edit,Write,Read,WebFetch,WebSearch,Glob,Grep",
            "--permission-mode",
            "dontAsk",
        ]
        model = self._config.models.get(request.tier)
        if model:
            argv += ["--model", model]
        if request.system:
            argv += ["--system-prompt", request.system]

        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, no shell
                argv,
                # `input` writes the prompt and closes the pipe. The prompt must
                # not go in argv: the npm shim is a .cmd, so the call routes
                # through cmd.exe, which caps a command line at 8191 characters
                # and truncates a report prompt into a fragment the model then
                # politely answers.
                input=prompt,
                capture_output=True,
                text=True,
                # UTF-8 explicitly, never the locale default. `text=True` alone
                # decodes with `locale.getpreferredencoding()`, which is cp1252 on
                # this machine, and the CLI emits UTF-8 — so "12 µM" arrived as
                # "12 µM" and, after a second round trip, "12 ÂµM". A live
                # critique caught it in section 1 of run 5726cd3b860f, which is
                # lucky: a preclinical summary is made of µg/mL, °C and ±, and
                # silently corrupting all of them while every citation still
                # resolves is the worst kind of defect this app can have.
                #
                # Strict, not errors="replace". A replacement character is a silent
                # substitution in a document whose only promise is that its values
                # match the source; a decode failure is loud and diagnosable. The
                # CLI's output is JSON from a Node process, so strict is safe.
                encoding="utf-8",
                timeout=self._config.timeout_s,
                cwd=self._config.cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCliUnavailable(
                f"The Claude CLI did not finish within {self._config.timeout_s:.0f}s."
            ) from exc

        raw = (proc.stdout or "").strip()
        self._raise_if_unusable(f"{raw}\n{proc.stderr}", proc.returncode)
        return raw

    def _compose_prompt(self, request: LlmRequest) -> str:
        """Flatten the request into one prompt.

        The schema goes in as an explicit instruction because the CLI has no
        tool-use/structured-output flag. The engine still validates what comes
        back, so a malformed answer fails the section rather than corrupting
        the draft.
        """
        parts: list[str] = []
        for message in request.messages:
            parts.append(message.content)
        if request.response_schema_name and request.response_schema_json:
            parts.append(
                "\n---\n"
                "Reply with ONE JSON object and nothing else. No prose before or "
                "after it, no code fence, no explanation. It must validate "
                f"against this JSON Schema for {request.response_schema_name}:\n"
                + json.dumps(request.response_schema_json, separators=(",", ":"))
                + "\n\nUse ONLY citation_id values that appear in the source "
                "material above. Never invent one: a citation that does not "
                "resolve is worse than no citation."
            )
        return "\n\n".join(parts)


def _looks_like_real_data(request: LlmRequest) -> bool:
    """Best-effort guard, not a classifier.

    Honest about its limits: it catches an explicit marker the caller sets, and
    nothing else. It is a tripwire against pointing the local CLI at production
    data by accident, not a data-loss-prevention control.
    """
    haystack = " ".join(m.content for m in request.messages).lower()
    return "data-classification: real" in haystack
