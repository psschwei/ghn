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
uv run python -m ghn clean                    # prune merged/closed/draft items from the inbox doc (offline)
uv run python -c "from ghn import run_pipeline; print('ok')"  # smoke-test the import
uv run python -m ghn eval                     # offline eval: real pipeline vs checked-in goldens
uv run python -m ghn eval --update            # regenerate goldens after an intentional output change
```

## Eval harness (`ghn eval`)

`ghn/eval.py` + `tests/{fixtures,baseline}/` are an offline regression check (plan item 2.16).
Each `tests/fixtures/*.json` scenario supplies canned GitHub data and model prose; the harness
stubs the network (`ghn.tools`) and the model (`start_session` + the two classify slots), then
runs the **real** `run_pipeline` against a temp inbox and diffs the written Org doc against
`tests/baseline/<scenario>.org`. What runs for real is the whole deterministic core: priority
decision (involvement beats the draft demotion, closed/merged forced low), flat-list ordering,
fold/dedup, `flatten_prose` sanitisation, the metadata table, backup, and mark-Done. Only model
prose and the fuzzy bucket call are canned, so this locks machinery, not model quality — run it
before any prose-prompt or ordering change. Goldens capture *current* behaviour, including
known-open bugs (e.g. B6 stale delta metadata); a fix intentionally changes a golden via
`--update`. No network, no model, no new deps (stdlib `unittest.mock`); the `tests/` tree is not
shipped in the wheel.

## Prerequisites at runtime

- **`gh` CLI**, authenticated (`gh auth login`). All GitHub access goes through it; no
  token is embedded. An unauthenticated host is silently skipped.
- **Ollama** running with the model pulled (default `granite4.1:8b` for both summaries and
  classification — 3B collapses `classify_bucket`'s whole "high" branch, so `assign`, `mention`
  and `author`+CHANGES_REQUESTED never reach high priority). Backend/models are set in `ghn/config.py`
  (`BACKEND` / `MODEL_ID` / `CLASSIFIER_MODEL_ID`), all env-overridable. To use a hosted or
  OpenAI-compatible endpoint instead of local Ollama, set `GHN_BACKEND=openai` (or
  `litellm`) plus `GHN_BASE_URL` (endpoint) and `GHN_API_KEY`. `GHN_BASE_URL` also works for
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

## Configuration

Every runtime knob resolves in this order (first wins): the environment variable →
`~/.config/ghn/config.toml` `[section] key` → the hardcoded default in `config.py`. The
TOML file lives outside the working tree, so it applies the same whether run from the repo
or after `uv tool install` (unlike a cwd-based dotenv). Sections: `[github]`
(`enterprise_host`, `inbox_path`), `[backend]` (`backend`, `base_url`, `api_key`),
`[model]` (`model_id`, `classifier_model_id`, `item_summary_max_tokens`),
`[llama]` (`spawn`, `binary`, `model`, `port`, `health_timeout`,
`args`). Empty/whitespace-only values are treated as unset and fall through.

## Architecture

Single linear pass, orchestrated by `pipeline.py:run_pipeline()` over 8 steps (documented
in that file's module docstring). The module layout maps onto Mellea's compiled-skill
conventions:

- **`pipeline.py`** — the orchestrator and the only place LLM sessions are opened. Holds
  all deterministic logic: Org-mode rendering/assembly, PR-template stripping, cutoff
  normalization (local `#+DATE` ↔ ISO-8601 UTC), priority routing, fold/carry-over
  reconciliation, priority-ordered assembly, and the write→mark-Done sequencing.
- **`tools.py`** — the *only* module that shells out to `gh`. Every call goes through `_gh`,
  which enforces an **allowlist of hosts (`ALLOWED_HOSTS`, from config) and HTTP methods
  (`ALLOWED_METHODS` = GET + DELETE only)**. This makes the destructive bulk
  `PUT /notifications -f read=true` mark-all-read call structurally unreachable — do not
  weaken this. Mark-Done is per-thread `DELETE /notifications/threads/{id}`.
- **`slots.py`** — the two `@generative` LLM classifiers (`classify_filter_mode`,
  `classify_bucket`). Their behavior is specified entirely in the docstrings (body is `...`);
  to change classification rules, edit the docstring, not code.
- **`schemas.py`** — Pydantic models for structured LLM output (`ItemRender`, `ActivityDelta`,
  `RunSummary`) plus the `FilterMode` / `Bucket` Literals (`Bucket` = `"high" | "medium" | "low"`).
- **`loader.py`** — pure text/JSON parsing: reads the existing inbox doc into a
  `{html_url: {block, last_seen}}` map and projects raw `gh` notification JSON.
- **`config.py`** — scalar constants only (env- and TOML-overridable: `GITHUB_INBOX_PATH`,
  `GITHUB_ENTERPRISE_HOST`, `GHN_*_MAX_TOKENS`, `GHN_BACKEND`, `GHN_BASE_URL`, `GHN_API_KEY`,
  `GHN_MODEL_ID`, `GHN_CLASSIFIER_MODEL_ID`, and the `GHN_LLAMA_*` spawn knobs). Lookup
  *tables* live in `pipeline.py`, not here. `BACKEND_KWARGS` (a dict) is the one exception to
  scalar-only, built from the endpoint envs.
- **`llama_server.py`** — the `spawned_llama_server()` context manager (start `llama-server`,
  poll `/health`, yield the base URL, terminate on exit). Only used when `GHN_LLAMA_SPAWN` is
  on; wired in `main.py`, not the pipeline.
- **`main.py`** — CLI entry point (`ghn` script). Parses the natural-language request,
  optionally wraps the run in `spawned_llama_server()`, runs the pipeline, prints the summary.
  Dispatches the `eval` and `clean` dev subcommands before argparse so their flags don't collide
  with the free-form request.
- **`clean.py`** — the `ghn clean` subcommand: prune merged/closed/draft items from the inbox
  doc. Offline (no fetch, no model) — it reads each item's recorded `State` metadata row via
  `loader.strip_finished_items`, backs the doc up (`pipeline.backup_inbox`), rewrites it, and
  reports removals. `run_pipeline` is untouched.

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
- **The inbox is one flat, priority-ordered list — priority is an org tag, not a subsection.**
  Every item is a top-level `*` heading carrying its priority as a trailing org tag
  (`:high:` / `:medium:` / `:low:`); there are no `* High/Medium/Low Priority` section
  headings (that layout, and the older `Action Required / Should Check / FYI` buckets, are
  retired). Items are sorted most-important-first: priority rank (`high` → `medium` → `low`),
  then recency within a tier. Position implies importance; the tag keeps priority visible and
  searchable (org agenda tag-match). `_ensure_priority_tag` back-fills a tag idempotently onto
  carried blocks that predate the tag layout; `_set_priority_tag` *replaces* it on delta-mode
  blocks, which are re-classified each run.
- **The heading tag and the sort position must always agree.** They are written by different code
  paths, so it is easy to break: the sort rank comes from `item["bucket"]`, the visible tag from
  the block text. Three paths must stay in sync — full render (`render_item_subtree`), delta
  (`render_activity_delta` → `_set_priority_tag`), and carried-over (which reads the prior tag
  back via `loader`'s `priority` key). When they diverged, the doc was ordered by pure recency
  while displaying `:high:` tags mid-list, so neither signal could be trusted.
- **Carried-over items keep their previous priority; they are not reset to `low`.** An item goes
  quiet precisely because nobody is acting on it, so demoting on silence inverts urgency. Blocks
  with no tag to read (pre-tag-layout) still default to `low`.
- **Priority overrides run in a fixed order** in `pipeline.py` (no model call): closed/merged →
  `low` unconditionally; then direct involvement (still a `requested_reviewer`, or the user is an
  assignee) → `high`, *including on drafts*; then draft with no involvement → `low`; everything
  else goes to the `classify_bucket` slot. Involvement must outrank the draft demotion — a draft
  is where colleagues @-mention or assign you, and demoting first buried exactly those items. We
  key off `requested_reviewers`, not `reason`, because GitHub flips `review_requested` to
  `comment` once the user comments.
- **Model prose is flattened before it enters the doc (`flatten_prose`).** A column-0 `*` in
  model output is an Org heading, and `loader` ends the item's subtree there — silently
  destroying appended `*Update` history and any user prose below. The models do emit column-0
  list markup despite the prompts, so this is enforced structurally, never by instruction alone.
  `_compact_inline_updates` heals blocks written before this existed. **Assignees are shown for
  both issues and PRs**; Reviewers / Approved by / Reviewed by / Merge queue rows are PR-only.
- **The doc is backed up before every write** (`backup_inbox`, keeping the newest 10 as
  `github.org.bak-<stamp>`). The overwrite is wholesale and the doc is the only record of the
  inbox, so a bad fold would otherwise be unrecoverable. Best-effort: it never raises, so it
  cannot block the write that actually persists the run.
- **The run summary is deterministic — Step 8 makes no model call.** It used to ask a model to
  paraphrase the four counts the pipeline had just computed, which cost a model load per run and
  could misreport the run when the model echoed a count back wrong.

## Multi-host support

The pipeline runs against `github.com` plus an optional GitHub Enterprise host
(`GITHUB_ENTERPRISE_HOST` env var, or `~/.config/ghn/config.toml`). `host` is threaded
through every notification dict and every `tools.py` call so enrichment and mark-Done hit
the right instance.
