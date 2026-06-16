"""Deterministic parsing/loading helpers (no LLM).

  * read_existing_inbox (elem_005) — parse the existing inbox doc (config.INBOX_PATH) into
    a map keyed by :URL: (the html_url de-dup key) -> raw item subtree text + last-seen
    timestamp (the per-item cutoff for the new-activity delta).
  * read_doc_date (elem_005) — parse the doc-level ``#+DATE:`` header as a fallback cutoff
    for items written before per-item :LAST_SEEN: tracking existed.
  * parse_notification (elem_008) — project a raw gh notification JSON object into a
    typed-ish dict of the fields the pipeline needs.

These are pure functions over text/JSON; org-mode parsing is line-oriented regex work.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

# A property drawer line like ``:URL:  https://...`` or ``:HOST: github.com``.
_PROP_RE = re.compile(r"^\s*:([A-Za-z0-9_]+):\s+(.*?)\s*$")
# Any org heading line: one or more leading stars then a space.
_HEADING_RE = re.compile(r"^(\*+)\s+(.*)$")
# The doc-level ``#+DATE: 2026-06-16 18:01`` header (matches current_timestamp format).
_DOC_DATE_RE = re.compile(r"^\s*#\+DATE:\s*(.+?)\s*$", re.IGNORECASE)


def read_existing_inbox(inbox_path: str | Path) -> dict[str, dict[str, Any]]:
    """Parse the existing inbox doc into ``{html_url: {"block": text, "last_seen": str|None}}``.

    Each tracked item is an org heading (``**`` or ``***``) immediately followed by a
    ``:PROPERTIES:`` drawer holding a ``:URL:`` property (and, for items written by a
    LAST_SEEN-aware run, a ``:LAST_SEEN:`` property). The returned map uses that ``:URL:``
    as the de-dup key (SKILL.md Step 2 / Step 5). An item subtree runs from its heading
    line up to (but not including) the next heading at the same-or-shallower depth, or a
    top-level ``*`` bucket heading.

    ``last_seen`` is the ISO-8601 cutoff for fetching new activity on the next run; it is
    ``None`` for items written before LAST_SEEN tracking existed (callers fall back to the
    doc-level ``#+DATE:`` header, see ``read_doc_date``).

    Returns an empty map when the file does not exist (NO_EXISTING_INBOX).
    """
    path = Path(inbox_path)
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()

    # Collect item blocks: an item heading has depth >= 2 (buckets are depth 1).
    items: dict[str, dict[str, Any]] = {}
    n = len(lines)
    i = 0
    while i < n:
        m = _HEADING_RE.match(lines[i])
        if not m or len(m.group(1)) < 2:
            i += 1
            continue
        depth = len(m.group(1))
        start = i
        j = i + 1
        # Extend the subtree until the next heading of same-or-shallower depth.
        while j < n:
            mh = _HEADING_RE.match(lines[j])
            if mh and len(mh.group(1)) <= depth:
                break
            j += 1
        block_lines = lines[start:j]
        block = "\n".join(block_lines)
        url = _extract_prop(block_lines, "URL")
        if url:
            items[url] = {
                "block": block,
                "last_seen": _extract_prop(block_lines, "LAST_SEEN"),
            }
        i = j
    return items


def read_doc_date(inbox_path: str | Path) -> str | None:
    """Return the doc-level ``#+DATE:`` header value, if present and parseable.

    Used as a fallback cutoff for items that predate per-item :LAST_SEEN: tracking. The
    header is local-time minute granularity (``%Y-%m-%d %H:%M``); we return it as a naive
    local-time ISO string the pipeline normalises before comparing against UTC timestamps.
    Returns ``None`` when absent or unparseable.
    """
    path = Path(inbox_path)
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        dm = _DOC_DATE_RE.match(line)
        if dm:
            raw = dm.group(1).strip()
            try:
                datetime.strptime(raw, "%Y-%m-%d %H:%M")
            except ValueError:
                return None
            return raw
        if line.strip() and not line.startswith("#+"):
            break  # past the header block; no point scanning the body
    return None


def _extract_prop(block_lines: list[str], name: str) -> str | None:
    """Return a named :PROP: drawer value from an item block, if present (case-insensitive)."""
    want = name.upper()
    for line in block_lines:
        pm = _PROP_RE.match(line)
        if pm and pm.group(1).upper() == want:
            return pm.group(2).strip()
    return None


def parse_notification(raw: dict[str, Any], host: str) -> dict[str, Any]:
    """Project a raw gh notification object into the fields the pipeline needs (elem_008).

    Carries the originating ``host`` through so later enrichment / mark-Done calls can
    target the right GitHub instance.
    """
    subject = raw.get("subject") or {}
    repository = raw.get("repository") or {}
    return {
        "id": raw.get("id", ""),
        "reason": raw.get("reason", ""),
        "title": subject.get("title", ""),
        "subject_type": subject.get("type", ""),
        "subject_url": subject.get("url", ""),
        "latest_comment_url": subject.get("latest_comment_url") or "",
        "repo_full_name": repository.get("full_name", ""),
        "updated_at": raw.get("updated_at", ""),
        "host": host,
    }
