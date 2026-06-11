# ghn — Guide

A Mellea-compiled skill that maintains a living "GitHub inbox" Org-mode document at
`~/org/github.org` (override with the `GITHUB_INBOX_PATH` environment variable). Each run
fetches the unread-notification delta from your GitHub
host(s), enriches each item with PR/issue context, folds the delta into the existing inbox
doc (adding new items, regenerating items with new activity, leaving everything else
untouched), then marks those notifications Done so they don't resurface unless something
new happens.

The doc *is* the inbox — items stay until you remove them by hand. GitHub's
`/notifications` feed is treated as a delta of what changed since the last run, never as
the source of truth for the doc's full contents.

## What it does

1. Identifies you on `github.com` and, if configured, your GitHub Enterprise host
   (see [`mellea-setup.md`](mellea-setup.md) §3b).
2. Reads the existing inbox doc (`~/org/github.org`), keyed by each item's `html_url`.
3. Fetches unread notifications (the delta) and applies an optional filter parsed from
   your request ("only issues", "only PRs", "review requests").
4. Enriches each delta item (PR state, review status, latest comment), gated by priority.
5. Classifies each item into **Action Required** / **Should Check** / **FYI** and renders
   a summary for it.
6. Writes the reconciled inbox doc (**before** marking anything Done — the irreversible
   commit point).
7. Confirms the write, then marks each folded thread Done via per-thread
   `DELETE /notifications/threads/{id}` (never the bulk mark-all-read).
8. Prints a conversational summary of what changed.

## Install

```bash
uv sync
```

See [`mellea-setup.md`](mellea-setup.md) for the `gh` CLI prerequisite and model-backend
configuration.

## Usage

```bash
# Full update, no filter
uv run python -m ghn "update my notifications"

# Only pull requests
uv run python -m ghn "only PRs"

# Just review requests
uv run python -m ghn "review requests"
```

Programmatically:

```python
from ghn import run_pipeline

summary = run_pipeline(user_request="what needs my attention on GitHub?")
print(summary.headline)
```

## Entry point

```
run_pipeline(user_request: str = '') -> RunSummary
```

GitHub data is fetched internally via the authenticated `gh` CLI, so the only user-facing
parameter is the natural-language request (which carries the optional filter intent).

## Model backend

- **Backend**: `ollama`
- **Model**: `granite4.1:3b`
