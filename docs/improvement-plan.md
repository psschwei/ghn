# Improvement plan: summaries and prioritization

Findings from an audit on 2026-07-28 of the live inbox (`~/org/github.org`, 28 items) against
`ghn/pipeline.py`, `slots.py`, `schemas.py`, `loader.py`, `tools.py`, and `config.py`.

Motivating complaint: *"the tool is fine for gathering notifications, but the summaries /
prioritization leaves a lot to be desired."*

Line references are as of commit `4f057ba`.

---

## Diagnosis

Three separate problems. Only one is about summary prose.

### 1. The priority signal is dead — mostly a bug, not a model failure

`AGENTS.md` claims the doc is sorted `high` → `medium` → `low`, then by recency within a tier.
It is not. Extracting the tags in document order from the live doc gives pure recency ordering,
with the two `:high:` items at positions 4 and 12 — below seven and nine `:low:` items
respectively.

Priority is stored in two places, written by different code paths that disagree:

- `loader.py:93-97` returns only `block` / `last_seen` / `notes`. It never parses the priority
  tag off the heading, so a carried item cannot retain its priority even in principle.
- `pipeline.py:857` hardcodes `_PRIORITY_RANK["low"]` for every carried-over item.
- `render_activity_delta` (`pipeline.py:556-563`) rewrites `:LAST_SEEN:` and `:NOTES:` but never
  the heading tag, while `_add` (`pipeline.py:825-831`) ranks from the *freshly computed*
  bucket. Tag and sort position therefore actively contradict each other.

The decay direction is also inverted. An unattended review request generates no new
notifications *because* nobody is talking on it, so it falls out of the delta and sinks to
`low`. Staleness should raise urgency.

### 2. The 3B classifier cannot follow its own spec

Measured directly (`ollama`, four representative inputs through `classify_bucket`):

| input | `granite4.1:3b` | `granite4.1:8b` | `slots.py` spec |
|---|---|---|---|
| `assign` | medium | **high** | high |
| `mention` + direct question | medium | **high** | high |
| `author` + CHANGES_REQUESTED | medium | **high** | high |
| `subscribed` | low | low | low |

3B collapses the entire `high` branch. `assign` — the strongest actionability signal GitHub
emits — never reaches `:high:`.

`config.py:141-143` justifies 3B on the grounds that classification is "a small fixed-label
pick, not prose". That premise does not hold: `slots.py:44-80` is a 37-line, 5-input spec with
nested conditionals and cross-field exclusions (*"but NOT if latest_review_state is APPROVED"*).

Compounding it, `pipeline.py:664-666` applies the draft/closed/merged demotion *before* the
involvement checks and then `continue`s. An explicit @-mention on a **draft** PR is forced to
`:low:` — and a draft PR is exactly where a colleague @-mentions you to ask a design question.

### 3. The feed is ~83% `subscribed`, so a 3-way bucket has no headroom

Live sample: 4 notifications, 3 `subscribed`. In the 28-item doc, 22 of 28 "Why you're seeing
this" lines read "Repo activity".

The tool pays an 8B model call to write five sentences of prose for items nobody should read.
**The weak summaries are mostly on items that should never have been summarized at all**, which
is why prompt tuning alone will not resolve the complaint.

---

## Confirmed bugs

Distinct from the design weaknesses above — these get fixed, not designed around.

| # | Bug | Location | Notes |
|---|---|---|---|
| B1 | **Silent data loss.** A column-0 `* ` in model delta output becomes an org heading; `loader.py` truncates the item there, permanently destroying appended updates and any user prose below. | `schemas.py:40-42` (bare `str`, no newline constraint), `pipeline.py:562` (raw interpolation), `loader.py:76-80` | **Reproduced.** Not hypothetical: the model already emits 3 column-0 numbered-list lines into the live doc. One character away from triggering. |
| B2 | No backup on the write path, over a hand-curated file. | `pipeline.py:869` (bare `write_text`) | Only `.bkp` on disk is from 2026-07-21. |
| B3 | 6 of 28 `*Latest activity:*` lines are empty. | `config.py:13` withholds the comment; `schemas.py:28-30` makes the field required and non-optional | A required field with no source data forces the model to emit `""` or confabulate. |
| B4 | Markdown leaks into Org-mode: 8 `**bold**`, 7 column-0 numbered lists. | prompt at `pipeline.py:769-778` | Same root cause as B1. |
| B5 | Persona leak: 7 instances of third-person "the user" in reader-facing prose. | `config.py:6` `PREFIX_TEXT` | |
| B6 | Stale metadata tables on delta items — a merged, approved PR renders as `State open / Approved by —`. | `pipeline.py:556-563` reuses `prev_block` verbatim | Fresh `enriched` data *is* fetched at `pipeline.py:255` and discarded. |
| B7 | `GHN_BACKEND=litellm` + `GHN_API_KEY` raises `TypeError`. | `config.py:120-126` forwards `api_key`; `LiteLLMBackend.__init__` accepts no `api_key` and no `**kwargs` | Its `base_url` is also stored but never used in the completion call. |
| B8 | Dead code shipped as live: `REASON_REFERENCE`, `LOOP_BUDGET`, `FORBIDDEN_BULK_MARK_READ_NOTE` — all defined, never read. | `pipeline.py:70-80`, `config.py:145`, `config.py:16` | `REASON_REFERENCE` is the exact deterministic table that would fix the classifier for free. Raising `LOOP_BUDGET` does nothing — Mellea's own `RejectionSamplingStrategy` default governs retries. |
| B9 | Cannot mix backends. `BACKEND` is a single global, so "hosted model for summaries, local for classification" is impossible — only `MODEL_ID` / `CLASSIFIER_MODEL_ID` split. | `config.py:108` | Blocks Tier 1 item 10. |
| B10 | `_gh`'s method allowlist validates a *string parameter*, not the verb `gh` actually sends. `gh api graphql` passes as `"GET"` while issuing a POST. | `tools.py:38-58` | Tighten before relying on the allowlist as a safety invariant. |

---

## Tier 0 — free wins — DONE (commit `fa821dc`)

| # | Change | Status |
|---|---|---|
| 0.1 | Classifier default → `granite4.1:8b`. | done — recovers all four collapsed branches |
| 0.2 | `flatten_prose` sanitizes all model output at the render boundary. Closes **B1**, and **B4** as a side effect. | done — plus `_compact_inline_updates` to heal existing blocks |
| 0.3 | `backup_inbox` writes `github.org.bak-<stamp>` before each overwrite, keeping 10. Closes **B2**. | done |
| 0.4 | `_set_priority_tag` (replaces rather than skips) + `loader` returns a `priority` key. Fixes the tag/sort divergence **and** the carried-item decay. | done |
| 0.5 | Involvement outranks the draft demotion; closed/merged still unconditional. Also derives `is_assignee`, which was fetched and never used. | done |
| 0.6 | `why_seeing` looked up from `REASON_DISPLAY`; `latest_activity` omitted rather than emitted empty. Closes **B3**. | done |
| 0.7 | Step 8 `RunSummary` model call deleted; headline built from the counts. Drops the now-dead `run_summary_max_tokens` knob. | done |

Verified against the real notification feed with mark-Done suppressed: correct priority order,
zero empty activity lines, zero markdown leakage, no truncation, `:NOTES:` preserved, backup
written, and the update fold idempotent per block.

Still open from the bug list: **B5** (persona leak — only in prose written by earlier runs; new
prose is clean), **B7** (litellm `api_key` crash), **B8** (`REASON_REFERENCE` /
`_parse_instruct_result` still dead — likely inputs to 2.11), **B9** (single-backend limit,
blocks 1.10), **B10** (`_gh` method allowlist).

## Tier 1 — cheap and high-impact

| # | Change | Notes |
|---|---|---|
| 1.8 | **Ingest filtering / tiered rendering.** Give `subscribed` items a one-line table row instead of a 26-line block. | Likely improves perceived summary quality more than any prompt change, and cuts ~20 model calls per run. |
| 1.9 | Rewrite the item prompt and `ItemRender` for action over paraphrase: replace `summary` / `why_seeing` / `latest_activity` with `action` (single next step), `blocked_on` (whose court the ball is in), `state_line`. Add explicit *"address the reader as you"*, *"no markdown — this is an Org-mode document"*, *"do not restate the metadata table"*. | Closes B4, B5. Consider gating on 1.16. |
| 1.10 | Per-role *backend* config, not just per-role model id — so summaries can use a strong hosted model while classification stays local. | Requires fixing B9. |

## Tier 2 — structural

| # | Change | Notes |
|---|---|---|
| 2.11 | **Deterministic scored priority**, replacing the classifier for everything except the one genuinely fuzzy call (*"is this comment a question directed at me?"*). Every input is already fetched and discarded: assignee, author, requested reviewer, `approved_by`, `mergeable_state`, `created_at`. | Maps cleanly onto org `[#A]/[#B]/[#C]` cookies, which org-agenda sorts natively. |
| 2.12 | Refresh the metadata table on delta items. Closes B6. | Data is already in hand. |
| 2.13 | Add high-value unused signals, all GET-only: CI check-runs, `requested_teams`, diff size, age/staleness, last-commenter. | `_PR_JQ` omits `requested_teams` entirely — mellea review requests arrive via a CODEOWNERS *team*, which is why only one item is ever `:high:`. Highest-leverage single item here. |
| 2.14 | Two-pass render with an `* Overview` section at the top. | Counts and high-priority titles are already computed at `pipeline.py:802-828` and sent to stdout instead of the doc. |
| 2.15 | Parallelize enrichment. | Currently 28 items × 3-7 blocking subprocess calls, fully serial. |
| 2.16 | **Eval harness** — DONE. `ghn eval` runs the real `run_pipeline` with the network (`ghn.tools`) and model (`start_session` + both classify slots) stubbed from `tests/fixtures/*.json`, diffing the written doc against `tests/baseline/*.org`. 6 scenarios (priority matrix, sanitization, delta-fold, carried-over, empty-delta, full-rerender-notes). `--update` regenerates goldens. Stdlib only. | The deterministic core runs for real — ordering, fold/dedup, `flatten_prose`, backup, mark-Done, and the Tier-0.5 priority decision — so 2.11's scorer will be exercised live once it lands. Goldens capture current behaviour incl. known bugs (B6). |

## Tier 3 — move judgment into Claude

Ranked.

1. **Hybrid (recommended end state).** `ghn --emit-json` dumps enriched, un-summarized items; a
   skill does summarization, prioritization, and cross-item synthesis with a frontier model and
   writes the org doc; `ghn --mark-done` closes the loop. Keeps the fold / dedup / mark-Done
   invariants in tested Python and puts judgment where judgment is good. Sacrifices offline
   operation for the summary step only.
2. **Backend swap.** Cheapest path to "better model", but see B7 — and Mellea has no Anthropic
   backend, so this needs a LiteLLM proxy or an OpenAI-compatible gateway.
3. **Full Claude skill.** Ranked last. The existing `~/.claude/agents/github-notifications.md`
   already demonstrates the failure mode: it writes to `/tmp` and does not maintain the living
   doc. The stateful fold is the hard part, and it is the part Python does well.

## Rejected

- **Batching several items into one model call** — breaks the one-schema-per-session invariant
  (KB5) for marginal gain.
- **GraphQL migration as a first move** — worth doing eventually (one query could replace 3-6
  REST calls per item), but B10 must be fixed first so the method allowlist means what it says.

---

## Sequencing

1. Tier 0 (0.1–0.5 first — these close the data-loss risk and make prioritization work at all).
2. 2.16 (eval harness) before any prose-prompt tuning, so 1.9 is verifiable.
3. 1.8 next — it reduces the surface that everything downstream has to summarize.
4. Then 2.11 / 2.13 together; scored priority is much stronger with `requested_teams` and CI data.
5. Tier 3 hybrid as the end state, once the JSON contract is settled.

---

## Appendix: inbox loss on 2026-07-28

During the audit session `~/org/github.org` went from 28 items to 1 (`#+DATE: 2026-07-28 12:09`).
The cause was not established. No subagent invoked the real pipeline (all repro work used
tempfiles); one auditor observed the file dropping 28 → 24 mid-read and attributed it to hand
editing, and there is Ollama traffic at 12:08–12:09 consistent with a `ghn` run.

Both states are preserved:

- `~/org/github.org.recovered-2026-07-28` — full 729-line, 28-item doc, verified to parse
  (`read_existing_inbox` → 28 items)
- `~/org/github.org.post-loss-2026-07-28` — the 1-item state

The surviving item (`mellea/pull/1443`) is byte-identical in the recovery, so restoring the
recovered file loses nothing. This episode is the concrete argument for B2.
