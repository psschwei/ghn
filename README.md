# ghn

Managing GitHub notifications with [GTD](https://gettingthingsdone.com/) and a living
[Org-mode](https://orgmode.org/) inbox.

Instead of living in GitHub's web UI, the things that need your attention land in a single
document on your laptop and stay there until you deal with them. Each run treats GitHub's
`/notifications` feed as a *delta* of what changed since the last run, folds that delta
into the doc, then marks those threads Done so they don't resurface unless something new
happens. The doc *is* the inbox; items stay until you remove them by hand.

![Example of the Org-mode notifications inbox](docs/images/example.png)

## Getting started

GitHub access goes through the authenticated [`gh` CLI](https://cli.github.com/), so make
sure you're logged in (`gh auth login`). Then:

```bash
git clone https://github.com/psschwei/ghn.git
cd ghn
uv tool install .
ghn
```

See [`docs/mellea-setup.md`](docs/mellea-setup.md) for full setup (model backend, hosts)
and [`docs/mellea-guide.md`](docs/mellea-guide.md) for the complete guide. The
same workflow is also packaged as a [Claude Code agent](docs/claude-agent.md).
