#!/usr/bin/env python3
"""
One place to shell out to `claude -p`, shared by score.py and draft.py.

It exists because of a specific failure that cost 25 minutes of wall clock. When the Midway
session expires, `claude` does not fail cleanly: some calls exit 1 with a warning on stderr,
and others hang until the caller's timeout. Both scripts used to treat every failure as
per-item bad luck, so a single expired cookie turned into one full timeout per batch and per
draft — a 420s score stage and an 1086s draft stage that produced nothing.

So two things here:

  1. `AuthExpired` is raised for the signatures that mean "you are not logged in". Callers
     abort the whole stage on it instead of grinding through the remaining items, because no
     later item can possibly succeed.
  2. `preflight()` makes one cheap call before a stage does any work, so an expired session
     costs seconds instead of the sum of every timeout.

`gather()` runs the calls concurrently. These are independent subprocesses waiting on a
network round trip, so they parallelise cleanly; the serial loops were most of the runtime
even on a healthy session. Nothing here touches SQLite — callers collect the results and
write them from the main thread, because a sqlite3 connection is not thread-safe.

    python3 claudecall.py          # preflight check, prints ok or what to run
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TIMEOUT = 180
DEFAULT_WORKERS = 4

# Matched against stderr, lowercased, and only when the exit status is non-zero. `claude`
# prints "credential check failed" as a warning on some successful calls too, so treating it
# as fatal regardless of exit status would abort a healthy run.
AUTH_SIGNS = (
    "midway session expired",
    "credential check failed",
    "not authenticated",
    "please run mwinit",
    "no valid credentials",
)

REMEDY = "Midway session looks expired. Run `mwinit` and try again."


class AuthExpired(RuntimeError):
    """Not logged in. Fatal for the whole stage, not just the item that hit it."""


def call(prompt: str, claude_bin: str = "claude", timeout: int = DEFAULT_TIMEOUT) -> str:
    proc = subprocess.run(
        [claude_bin, "-p", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if any(s in err.lower() for s in AUTH_SIGNS):
            raise AuthExpired(REMEDY)
        raise RuntimeError(f"claude exited {proc.returncode}: {err[:300]}")
    return proc.stdout


def preflight(claude_bin: str = "claude", timeout: int = 90) -> tuple[bool, str]:
    """One cheap call to prove the CLI is usable. (ok, message)."""
    try:
        out = call("Reply with the single word: ok", claude_bin, timeout)
    except AuthExpired as e:
        return False, str(e)
    except subprocess.TimeoutExpired:
        # A hang with no output is the other face of an expired session, so say so rather
        # than reporting a bare timeout and leaving the reader to guess.
        return False, (f"claude did not respond within {timeout}s and printed nothing. "
                       f"That is usually an expired session — try `mwinit`.")
    except FileNotFoundError:
        return False, (f"`{claude_bin}` is not on PATH. Put its absolute path in "
                       f"runtime.claude_bin in config/profile.yaml.")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return True, (out.strip()[:60] or "(empty reply)")


def gather(prompts: list[str], claude_bin: str = "claude",
           timeout: int = DEFAULT_TIMEOUT,
           workers: int = DEFAULT_WORKERS) -> list[tuple[str | None, Exception | None]]:
    """
    Run prompts concurrently, preserving order. Returns one (text, error) pair per prompt;
    exactly one side of each pair is set, so a caller can report per-item failures without
    losing the successes alongside them.

    An AuthExpired anywhere propagates: if the session is gone, the remaining prompts are
    guaranteed to fail too and waiting for them only burns the timeout again.
    """
    if not prompts:
        return []
    n = max(1, min(workers, len(prompts)))
    out: list[tuple[str | None, Exception | None]] = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(call, p, claude_bin, timeout) for p in prompts]
        for f in futs:
            try:
                out.append((f.result(), None))
            except AuthExpired:
                for other in futs:
                    other.cancel()
                raise
            except Exception as e:  # noqa: BLE001
                out.append((None, e))
    return out


class Unusable(RuntimeError):
    """preflight said the CLI cannot be used. Fatal for the stage, before any work is spent."""


def run(prompts: list[str], claude_bin: str = "claude", timeout: int = DEFAULT_TIMEOUT,
        workers: int = DEFAULT_WORKERS, announce: str = "",
        ) -> list[tuple[str | None, Exception | None]]:
    """
    preflight, then gather. The three stages — score, draft, tailor — each wired up this same
    pair by hand, with the same two early returns, so a change to the abort policy had to be
    made in three places or not at all.

    Raises Unusable when the CLI is not usable, and lets AuthExpired propagate out of gather.
    Both mean "abandon the stage". Per-item errors still come back inside the result list,
    because one timed-out batch must not cost the ones that succeeded.

    `announce` is printed only once preflight has passed. It is a parameter rather than a line
    in the caller because the order matters on the failing path: printing "calling claude for
    6 draft(s)" and then "claude is not usable" would claim work that never started. Each
    stage words it differently (batch(es), draft(s), resume(s)) and two of them want a leading
    blank line, so the text stays with the caller and only the timing moves here.
    """
    ok, msg = preflight(claude_bin)
    if not ok:
        raise Unusable(msg)
    if announce:
        print(announce, flush=True)
    return gather(prompts, claude_bin, timeout, workers)


if __name__ == "__main__":
    ok, msg = preflight()
    print(f"claude: {'ok' if ok else 'NOT USABLE'} — {msg}")
    sys.exit(0 if ok else 1)
