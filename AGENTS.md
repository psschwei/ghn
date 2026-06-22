# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ghn` is a CLI that maintains a living Org-mode "GitHub inbox" at `~/org/github.org`. Each
run treats GitHub's `/notifications` feed as a *delta* of what changed since the last run,
folds that delta into the existing doc, then marks those threads Done. The doc — not the
GitHub feed — is the source of truth for the inbox's full contents; items stay until
removed by hand.

It is a [Mellea](https://github.com/generative-computing/mellea)-compiled skill: the code
was generated from a `SKILL.md` spec, which is why source comments carry `elem_*` / `KB*`
provenance markers and `PROVENANCE:` references.

## Commands

Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                       # install deps into .venv
uv run python -m ghn "update my notifications" # run (full update, no filter)
uv run python -m ghn "only PRs"               # filtered runs: "only PRs" / "only issues" / "review requests"
ghn                                           # after `uv tool install .`
uv run python -c "from ghn import run_pipeline; print('ok')"  # smoke-test the import
```

There is **no test suite** in this repo (the `.pytest_cache` is stale/empty) and no lint
config. Verify changes by running the pipeline.

## Prerequisites at runtime

- **`gh` CLI**, authenticated (`gh auth login`). All GitHub access goes through it; no
  token is embedded. An unauthenticated host is silently skipped.
- **Ollama** running with the model pulled: `ollama pull granite4.1:3b`. Backend/model are
  set in `ghn/config.py` (`BACKEND` / `MODEL_ID`).

## Architecture

Single linear pass, orchestrated by `pipeline.py:run_pipeline()` over 8 steps (documented
in that file's module docstring). The module layout maps onto Mellea's compiled-skill
conventions:

- **`pipeline.py`** — the orchestrator and the only place LLM sessions are opened. Holds
  all deterministic logic: Org-mode rendering/assembly, PR-template stripping, cutoff
  normalization (local `#+DATE` ↔ ISO-8601 UTC), bucket routing, fold/carry-over
  reconciliation, and the write→mark-Done sequencing.
- **`tools.py`** — the *only* module that shells out to `gh`. Every call goes through `_gh`,
  which enforces an **allowlist of hosts and HTTP methods (GET + DELETE only)**. This makes
  the destructive bulk `PUT /notifications -f read=true` mark-all-read call structurally
  unreachable — do not weaken this. Mark-Done is per-thread `DELETE /notifications/threads/{id}`.
- **`slots.py`** — the two `@generative` LLM classifiers (`classify_filter_mode`,
  `classify_bucket`). Their behavior is specified entirely in the docstrings (body is `...`);
  to change classification rules, edit the docstring, not code.
- **`schemas.py`** — Pydantic models for structured LLM output (`ItemRender`, `ActivityDelta`,
  `RunSummary`) plus the `FilterMode` / `Bucket` Literals.
- **`loader.py`** — pure text/JSON parsing: reads the existing inbox doc into a
  `{html_url: {block, last_seen}}` map and projects raw `gh` notification JSON.
- **`config.py`** — scalar constants only (env-overridable: `GITHUB_INBOX_PATH`,
  `GITHUB_ENTERPRISE_HOST`, `GHN_*_MAX_TOKENS`). Lookup *tables* live in `pipeline.py`, not here.

### Key invariants

- **Write before mark-Done.** The inbox doc is written first (the irreversible commit
  point); threads are only marked Done after `confirm_inbox_written()` passes. A failed
  mark-Done leaves the thread to be retried next run — never reorder this.
- **De-dup key is `html_url`.** New vs. known items, and the fold/carry-over decision, are
  all keyed on it.
- **Per-item cutoff drives the new-activity delta.** Each item stores `:LAST_SEEN:` in its
  Org property drawer; on the next run, known items fetch only comments/reviews since that
  cutoff and render an `ActivityDelta` instead of a full re-summary. The doc-level `#+DATE`
  header is the fallback cutoff for items predating `:LAST_SEEN:` tracking.
- **One Pydantic schema per Mellea session (KB5).** Each distinct structured output
  (`ItemRender`, `ActivityDelta`, `RunSummary`, and each slot) gets its own
  `start_session()`. Don't share a session across different schemas.
- **Buckets are Action Required / Should Check / FYI.** Closed/merged items and draft PRs
  are forced to FYI deterministically in `pipeline.py` (no model call); a live PR where the
  user is still a requested reviewer is forced to Action Required deterministically (the
  notification `reason` flips from `review_requested` to `comment` once the user comments,
  so we key off `requested_reviewers`, not `reason`); everything else is classified by the
  `classify_bucket` slot.

## Multi-host support

The pipeline runs against `github.com` plus an optional GitHub Enterprise host
(`GITHUB_ENTERPRISE_HOST` env var, or `~/.config/ghn/config.toml`). `host` is threaded
through every notification dict and every `tools.py` call so enrichment and mark-Done hit
the right instance.
