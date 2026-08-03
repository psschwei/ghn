"""Deterministic parsing/loading helpers (no LLM).

  * read_existing_inbox (elem_005) — parse the existing inbox doc (config.INBOX_PATH) into
    a map keyed by :URL: (the html_url de-dup key) -> raw item subtree text + last-seen
    timestamp (the per-item cutoff for the new-activity delta) + any :NOTES: property text
    (user-owned free text, preserved verbatim across runs).
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
# A priority org tag on a heading line (``* Title :high:``), possibly among other tags
# (``* Title :waiting:high:``). Used to read a carried item's priority back out of the doc so
# it can be preserved rather than reset.
_PRIORITY_TAG_RE = re.compile(r":(high|medium|low):")
# The doc-level ``#+DATE: 2026-06-16 18:01`` header (matches current_timestamp format).
_DOC_DATE_RE = re.compile(r"^\s*#\+DATE:\s*(.+?)\s*$", re.IGNORECASE)
# The ``| State | <value> |`` row of an item's metadata table (render_item_subtree writes it
# first). ``clean`` reads the value to decide whether an item is finished (merged/closed/draft).
_STATE_ROW_RE = re.compile(r"^\s*\|\s*State\s*\|\s*(.+?)\s*\|", re.MULTILINE)


def read_existing_inbox(inbox_path: str | Path) -> dict[str, dict[str, Any]]:
    """Parse the existing inbox doc into ``{html_url: {"block", "last_seen", "notes"}}``.

    Each tracked item is an org heading (a top-level ``*`` heading tagged with its priority,
    e.g. ``* Fix auth flow :high:``) immediately followed by a ``:PROPERTIES:`` drawer
    holding a ``:URL:`` property (and, for items written by a LAST_SEEN-aware run, a
    ``:LAST_SEEN:`` property). The presence of ``:URL:`` — not heading depth — is what marks
    a heading as an item, so this also parses an older doc whose items were ``**`` headings
    nested under ``* High/Medium/Low Priority`` bucket headings. The returned map uses that
    ``:URL:`` as the de-dup key (SKILL.md Step 2 / Step 5). An item subtree runs from its
    heading line up to (but not including) the next heading at the same-or-shallower depth.

    ``last_seen`` is the ISO-8601 cutoff for fetching new activity on the next run; it is
    ``None`` for items written before LAST_SEEN tracking existed (callers fall back to the
    doc-level ``#+DATE:`` header, see ``read_doc_date``).

    ``notes`` is the value of the item's ``:NOTES:`` property, or ``None`` when the line is
    empty/absent (an auto-emitted empty ``:NOTES:`` line reads as ``None`` — nothing to
    carry). It is user-owned free text the pipeline never parses; it is threaded back through
    and re-emitted so a note the user typed survives every rebuild path.

    ``priority`` is the ``:high:``/``:medium:``/``:low:`` org tag read off the item's heading, or
    ``None`` for a block written before priority tags existed. The pipeline needs it to preserve
    a carried-over item's priority: without it, an item that simply stops generating
    notifications would be re-ranked to the bottom on every run, which inverts urgency (an
    unattended review request goes quiet precisely *because* nobody is acting on it).

    Returns an empty map when the file does not exist (NO_EXISTING_INBOX).
    """
    path = Path(inbox_path)
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()

    # Collect item blocks. An item is any org heading whose :PROPERTIES: drawer carries a
    # :URL: — that presence (checked below via the `if url:` guard), not heading depth, is
    # what identifies an item. Items are now top-level `*` headings tagged with their
    # priority (:high:/:medium:/:low:); we accept any depth >= 1 so this still round-trips an
    # older doc that had `** items` nested under `* High/Medium/Low Priority` bucket headings
    # (those bucket headings have no :URL: and are skipped).
    items: dict[str, dict[str, Any]] = {}
    n = len(lines)
    i = 0
    while i < n:
        m = _HEADING_RE.match(lines[i])
        if not m:
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
        # A heading is an item only if its OWN property drawer (the lines from this heading
        # up to the first nested heading) carries a :URL:. Restricting the lookup to the
        # heading's own drawer keeps a URL-less container heading — e.g. a retired
        # ``* High Priority`` bucket in an older doc — from claiming a nested item's :URL:
        # (and swallowing that item's block). Such a container matches no URL here, so we
        # fall through and advance past just its heading line, letting its ``**`` children be
        # parsed as items on their own.
        own_drawer = _own_drawer_lines(block_lines)
        url = _extract_prop(own_drawer, "URL")
        if url:
            block = "\n".join(block_lines)
            items[url] = {
                "block": block,
                "last_seen": _extract_prop(own_drawer, "LAST_SEEN"),
                "notes": _extract_prop(own_drawer, "NOTES"),
                "priority": _extract_priority(block_lines[0]),
            }
            i = j
        else:
            # Not an item (no :URL: in its own drawer) — skip only this heading line so any
            # nested headings that ARE items still get their own turn.
            i = start + 1
    return items


def strip_finished_items(
    text: str, remove_states: frozenset[str]
) -> tuple[str, list[tuple[str, str, str]]]:
    """Drop item subtrees whose recorded ``State`` is a finished state (``ghn clean``).

    Walks ``text`` the same way ``read_existing_inbox`` does — an item is any org heading whose
    own :PROPERTIES: drawer carries a :URL: — and reads each item's ``| State | <value> |``
    metadata row (``_STATE_ROW_RE``). When that value (lower-cased) is in ``remove_states``
    (e.g. ``{"merged", "closed", "draft"}``), the whole item subtree is dropped, including its
    trailing blank lines, so the kept items stay correctly separated. Every other line — the
    doc header (``#+TITLE:`` / ``#+DATE:``, deliberately left intact), URL-less container
    headings, kept items, and any hand-added prose — is emitted verbatim. This is a
    line-level verbatim filter, not a re-render, so curated text (including :NOTES:) is
    preserved byte-for-byte.

    An item with no ``State`` row is kept (conservative — we only remove what we can positively
    identify as finished).

    Returns ``(new_text, removed)`` where each ``removed`` entry is ``(heading, url, state)``.
    ``new_text`` follows ``render_inbox_org``'s convention (a single trailing newline). When
    nothing is removed, ``new_text`` is the input normalised to that same convention and
    ``removed`` is empty.
    """
    lines = text.splitlines()
    out: list[str] = []
    removed: list[tuple[str, str, str]] = []
    n = len(lines)
    i = 0
    while i < n:
        m = _HEADING_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        depth = len(m.group(1))
        start = i
        j = i + 1
        # Extend the subtree until the next heading of same-or-shallower depth (mirrors
        # read_existing_inbox's span logic).
        while j < n:
            mh = _HEADING_RE.match(lines[j])
            if mh and len(mh.group(1)) <= depth:
                break
            j += 1
        block_lines = lines[start:j]
        own_drawer = _own_drawer_lines(block_lines)
        url = _extract_prop(own_drawer, "URL")
        if url:
            sm = _STATE_ROW_RE.search("\n".join(block_lines))
            state = sm.group(1).strip().lower() if sm else None
            if state in remove_states:
                removed.append((block_lines[0].strip(), url, state))
                i = j  # drop the whole subtree (including its trailing blank lines)
                continue
            out.extend(block_lines)
            i = j
        else:
            # Not an item (no :URL: in its own drawer) — emit just this heading line and let any
            # nested headings that ARE items get their own turn.
            out.append(lines[start])
            i = start + 1
    return "\n".join(out).rstrip() + "\n", removed


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


def _own_drawer_lines(block_lines: list[str]) -> list[str]:
    """Return the block's own lines, up to (not including) its first nested heading.

    ``block_lines`` starts with the item's heading; its :PROPERTIES: drawer (and thus its
    :URL:) lives before any nested child heading. Truncating at the first child heading keeps
    a property lookup from reaching into a descendant's drawer — so a URL-less container
    heading does not borrow a child's :URL:.
    """
    if not block_lines:
        return block_lines
    out = [block_lines[0]]  # the heading itself
    for line in block_lines[1:]:
        if _HEADING_RE.match(line):
            break
        out.append(line)
    return out


def _extract_priority(heading_line: str) -> str | None:
    """Return the priority org tag on a heading line, or ``None`` if it carries none.

    Matches the tag anywhere in the heading's tag group so a heading the user has added their own
    tags to (``* Title :waiting:high:``) still reports its priority.
    """
    m = _PRIORITY_TAG_RE.search(heading_line)
    return m.group(1) if m else None


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
