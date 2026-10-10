#!/usr/bin/env python3
"""
Pick the engine that does the judgement work, and define the shape all engines share.

Three stages need judgement: score, draft, tailor. Until now each one shelled out to the
`claude` CLI directly through claudecall. That is fine while the CLI is available and useless
the day it is not, which is the day this module exists for: `runtime.engine: heuristic` runs
the whole pipeline with no subprocess, no network and no account.

    runtime:
      engine: heuristic        # the default, and what an unedited profile gets

The default matters more than it looks. The live config/profile.yaml has no `engine:` key at
all, so `resolve` has to treat "absent" as "heuristic" rather than erroring or falling back to
claude. An unedited profile on a machine with no `claude` binary runs end to end.

`resolve` is deliberately separate from `get` so a stage can validate and print the name before
importing anything. The import inside `get` is lazy, and that laziness is the mechanism behind
the zero-subprocess guarantee: with engine=heuristic, `engine_claude` is never imported, so
`claudecall` is never imported, so nothing in the process can reach `subprocess.run`.

    python3 engine.py          # print the engine the current profile selects
"""

from __future__ import annotations

import sys
import typing

VALID = ("heuristic", "claude", "ollama")
DEFAULT = "heuristic"


class BadEngine(ValueError):
    """An engine name that cannot be used. Fatal at startup, before any work is spent."""


class EngineUnusable(RuntimeError):
    """
    The engine cannot do the work. Two situations, one type, told apart by `mid_stage`.

    The distinction is not cosmetic: the three stages print different lines for them today and
    must keep doing so. `mid_stage=False` is "the preflight said no, before any work" and
    prints `claude is not usable: ...`. `mid_stage=True` is "the session died part way
    through, after some rows may already have been written" and prints `aborted: ...`.
    """

    def __init__(self, msg: str, *, mid_stage: bool = False):
        super().__init__(msg)
        self.mid_stage = mid_stage


class ConfigError(ValueError):
    """
    A `scoring:` block an engine cannot use. Fatal at startup, named down to the key.

    It lives here rather than in engine_heuristic so a stage can catch it without importing
    the engine module -- the same lazy-import rule that keeps claudecall off the heuristic path,
    applied symmetrically.
    """


class UnparseableReply(RuntimeError):
    """
    An engine produced output the stage could not read. Its own type because score.py prints a
    different line for it (`unparseable reply: ...`) than for a call that failed outright, and
    that difference survives the move of batching into the engine only if the batch callback
    can tell the two apart.
    """


@typing.runtime_checkable
class EngineProtocol(typing.Protocol):
    """
    What the three stages may call. Documented as a Protocol rather than an ABC: there are two
    implementations in one repo, duck typing is enough, and a metaclass would buy nothing.

    Every method is list-shaped, including the two that look per-row. `draft.py` and
    `tailor.py` do not loop over calls today -- they build every prompt and make ONE call into
    `claudecall.run`, which preflights once and then runs the prompts concurrently. A per-row
    interface would force them serial again and reinstate the 1086-second draft stage
    claudecall was written to kill, so the interface is the shape claudecall.run already is.
    """

    name: str

    def preflight(self, profile: dict | None = None) -> tuple[bool, str]: ...

    def plan(self, rows: list, *, batch_size: int) -> list[list]: ...

    def dry_run_preview(self, profile: dict, unit: list, *,
                        master: str | None = None) -> str: ...

    def score_reqs(self, profile: dict, rows: list, *, master: str | None = None,
                   batch_size: int = 12, announce: str = "",
                   on_batch=None) -> list[dict]: ...

    def draft_referrals(self, profile: dict, rows: list, kind: str, *,
                        announce: str = "") -> list: ...

    def tailor_resumes(self, master: str, profile: dict, rows: list, *,
                       announce: str = "") -> list: ...


def resolve(rt: dict | None, override: str | None = None) -> str:
    """
    The engine name, from the --engine override or runtime.engine, defaulting to heuristic.

    The `or DEFAULT` sits after `.strip().lower()` rather than inside the first chain, so
    unset, "", "   " and a missing runtime: block all mean heuristic and only a non-empty
    unrecognised name raises. Under the shorter `(override or rt.get("engine") or DEFAULT)`
    an empty value silently defaulted while a whitespace-only one raised, which is backwards:
    both mean the same thing to someone editing YAML.
    """
    name = (override or (rt or {}).get("engine") or "").strip().lower() or DEFAULT
    if name not in VALID:
        raise BadEngine(
            f"unknown engine {name!r}. Valid values are: {', '.join(VALID)}. "
            f"Set runtime.engine in config/profile.yaml or pass --engine."
        )
    return name


def get(name: str):
    """
    The engine instance. Imports lazily, which is what keeps the heuristic path free of any
    path to claudecall -- see the module docstring.
    """
    if name == "claude":
        import engine_claude
        return engine_claude.ClaudeEngine()
    if name == "ollama":
        # Accepted as a config value so a profile carrying it fails with a sentence instead of
        # a KeyError, but there is no local-model backend on this branch. Deliberately not a
        # stub that silently degrades to heuristic: an owner who wrote `engine: ollama` wants
        # a local model, and quietly giving them something else is worse than saying no.
        raise BadEngine(
            "engine 'ollama' is not implemented on this branch. Use 'heuristic' (scripted, "
            "no network) or 'claude' (needs the CLI and a live session)."
        )
    import engine_heuristic
    return engine_heuristic.HeuristicEngine()


if __name__ == "__main__":
    import jobprofile

    if not jobprofile.PROFILE.exists():
        sys.exit(f"no profile at {jobprofile.PROFILE} — copy config/profile.example.yaml")
    rt = jobprofile.runtime(jobprofile.load())
    try:
        name = resolve(rt)
    except BadEngine as e:
        sys.exit(str(e))
    print(f"engine: {name}" + ("  (runtime.engine unset, using the default)"
                               if not (rt.get("engine") or "").strip() else ""))
