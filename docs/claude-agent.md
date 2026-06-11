# notifications

Managing GitHub Notifications with Claude and GTD.

This repo holds a Claude Code agent that maintains a **living "GitHub inbox"** — a
single [Org-mode](https://orgmode.org/) document on your laptop that you treat as your
offline notifications inbox. Instead of living in GitHub's web UI, the things that need
your attention land in `~/org/github.org`, and stay there until you deal with them.

## How it works

Each run, the `github-notifications` agent:

1. Fetches your **new (unread)** notifications from GitHub — this is the *delta* of what
   changed since the last run.
2. Enriches each one with context (PR state, review status, latest comment).
3. Folds the delta into `~/org/github.org`, keyed by URL:
   - new item → added
   - item already in the doc with new activity → its entry is regenerated from scratch
   - everything else in the doc → left untouched
4. Writes the doc, then **marks those notifications as Done** (removed from your GitHub
   inbox — they won't resurface unless something new happens).

Notifications are a *feed of changes*, not the source of truth. The doc is the inbox.
Items only leave when **you delete them** from `~/org/github.org` by hand — that's
the seam for a future GTD workflow.

The inbox lives at `~/org/github.org` by default, outside this repo, so your worklist
stays local. Set the `GITHUB_INBOX_PATH` environment variable to put it somewhere else.

## Running it

Open Claude Code in this repo and ask the `github-notifications` agent to update your
inbox. For example:

```
update my notifications inbox
update my inbox, only PRs
update my inbox, only issues
update my inbox, just review requests
```

The agent writes to `~/org/github.org` (or `$GITHUB_INBOX_PATH`).
