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
  set in `ghn/config.py` (`BACKEND` / `MODEL_ID` / `CLASSIFIER_MODEL_ID`), all env-overridable.
  To use a hosted or OpenAI-compatible endpoint instead of local Ollama, set `GHN_BACKEND=openai`
  (or `litellm`) plus `GHN_BASE_URL` (endpoint) and `GHN_API_KEY`. `GHN_BASE_URL` also works for
  Ollama (e.g. a remote GPU box); `GHN_API_KEY` is ignored by the Ollama backend.
- **Self-hosted `llama.cpp` (spawn-per-run)**: set `GHN_LLAMA_SPAWN=1` to have `main.py` stand
  up its own `llama-server` for the duration of a run (`llama_server.py`), then tear it down —
  even on error. `llama-server` serves an OpenAI-compatible `/v1` endpoint, so the run is
  executed with backend `openai` pointed at `http://127.0.0.1:{port}/v1` via `run_pipeline`'s
  `base_url` override (the module-level `BACKEND`/`BACKEND_KWARGS` are left untouched). One
  model serves both roles: set `GHN_MODEL_ID` and `GHN_CLASSIFIER_MODEL_ID` to the same served
  name. Requires the `llama-server` binary on PATH and a model via `GHN_LLAMA_MODEL` (a local
  `.gguf` path, or a Hugging Face repo spec like `ibm-granite/granite-4.1-8b-GGUF`,
  passed as `-hf`). Knobs: `GHN_LLAMA_BINARY`, `GHN_LLAMA_PORT` (default 8080),
  `GHN_LLAMA_HEALTH_TIMEOUT` (default 300s — a large MoE loads cold slowly),
  `GHN_LLAMA_ARGS` (extra flags, shlex-split, e.g. `-ngl 99 -c 8192`). Trade-off: the model's
  weights load cold on **every** run (no resident daemon like Ollama) — fine for occasional/
  manual runs; for tight loops prefer a persistent server pointed at with `GHN_BASE_URL`.

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
  `GITHUB_ENTERPRISE_HOST`, `GHN_*_MAX_TOKENS`, `GHN_BACKEND`, `GHN_BASE_URL`, `GHN_API_KEY`,
  `GHN_MODEL_ID`, `GHN_CLASSIFIER_MODEL_ID`, and the `GHN_LLAMA_*` spawn knobs). Lookup
  *tables* live in `pipeline.py`, not here. `BACKEND_KWARGS` (a dict) is the one exception to
  scalar-only, built from the endpoint envs.
- **`llama_server.py`** — the `spawned_llama_server()` context manager (start `llama-server`,
  poll `/health`, yield the base URL, terminate on exit). Only used when `GHN_LLAMA_SPAWN` is
  on; wired in `main.py`, not the pipeline.

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
- **The `:NOTES:` property is user-owned and preserved verbatim.** Every item's
  `:PROPERTIES:` drawer carries an always-present `:NOTES:` line (empty by default) the user
  can type a note into. `loader.py` reads it (`_extract_prop(..., "NOTES")`), the pipeline
  threads it through as `notes`, and `render_item_subtree` re-emits it. It survives all three
  rebuild paths: full re-render re-emits the carried value; carried-over/delta-mode reuse the
  block verbatim, and `_ensure_notes_line` back-fills an empty line onto pre-feature blocks
  without ever touching a typed note. Never parse, rewrite, or act on this text — like
  `:LAST_SEEN:`, it's drawer state the pipeline manages structurally, not content.
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
