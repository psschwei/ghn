"""``ghn clean`` — remove finished items from the inbox doc (offline, no model, no network).

The inbox at ``config.INBOX_PATH`` is hand-curated and items stay until removed by hand; over
time it accumulates finished work. ``clean`` drops every item whose recorded ``State`` is a
finished state (merged / closed / draft), backs the doc up first (the same best-effort
``backup_inbox`` the pipeline uses, so a removal is recoverable), and reports what it removed.

Detection is purely the doc's own ``| State | ... |`` metadata row — no ``gh`` calls — so an
item last folded as ``open`` that has since merged on GitHub is only cleaned once a later
notification refreshes its State. This mirrors ``eval.py``: a small self-contained dev-facing
subcommand dispatched from ``main.py`` before the argparse request parser.
"""

from __future__ import annotations

from pathlib import Path

from . import config
from .loader import strip_finished_items
from .pipeline import backup_inbox

# Items in any of these states have nothing actionable left (or, for drafts, nothing yet), so
# ``clean`` evicts them. Matched case-insensitively against the State metadata cell.
REMOVE_STATES: frozenset[str] = frozenset({"merged", "closed", "draft"})


def run_clean(argv: list[str] | None = None) -> int:
    """Remove merged/closed/draft items from the inbox doc. Returns a process exit code."""
    path = Path(config.INBOX_PATH)
    if not path.exists() or path.stat().st_size == 0:
        print(f"Inbox {config.INBOX_PATH} is empty or missing; nothing to clean.")
        return 0

    text = path.read_text(encoding="utf-8")
    new_text, removed = strip_finished_items(text, REMOVE_STATES)

    if not removed:
        print("Nothing to clean — no merged, closed, or draft items in the inbox.")
        return 0

    # Back up before overwriting (best-effort, never raises), then write the pruned doc.
    backup_inbox(path)
    path.write_text(new_text, encoding="utf-8")

    print(
        f"Cleaned {len(removed)} item{'s' if len(removed) != 1 else ''} "
        f"from {config.INBOX_PATH}:"
    )
    for heading, url, state in removed:
        print(f"  [{state}] {heading}")
        print(f"          {url}")
    return 0
