"""A pool of pre-warmed Claude CLI processes.

Why this exists
---------------
The CLI costs about 45 seconds to start, and that cost is entirely startup: a
one-word prompt measured 47.7s and an 8,000-character prompt 42.1s. It is a
330MB single-file binary unpacking and booting a Node runtime. The orchestrator
spends one process per model call and a five-section report makes roughly
fifteen, so a report could not finish inside fourteen minutes.

The obvious fix is to keep one process alive and feed it every prompt over
`--input-format stream-json`. That works and it is fast — measured 38.1s for the
first call then 4.9s and 11.7s for the next two. **It is also unsafe for this
application**, and the measurement that settles it is in the tests: a process
asked to remember a codeword still recalls it on a later message, *including
when the input message carries a different `session_id`* — the CLI ignores that
field and the returned session id never changes. One process is one
conversation.

For a report generator whose entire claim is that every value traces to a
retrieved source, that is a provenance hole rather than an optimisation. Section
five's prompt would carry section one's chunks in the conversation history, and
the model could cite data that section was never given.

So: processes are pre-warmed, each serves exactly ONE call, and is then closed.
Startup happens before the call instead of during it, and every call is a virgin
conversation. Measured 6.6s and 6.2s against warm processes, with a second
process correctly answering "NONE" to a codeword only the first was told.

The trade
---------
Memory, and only memory. A warm process makes no API call until it is given a
prompt, so idle warmth is free in cost terms. It is not free in RSS, which is
why the pool is small and why `size=0` disables it entirely and falls back to
cold spawns.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass

#: Flags every invocation needs.
#:
#: `--verbose` is not optional: the CLI refuses `--output-format stream-json`
#: under `--print` without it ("requires --verbose"), which cost one probe to
#: discover. Tools are disabled because drafting a report never needs to edit a
#: file or run a command, and this subprocess runs on the analyst's machine.
_STREAM_FLAGS = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-mode",
    "dontAsk",
    "--disallowed-tools",
    "Bash,Edit,Write,Read,WebFetch,WebSearch,Glob,Grep",
)


@dataclass
class CliResult:
    """One completed exchange, as the stream reported it."""

    text: str
    #: From the terminating `result` event. The single-shot path had no token
    #: accounting at all and wrote zeros into the audit trail; the stream gives
    #: real numbers, so the audit record stops being a guess.
    input_tokens: int
    output_tokens: int
    cost_usd: float
    session_id: str
    is_error: bool
    raw_events: int
    #: Why generation stopped, as the stream reported it. Not defaulted and not
    #: assumed: the client used to hardcode "end_turn", so a reply cut off at a
    #: token limit was announced to the pipeline as a normal completion and a
    #: truncated draft looked like a finished one.
    stop_reason: str


class WarmProcess:
    """A booted CLI waiting for its one prompt."""

    def __init__(self, argv: list[str], cwd: str | None) -> None:
        self.started_at = time.monotonic()
        self._proc = subprocess.Popen(  # noqa: S603 - fixed binary, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
        )

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def ask(self, prompt: str, timeout_s: float) -> CliResult:
        """Send the one prompt this process will ever receive, and read to the
        terminating `result` event.

        A deadline is enforced per read rather than for the whole exchange,
        because a stalled stream and a slow-but-progressing one look identical
        from the outside and only one of them should be killed.
        """
        assert self._proc.stdin and self._proc.stdout
        message = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

        deadline = time.monotonic() + timeout_s
        events = 0
        while True:
            if time.monotonic() > deadline:
                self.close()
                raise TimeoutError(f"no result event within {timeout_s:.0f}s")
            line = self._proc.stdout.readline()
            if not line:
                stderr = (self._proc.stderr.read() if self._proc.stderr else "") or ""
                self.close()
                raise RuntimeError(
                    f"the CLI closed its stream after {events} events: "
                    f"{stderr.strip()[:300]}"
                )
            events += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON noise on stdout is not fatal
            if event.get("type") != "result":
                continue
            usage = event.get("usage") or {}
            return CliResult(
                text=str(event.get("result") or ""),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cost_usd=float(event.get("total_cost_usd") or 0.0),
                session_id=str(event.get("session_id") or ""),
                is_error=bool(event.get("is_error")),
                raw_events=events,
                stop_reason=str(event.get("stop_reason") or ""),
            )

    def close(self) -> None:
        """Close stdin and reap. Killed rather than waited on if it lingers —
        a process that has already answered has nothing left worth waiting for."""
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass


class WarmPool:
    """Keeps `size` processes booted, hands them out one call at a time.

    Deliberately not a connection pool: nothing is returned to it. A process
    goes out, serves one prompt and dies, and a replacement is booted in the
    background. That is what keeps conversations isolated.
    """

    def __init__(
        self,
        argv_prefix: list[str],
        *,
        size: int = 3,
        cwd: str | None = None,
        warm_ttl_s: float = 900.0,
    ) -> None:
        self._argv = list(argv_prefix) + list(_STREAM_FLAGS)
        self._cwd = cwd
        self._size = max(0, size)
        #: A warm process is still a process. Past this age it is replaced
        #: rather than used, so a pool left idle overnight does not hand out
        #: something that has been sitting on a socket for hours.
        self._warm_ttl_s = warm_ttl_s
        self._ready: queue.Queue[WarmProcess] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._filling = 0
        for _ in range(self._size):
            self._start_one()

    # -- warming ------------------------------------------------------------

    def _start_one(self) -> None:
        with self._lock:
            if self._closed or self._filling + self._ready.qsize() >= self._size:
                return
            self._filling += 1

        def work() -> None:
            try:
                self._ready.put(WarmProcess(self._argv, self._cwd))
            except Exception:  # noqa: BLE001 - a failed warm is not fatal
                pass
            finally:
                with self._lock:
                    self._filling -= 1

        threading.Thread(target=work, name="cli-warm", daemon=True).start()

    # -- use ----------------------------------------------------------------

    def acquire(self, *, wait_s: float = 0.0) -> WarmProcess:
        """A process to serve one call.

        Falls back to a cold spawn rather than blocking indefinitely: slow is a
        better failure than stuck, and the caller cannot tell the difference
        except in the timing.
        """
        deadline = time.monotonic() + wait_s
        while True:
            try:
                candidate = self._ready.get_nowait()
            except queue.Empty:
                self._start_one()
                if time.monotonic() >= deadline:
                    return WarmProcess(self._argv, self._cwd)  # cold
                time.sleep(0.2)
                continue
            self._start_one()  # replace what we just took
            stale = time.monotonic() - candidate.started_at > self._warm_ttl_s
            if candidate.alive and not stale:
                return candidate
            candidate.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                self._ready.get_nowait().close()
            except queue.Empty:
                return

    # -- introspection, for tests and the engine chip -----------------------

    @property
    def ready(self) -> int:
        return self._ready.qsize()

    @property
    def size(self) -> int:
        return self._size
