#!/usr/bin/env python3
"""
Today's behaviour, behind the engine interface. Nothing here is new work.

The prompts stay in the stage modules -- `score.build_prompt`, `score.RUBRIC`,
`draft.build_prompt`, `draft.VOICE`, `tailor.build_prompt`, `tailor.RULES` -- because
`score.py --dry-run` prints one and the existing tests reference them. This module owns only
the call-and-parse pair, so the diff on the three stages stays small.

The one property worth stating because it would be easy to lose: each method makes ONE call
into `claudecall.run`, which preflights once and then runs every prompt concurrently with
`max_parallel_claude` workers. A per-row engine interface would have made these serial again
and brought back the 1086-second draft stage claudecall.py exists to prevent, so the interface
is list-shaped to match what claudecall.run already is.

`claudecall.py` itself is untouched by this branch.
"""

from __future__ import annotations

import claudecall
import draft
import engine
import jobprofile
import score
import tailor


class ClaudeEngine:
    """
    Reads `claude_bin` and `max_parallel_claude` from the profile it is handed, rather than
    taking them as parameters. The rule: a value with a CLI override travels as a parameter
    (`batch_size`, from --batch), a value that only lives in config is read from the profile.
    """

    name = "claude"

    @staticmethod
    def _rt(profile: dict | None) -> tuple[str, int]:
        rt = jobprofile.runtime(profile or {})
        return (rt.get("claude_bin", "claude"),
                rt.get("max_parallel_claude", claudecall.DEFAULT_WORKERS))

    def preflight(self, profile: dict | None = None) -> tuple[bool, str]:
        claude_bin, _ = self._rt(profile)
        return claudecall.preflight(claude_bin)

    def plan(self, rows: list, *, batch_size: int) -> list[list]:
        return [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]

    def dry_run_preview(self, profile: dict, unit: list, *,
                        master: str | None = None) -> str:
        # `master` is accepted and ignored on purpose: score.build_prompt does not use the
        # resume, and feeding it one would change the claude path's prompt. Same in
        # score_reqs below. Do not "fix" this.
        return score.build_prompt(profile, unit)

    def _run(self, prompts: list[str], profile: dict | None, timeout: int, announce: str):
        claude_bin, workers = self._rt(profile)
        try:
            return claudecall.run(prompts, claude_bin, timeout, workers, announce=announce)
        except claudecall.Unusable as e:
            raise engine.EngineUnusable(str(e)) from e
        except claudecall.AuthExpired as e:
            # Mid-stage: some batches may already have been written, which is why this prints
            # `aborted:` in the stage rather than `claude is not usable:`.
            raise engine.EngineUnusable(str(e), mid_stage=True) from e

    def score_reqs(self, profile: dict, rows: list, *, master: str | None = None,
                   batch_size: int = 12, announce: str = "",
                   on_batch=None) -> list[dict]:
        batches = self.plan(rows, batch_size=batch_size)
        replies = self._run([score.build_prompt(profile, b) for b in batches],
                            profile, score.CALL_TIMEOUT, announce)

        out: list[dict] = []
        offset = 0
        for i, (batch, (raw, err)) in enumerate(zip(batches, replies), 1):
            results, error = None, err
            if err is None:
                try:
                    # `n` is 1-based into `rows`, not into the batch, on both the return value
                    # and the callback. The offset is what lets a caller write
                    # `rows[r["n"] - 1]`, which is score.py's existing `batch[r["n"] - 1]`
                    # with one substitution.
                    results = [dict(r, n=r["n"] + offset)
                               for r in score.parse_scores(raw, len(batch))]
                except Exception as e:  # noqa: BLE001
                    error = engine.UnparseableReply(str(e))
            if on_batch is not None:
                on_batch(i, len(batches), len(batch), results, error)
            if results:
                out.extend(results)
            offset += len(batch)
        return out

    def draft_referrals(self, profile: dict, rows: list, kind: str, *,
                        announce: str = "") -> list:
        replies = self._run([draft.build_prompt(profile, r, kind) for r in rows],
                            profile, draft.CALL_TIMEOUT, announce)
        out = []
        for raw, err in replies:
            out.append(err if err is not None else draft.split_sections(raw.strip()))
        return out

    def tailor_resumes(self, master: str, profile: dict, rows: list, *,
                       announce: str = "") -> list:
        replies = self._run([tailor.build_prompt(master, profile, r) for r in rows],
                            profile, tailor.CALL_TIMEOUT, announce)
        out = []
        for raw, err in replies:
            out.append(err if err is not None else tailor.split_sections(raw))
        return out
