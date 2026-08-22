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
from dataclasses import dataclass, field
from pathlib import Path

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

    @property
    def binary(self) -> str:
        return self._binary

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
        # The prompt goes on STDIN, not in argv. A report prompt carries the
        # section instruction plus every retrieved chunk and table, so it runs
        # to tens of thousands of characters — and the npm shim is a .cmd, which
        # routes through cmd.exe and caps a command line at 8191 characters.
        # Over that the call either dies with "The command line is too long" or,
        # worse, arrives truncated: the first real run failed with the CLI
        # replying "your message may have been cut off — I only received the
        # template title", because that is all that fit.
        #
        # Measured: a 12,859-character prompt fails as an argument and succeeds
        # on stdin with a marker placed at its very end.
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
                # `input` writes the prompt and closes the pipe, which also
                # settles the reason stdin used to be DEVNULL: the CLI waits
                # ~3s for piped input and warns into stdout if the pipe is left
                # open. Sending the prompt and closing satisfies both.
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._config.timeout_s,
                cwd=self._config.cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCliUnavailable(
                f"The Claude CLI did not finish within {self._config.timeout_s:.0f}s."
            ) from exc

        raw = (proc.stdout or "").strip()
        self._raise_if_unusable(f"{raw}\n{proc.stderr}", proc.returncode)
        if not raw:
            raise StructuredOutputError("The Claude CLI returned no output.")

        parsed: dict[str, object] | None = None
        if request.response_schema_name:
            body = _strip_fence(raw)
            try:
                loaded = json.loads(body)
            except json.JSONDecodeError as exc:
                raise StructuredOutputError(
                    f"Expected JSON for {request.response_schema_name!r} but the "
                    f"CLI returned prose: {raw[:200]!r}"
                ) from exc
            if not isinstance(loaded, dict):
                raise StructuredOutputError(
                    f"Expected a JSON object for {request.response_schema_name!r}, "
                    f"got {type(loaded).__name__}."
                )
            parsed = loaded

        return LlmResponse(
            text=raw,
            parsed_json=parsed,
            model_version=self._config.models.get(request.tier) or "claude-code-cli",
            stop_reason="end_turn",
            usage=LlmUsage(
                # The CLI's text output carries no token accounting. Estimating
                # would put invented numbers in the audit trail, so both stay 0
                # and the audit record shows the model_version instead.
                input_tokens=0,
                output_tokens=0,
            ),
        )

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
