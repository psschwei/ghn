"""Orchestration for the GitHub notifications inbox skill (Type A, Sequential, P4).

Phase flow (SKILL.md Steps 1-8):
  1. Identify the user on each configured host (tools.fetch_user_logins).
  2. Read the existing inbox doc (loader.read_existing_inbox).
  3. Fetch the unread-notification delta from each configured host (tools.fetch_notifications).
     Classify the user's filter request (slots.classify_filter_mode) and apply it.
     Empty delta -> early exit (no write, no mark Done).
  4. Enrich each delta item (tools.enrich_*), gated by reason priority.
  5. Classify each item into a bucket (slots.classify_bucket) and render its prose
     (m.instruct(format=ItemRender)). Fold against the existing items by html_url.
  6. Render + write the whole inbox doc (deterministic Org-mode assembly).
  7. Confirm the write, then mark each folded thread Done (tools.mark_thread_done).
  8. Generate a conversational run summary (m.instruct(format=RunSummary)).

KB notes baked in: KB1 (parse thunks), KB5 (one BaseModel per session — ItemRender,
RunSummary, and each @generative slot's response model each get their own session),
KB7 (persona via ModelOption.SYSTEM_PROMPT).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mellea import start_session
from mellea.backends.model_options import ModelOption
from pydantic import BaseModel

from .config import (
    BACKEND,
    BACKEND_KWARGS,
    CLASSIFIER_MODEL_ID,
    EMPTY_BUCKET_PLACEHOLDER,
    GITHUB_COM_HOST,
    GITHUB_HOSTS,
    HIGH_PRIORITY_REASONS,
    INBOX_PATH,
    ITEM_SUMMARY_MAX_TOKENS,
    MODEL_ID,
    RUN_SUMMARY_MAX_TOKENS,
    ORG_TITLE,
    PREFIX_TEXT,
    SKIP_LOW_PRIORITY_COMMENT_FETCH,
)
from .loader import parse_notification, read_doc_date, read_existing_inbox
from .schemas import ActivityDelta, ItemRender, RunSummary
from .slots import classify_bucket, classify_filter_mode
from . import tools

# --- C2 lookup tables (elem_010, elem_022) ------------------------------------
# config.py is scalar-only (writer-rendered), so these dict constants live here.

# reason -> human-readable "Why you're seeing this" string.
REASON_DISPLAY: dict[str, str] = {
    "review_requested": "Review requested",
    "mention": "You were mentioned",
    "author": "Activity on your PR/issue",
    "assign": "Assigned to you",
    "comment": "New comment in conversation",
    "subscribed": "Repo activity",
    "team_mention": "Your team was mentioned",
    "state_change": "State change on a watched item",
    "ci_activity": "CI status changed",
}

# reason -> coarse priority (informational; bucket decision is the slot's job).
REASON_REFERENCE: dict[str, str] = {
    "assign": "high",
    "review_requested": "high",
    "mention": "high",
    "author": "medium",
    "comment": "medium",
    "team_mention": "medium",
    "subscribed": "low",
    "state_change": "low",
    "ci_activity": "low",
}

_HIGH_PRIORITY: frozenset[str] = frozenset(
    r.strip() for r in HIGH_PRIORITY_REASONS.split(",") if r.strip()
)


# --- KB1 thunk-parsing helpers ------------------------------------------------

def _parse_instruct_result(thunk, model_class: type[BaseModel]):
    """Parse an m.instruct(format=Model) result into its Pydantic model."""
    return model_class.model_validate_json(thunk.value)


def _safe_parse_with_fallback(thunk, model_class: type[BaseModel], **fallback_kwargs):
    """Parse with a fallback model on parse failure (KB2 — truncation guard)."""
    try:
        return model_class.model_validate_json(thunk.value)
    except Exception:
        return model_class(**fallback_kwargs)


# --- deterministic helpers ----------------------------------------------------

def current_timestamp() -> str:
    """Current timestamp for the #+DATE header (elem_019, replaces shell `date`)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --- new-activity delta: cutoff normalisation + PR-template stripping ----------

# Section headings in the mellea PR template that are pure scaffolding (checklist /
# attribution / type-routing / footer) — everything from one of these to the next ``##``
# heading (or EOF) is dropped. ``## Description`` and ``## Issue`` are kept.
_TEMPLATE_DROP_SECTIONS = ("testing", "attribution", "adding a new component")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]")
_HEADING_MD_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*$")


def _normalize_cutoff(value: str | None) -> str | None:
    """Normalise a stored cutoff to an ISO-8601 UTC string for date comparison.

    Accepts either an ISO-8601 timestamp (a stored :LAST_SEEN: or a subject's
    ``created_at`` — already UTC, e.g. ``2026-06-16T01:39:21Z``) or the doc-level
    ``#+DATE:`` header in local ``%Y-%m-%d %H:%M`` form, which is converted from local
    time to a UTC ``...Z`` string. Returns ``None`` when ``value`` is empty/unparseable.
    """
    if not value:
        return None
    raw = value.strip()
    # Already an ISO-8601 instant (the common :LAST_SEEN: / created_at case).
    if "T" in raw:
        return raw
    # Doc-level #+DATE header: naive local time -> UTC instant.
    try:
        naive_local = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    aware_local = naive_local.astimezone()  # attaches the system local tz
    return aware_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_pr_template(body: str | None) -> str:
    """Strip GitHub PR/issue template scaffolding, keeping the human-written prose.

    Drops HTML comments, checklist lines (``- [ ]`` / ``- [x]``), and whole scaffolding
    sections (Testing / Attribution / "Adding a new component…") plus the trailing
    ``NOTE:`` footer. Keeps ``## Description`` / ``## Issue`` content. Repos without the
    template are unaffected (no matching sections -> body returned cleaned of comments
    only). Returns ``""`` for an empty body.
    """
    if not body:
        return ""
    text = _HTML_COMMENT_RE.sub("", body.replace("\r\n", "\n").replace("\r", "\n"))
    out: list[str] = []
    dropping = False
    for line in text.split("\n"):
        hm = _HEADING_MD_RE.match(line)
        if hm:
            title = hm.group(2).strip().lower()
            dropping = any(title.startswith(s) for s in _TEMPLATE_DROP_SECTIONS)
            if dropping:
                continue
        if dropping:
            continue
        if _CHECKLIST_RE.match(line):
            continue
        if line.strip().startswith("NOTE:"):
            continue
        out.append(line)
    # Collapse the runs of blank lines left behind by the removals.
    cleaned: list[str] = []
    for line in out:
        if not line.strip() and (not cleaned or not cleaned[-1].strip()):
            continue
        cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip()


def _host_for(notif: dict[str, Any]) -> str:
    """Return the GitHub host a notification belongs to."""
    return notif.get("host", GITHUB_COM_HOST)


def _passes_filter(notif: dict[str, Any], filter_mode: str) -> bool:
    """Apply the user's filter mode to a single notification (elem_006)."""
    stype = notif.get("subject_type", "")
    if filter_mode == "issues":
        return stype == "Issue"
    if filter_mode == "prs":
        return stype == "PullRequest"
    if filter_mode == "review_requests":
        return stype == "PullRequest" and notif.get("reason") == "review_requested"
    return True  # "all"


def _merge_queue_summary(enriched: dict[str, Any]) -> str:
    """Render the merge-queue / auto-merge status line for a PR (REST-only signal).

    ``mergeable_state == "queued"`` means GitHub has placed the PR in the merge
    queue. Otherwise, ``auto_merge`` being set means auto-merge is armed (it will
    enter the queue / merge once checks pass). Falls back to "—" when neither holds.
    """
    if str(enriched.get("mergeable_state") or "") == "queued":
        return "In merge queue"
    if enriched.get("auto_merge"):
        by = str(enriched.get("auto_merge_enabled_by") or "")
        return f"Auto-merge enabled (by {by})" if by else "Auto-merge enabled"
    return "—"


def _pr_state_summary(enriched: dict[str, Any]) -> str:
    """Collapse PR enrichment fields into a single state word for the bucket slot."""
    if enriched.get("inaccessible"):
        return "inaccessible"
    if enriched.get("merged"):
        return "merged"
    if enriched.get("draft"):
        return "draft"
    return str(enriched.get("state") or "n/a")


def _fetch_delta(filter_mode: str) -> list[dict[str, Any]]:
    """Fetch + parse + filter the unread delta from both hosts (Step 3)."""
    delta: list[dict[str, Any]] = []
    for host in GITHUB_HOSTS:
        try:
            raw_items = tools.fetch_notifications(host)
        except tools.GitHubToolError:
            continue  # host unavailable / not authenticated
        for raw in raw_items:
            notif = parse_notification(raw, host)
            if _passes_filter(notif, filter_mode):
                delta.append(notif)
    return delta


def _enrich(
    notif: dict[str, Any],
    logins: dict[str, str],
    existing: dict[str, dict[str, Any]],
    doc_cutoff: str | None,
) -> dict[str, Any]:
    """Enrich one notification with subject + review + latest-comment context (Step 4).

    Also classifies the item as brand-new vs known-with-new-activity using a per-item
    cutoff: the item's stored ``:LAST_SEEN:`` (falling back to the doc-level ``#+DATE:``
    header, ``doc_cutoff``). For a known item with a cutoff, fetches the comments/reviews
    created since that cutoff (the new-activity delta) and attaches them under
    ``new_comments`` / ``new_reviews``; ``is_new`` marks items the inbox has not seen.
    """
    host = _host_for(notif)
    subject_url = notif.get("subject_url", "")
    subject_type = notif.get("subject_type", "")
    enriched = tools.enrich_subject(subject_url, subject_type, host) if subject_url else {}

    user_reviewed = "no"
    latest_review_state = "none"
    is_requested_reviewer = False
    review_summary: dict[str, list[str]] = {
        "approved_by": [],
        "changes_requested_by": [],
        "commented_by": [],
    }
    if subject_type == "PullRequest" and subject_url and not enriched.get("inaccessible"):
        review_summary = tools.fetch_review_summary(subject_url, host)
        login = logins.get(host, "")
        if login:
            own = tools.fetch_review_state(subject_url, host, login=login, exclude=False)
            user_reviewed = "yes" if own and own != "none" else "no"
            # GitHub still listing the user in requested_reviewers means it is actively
            # asking them to review — authoritative regardless of the notification reason
            # (which flips from review_requested to comment once the user comments).
            requested = [r.lower() for r in (enriched.get("requested_reviewers") or [])]
            is_requested_reviewer = login.lower() in requested
            if notif.get("reason") == "author":
                latest_review_state = tools.fetch_review_state(
                    subject_url, host, login=login, exclude=True
                )

    latest_comment: dict[str, Any] = {}
    reason = notif.get("reason", "")
    should_fetch_comment = reason in _HIGH_PRIORITY or not SKIP_LOW_PRIORITY_COMMENT_FETCH
    if should_fetch_comment and notif.get("latest_comment_url"):
        latest_comment = tools.fetch_latest_comment(notif["latest_comment_url"], host)

    html_url = enriched.get("html_url", subject_url)
    prior = existing.get(html_url)
    is_new = prior is None
    # Per-item cutoff: the stored :LAST_SEEN:, else the doc-level #+DATE fallback.
    cutoff = _normalize_cutoff((prior or {}).get("last_seen")) or doc_cutoff

    new_comments: list[dict[str, Any]] = []
    new_reviews: list[dict[str, Any]] = []
    # Only known items with a usable cutoff get a new-activity delta; for brand-new items
    # the full summary already covers everything, so we skip the extra calls.
    if not is_new and cutoff and subject_url and not enriched.get("inaccessible"):
        new_comments = tools.fetch_new_comments(subject_url, host, since=cutoff)
        if subject_type == "PullRequest":
            new_reviews = tools.fetch_new_reviews(subject_url, host, since=cutoff)

    return {
        **notif,
        "enriched": enriched,
        "html_url": html_url,
        "user_reviewed": user_reviewed,
        "latest_review_state": latest_review_state,
        "is_requested_reviewer": is_requested_reviewer,
        "review_summary": review_summary,
        "latest_comment": latest_comment,
        "is_new": is_new,
        "cutoff": cutoff,
        "new_comments": new_comments,
        "new_reviews": new_reviews,
        # Any typed :NOTES: property value from the prior block (None for new items);
        # re-emitted verbatim by render_item_subtree so a note survives a full re-render.
        "notes": (prior or {}).get("notes"),
    }


# --- Org-mode rendering (elem_020/021/022) ------------------------------------

def render_item_subtree(item: dict[str, Any], render: ItemRender, level: int) -> str:
    """Render one item subtree at the given heading depth (Step 6).

    Emits: heading, :PROPERTIES: drawer (:URL: + :HOST: — REQUIRED in every section — plus an
    always-present :NOTES: line, empty by default, that the user can type a note into;
    user-owned free text re-emitted verbatim so a typed note survives this full re-render),
    the raw html_url on its own line (for C-c C-o / link-open access), a metadata table
    (Reviewers row omitted for Issues), the generated summary, the "Why you're seeing this"
    line, and the "Latest activity" line.
    """
    stars = "*" * level
    indent = " " * (level + 1)
    enriched = item.get("enriched", {})
    host = _host_for(item)
    is_pr = item.get("subject_type") == "PullRequest"

    lines = [f"{stars} {item.get('title', '(untitled)')}"]
    lines.append(f"{indent}:PROPERTIES:")
    lines.append(f"{indent}:URL:  {item.get('html_url', '')}")
    lines.append(f"{indent}:HOST: {host}")
    # :LAST_SEEN: is the per-item cutoff for the next run's new-activity delta — the
    # notification's updated_at (the latest activity GitHub reports for this thread).
    last_seen = str(item.get("updated_at") or "")
    if last_seen:
        lines.append(f"{indent}:LAST_SEEN: {last_seen}")
    # :NOTES: is always emitted (empty by default) so the user can jot a note without
    # remembering any syntax — just type after the colon. It's user-owned free text the
    # pipeline never parses; a note the user typed is carried on `item["notes"]` and
    # re-emitted here so it survives a full re-render.
    notes = str(item.get("notes") or "")
    lines.append(f"{indent}:NOTES: {notes}".rstrip())
    lines.append(f"{indent}:END:")
    lines.append(f"{indent}{item.get('html_url', '')}")
    lines.append("")

    # For PRs, collapse draft/merged into the State word (raw .state is just
    # "open"/"closed" and would show a draft PR as "open"); Issues keep raw .state.
    state_value = _pr_state_summary(enriched) if is_pr else str(enriched.get("state", "unknown"))
    rows = [
        ("State", state_value),
        ("Author", str(enriched.get("user", "unknown"))),
    ]
    if is_pr:
        reviewers = enriched.get("requested_reviewers") or []
        rows.append(("Reviewers", ", ".join(reviewers) if reviewers else "—"))
        review_summary = item.get("review_summary") or {}
        approved_by = review_summary.get("approved_by") or []
        # "Reviewed by" = anyone who left a review but hasn't approved (commented
        # or requested changes) — so the user can see a PR was looked at without
        # yet being approved.
        reviewed_by = (review_summary.get("changes_requested_by") or []) + (
            review_summary.get("commented_by") or []
        )
        rows.append(("Approved by", ", ".join(approved_by) if approved_by else "—"))
        rows.append(("Reviewed by", ", ".join(reviewed_by) if reviewed_by else "—"))
        rows.append(("Merge queue", _merge_queue_summary(enriched)))
    labels = enriched.get("labels") or []
    rows.append(("Labels", ", ".join(labels) if labels else "—"))
    rows.append(("Milestone", str(enriched.get("milestone") or "—")))

    width = max(len(k) for k, _ in rows)
    lines.append(f"{indent}| Field{' ' * (width - 5)} | Value |")
    lines.append(f"{indent}|{'-' * (width + 2)}+-------|")
    for key, val in rows:
        lines.append(f"{indent}| {key}{' ' * (width - len(key))} | {val} |")
    lines.append("")

    lines.append(f"{indent}{render.summary}")
    lines.append("")
    lines.append(f"{indent}*Why you're seeing this:* {render.why_seeing}")
    lines.append("")
    lines.append(f"{indent}*Latest activity:* {render.latest_activity}")
    lines.append("")
    return "\n".join(lines)


# Matches a :LAST_SEEN: drawer line so we can advance it in a carried-over block.
_LAST_SEEN_LINE_RE = re.compile(r"^(\s*):LAST_SEEN:\s.*$", re.MULTILINE)
_URL_LINE_RE = re.compile(r"^(\s*):URL:\s.*$", re.MULTILINE)
_BLOCK_HEADING_RE = re.compile(r"^(\*+)(\s+.*)$", re.MULTILINE)
# A :NOTES: property line (with or without a value); used to detect its presence.
_NOTES_LINE_RE = re.compile(r"^(\s*):NOTES:(?:\s.*)?$", re.MULTILINE)
# The :PROPERTIES: drawer's terminating :END: (first :END: at a property indent); we insert
# an empty :NOTES: line just before it so pre-feature carried blocks gain the line, too.
_DRAWER_END_RE = re.compile(r"^(\s*):END:\s*$", re.MULTILINE)

# A stale ``*** Update <timestamp>`` org *heading* (heading line + its indented body) left in
# the doc by a pre-fix run. The heading line is ``<stars> <space> Update <rest-of-line>`` at the
# start of a line; the body is every following line up to the next heading or end of block.
# Deltas are now inline ``*Update <ts>:*`` prose (see render_activity_delta) — this regex only
# matches the broken heading form (note the *space* after the stars), never the inline form,
# which is indented and has ``*Update`` with no space before "Update".
_STALE_UPDATE_HEADING_RE = re.compile(
    r"^\*+[ \t]+Update[ \t]+(?P<ts>.*?)[ \t]*\n(?P<body>(?:(?!\*+[ \t]).*(?:\n|$))*)",
    re.MULTILINE,
)


def _relevel_block(block: str, target_top_level: int) -> str:
    """Shift every heading in a carried block so its top heading sits at target level.

    Items now always sit at level 2 directly under their `*` priority bucket, but a
    carried block may have been written by an older run at a different depth (e.g. level 3
    under the retired FYI `** Pull Requests` grouping). We compute the block's current top
    heading depth and apply the same delta to all headings in the block, clamping at 1 so
    headings never lose all their stars.
    """
    headings = _BLOCK_HEADING_RE.findall(block)
    if not headings:
        return block
    current_top = min(len(stars) for stars, _ in headings)
    shift = target_top_level - current_top
    if shift == 0:
        return block

    def _shift(m: re.Match[str]) -> str:
        new_depth = max(1, len(m.group(1)) + shift)
        return ("*" * new_depth) + m.group(2)

    return _BLOCK_HEADING_RE.sub(_shift, block)


def _normalize_stale_update_headings(block: str, level: int) -> str:
    """Heal a carried-over block written by a pre-fix run that used ``*** Update`` headings.

    Older runs rendered the new-activity delta as a child org heading (``*** Update <ts>``
    followed by the indented prose). Updates must never be a subheading — they belong inline
    under the item, mirroring ``render_activity_delta``. This rewrites each such heading-block
    into a single ``*Update <ts>:* <body>`` line at the item's indent (``level + 1`` spaces),
    collapsing the indented body to one line. The inline form current runs already produce is
    left untouched (it isn't an org heading, so it doesn't match).
    """
    indent = " " * (level + 1)

    def _inline(m: re.Match[str]) -> str:
        ts = m.group("ts").strip()
        body = " ".join(line.strip() for line in m.group("body").splitlines() if line.strip())
        line = f"{indent}*Update {ts}:*"
        if body:
            line += f" {body}"
        return line + "\n"

    return _STALE_UPDATE_HEADING_RE.sub(_inline, block)


def _bump_last_seen(block: str, new_last_seen: str) -> str:
    """Advance (or insert) the :LAST_SEEN: drawer property in a carried-over block.

    If the block already has a :LAST_SEEN: line, rewrite it in place; otherwise insert one
    just after the :URL: line so older items (written before LAST_SEEN tracking) gain the
    property on their first refresh. No-op return when ``new_last_seen`` is empty.
    """
    if not new_last_seen:
        return block
    if _LAST_SEEN_LINE_RE.search(block):
        return _LAST_SEEN_LINE_RE.sub(rf"\g<1>:LAST_SEEN: {new_last_seen}", block, count=1)

    def _insert(m: re.Match[str]) -> str:
        indent = m.group(1)
        return f"{m.group(0)}\n{indent}:LAST_SEEN: {new_last_seen}"

    return _URL_LINE_RE.sub(_insert, block, count=1)


def _ensure_notes_line(block: str) -> str:
    """Add an empty ``:NOTES:`` line to a carried block that predates the property.

    A note the user has already typed is inside the block verbatim, so this is a no-op when
    a ``:NOTES:`` line already exists (with or without a value) — it never touches user text.
    Otherwise it inserts an empty ``:NOTES:`` line just before the :PROPERTIES: drawer's
    closing ``:END:``, so older items gain the always-present notes slot on their first
    refresh (mirroring how ``_bump_last_seen`` back-fills :LAST_SEEN:).
    """
    if _NOTES_LINE_RE.search(block):
        return block

    def _insert(m: re.Match[str]) -> str:
        indent = m.group(1)
        return f"{indent}:NOTES:\n{m.group(0)}"

    return _DRAWER_END_RE.sub(_insert, block, count=1)


def render_activity_delta(
    prev_block: str,
    item: dict[str, Any],
    delta: ActivityDelta,
    timestamp: str,
    level: int,
) -> str:
    """Append a dated new-activity delta to a carried-over item block (Step 5/6).

    Keeps the original item subtree verbatim except for (1) re-leveling its headings so the
    item heading sits at ``level`` (the item may have been re-bucketed to a different depth),
    (2) advancing its :LAST_SEEN: to the notification's updated_at, and (3) back-filling an
    empty :NOTES: line if the carried block predates it (never touches a note the user
    already typed). Then appends the new-activity prose inline under the item heading as an
    ``*Update <timestamp>:*`` line (mirroring ``*Latest activity:*``) — NOT a child heading,
    so it stays part of the parent item rather than splitting off its own org section.
    """
    releveled = _relevel_block(prev_block.rstrip("\n"), target_top_level=level)
    # Heal any stale ``*** Update`` heading a pre-fix run left in the carried block, so the
    # item carries only inline update lines before we append the newest one.
    healed = _normalize_stale_update_headings(releveled, level)
    bumped = _ensure_notes_line(_bump_last_seen(healed, str(item.get("updated_at") or "")))
    indent = " " * (level + 1)
    out = [bumped.rstrip("\n"), "", f"{indent}*Update {timestamp}:* {delta.delta}", ""]
    return "\n".join(out)


def render_inbox_org(
    buckets: dict[str, list[str]],
    timestamp: str,
) -> str:
    """Assemble the full Org-mode inbox document (Step 6).

    ``buckets`` maps "high"/"medium"/"low" to lists of pre-rendered item subtree
    strings. Each is a flat list under its ``*`` priority heading; empty buckets get
    the italic placeholder.
    """
    out = [f"#+TITLE: {ORG_TITLE}", f"#+DATE: {timestamp}", ""]

    for key, heading in (
        ("high", "* High Priority"),
        ("medium", "* Medium Priority"),
        ("low", "* Low Priority"),
    ):
        out.append(heading)
        if buckets.get(key):
            out.extend(buckets[key])
        else:
            out.append(EMPTY_BUCKET_PLACEHOLDER)
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def confirm_inbox_written(inbox_path: str | Path) -> bool:
    """Write-gate (elem_023): inbox file exists and is non-empty before mark-Done."""
    p = Path(inbox_path)
    return p.exists() and p.stat().st_size > 0


# --- main orchestration -------------------------------------------------------

def run_pipeline(user_request: str = "") -> RunSummary:
    """Run one full inbox-update pass. Returns a conversational RunSummary.

    P4 shape: GitHub data is fetched internally via tools; the only user-facing
    parameter is the natural-language request, which carries the optional filter intent.
    """
    inbox_path = Path(INBOX_PATH)

    # Step 1: identify the user on both hosts.
    try:
        logins = tools.fetch_user_logins()
    except tools.GitHubToolError:
        logins = {}

    # Step 2: read the existing inbox (de-dup map keyed by html_url) + the doc-level
    # #+DATE header, used as a fallback cutoff for items that predate :LAST_SEEN:.
    existing = read_existing_inbox(inbox_path)
    doc_cutoff = _normalize_cutoff(read_doc_date(inbox_path))

    # Step 3 (filter): classify the user's request into a filter mode.
    # Each @generative slot defines its own response model — give it its own session (KB5).
    # Classification is a small fixed-label pick, so it runs on the cheaper CLASSIFIER_MODEL_ID.
    with start_session(BACKEND, CLASSIFIER_MODEL_ID, **BACKEND_KWARGS) as m_filter:
        filter_mode = classify_filter_mode(m_filter, user_request=user_request or "")
    filter_mode = str(filter_mode)

    # Step 3 (fetch): get the delta, apply the filter.
    delta = _fetch_delta(filter_mode)

    # Empty-delta early exit (elem_009): no write, no mark Done.
    if not delta:
        carried = len(existing)
        return RunSummary(
            headline="Nothing new on GitHub — your inbox is already up to date.",
            new_count=0,
            refreshed_count=0,
            carried_over_count=carried,
            most_important="No High Priority items changed this run.",
            reminder=(
                f"Items stay in {INBOX_PATH} until you remove them by hand."
            ),
        )

    # Step 4: enrich each delta item (also classifies new vs known-with-new-activity
    # and fetches the new-activity delta for known items).
    enriched_items = [_enrich(n, logins, existing, doc_cutoff) for n in delta]

    # Step 5 (classify): bucket each item. classify_bucket's response model is one
    # schema type, so a single dedicated session is safe for all bucket calls (KB5).
    # Closed/merged issues+PRs and draft PRs are forced to Low Priority deterministically —
    # these are unambiguous and we don't burn a model call (or trust the 3B model) on them.
    # Bucketing is fixed-label classification, so it runs on the cheaper CLASSIFIER_MODEL_ID.
    with start_session(BACKEND, CLASSIFIER_MODEL_ID, **BACKEND_KWARGS) as m_bucket:
        for item in enriched_items:
            enriched = item.get("enriched", {})
            pr_state = _pr_state_summary(enriched)
            if pr_state in ("closed", "merged", "draft"):
                item["bucket"] = "low"
                continue
            # An outstanding review request on a live PR is unambiguously High Priority,
            # whatever the notification reason says — GitHub flips review_requested to
            # comment once the user comments, which would otherwise demote it to Low.
            if item.get("is_requested_reviewer"):
                item["bucket"] = "high"
                continue
            comment_body = str((item.get("latest_comment") or {}).get("body", ""))
            item["bucket"] = str(
                classify_bucket(
                    m_bucket,
                    reason=item.get("reason", ""),
                    pr_state=pr_state,
                    user_reviewed=item.get("user_reviewed", "no"),
                    latest_review_state=item.get("latest_review_state", "none"),
                    latest_comment_text=comment_body,
                )
            )

    # A delta-mode item is one we've seen before that has genuinely new activity since
    # its cutoff — those get an appended dated delta block instead of a full re-render.
    # Known items where we found no new activity fall back to a full re-render (keeps the
    # summary current and avoids dropping an item GitHub still flags as unread).
    def _is_delta_mode(it: dict[str, Any]) -> bool:
        return (not it.get("is_new", True)) and bool(
            it.get("new_comments") or it.get("new_reviews")
        )

    # Step 5 (render): full-summary items use ItemRender; delta-mode items use
    # ActivityDelta. Each is one schema type -> its own session (KB5).
    rendered: dict[str, tuple[dict[str, Any], ItemRender]] = {}
    delta_rendered: dict[str, tuple[dict[str, Any], ActivityDelta]] = {}
    full_items = [it for it in enriched_items if not _is_delta_mode(it)]
    delta_items = [it for it in enriched_items if _is_delta_mode(it)]

    with start_session(BACKEND, MODEL_ID, **BACKEND_KWARGS) as m_render:
        for item in full_items:
            enriched = item.get("enriched", {})
            comment = item.get("latest_comment") or {}
            why = REASON_DISPLAY.get(item.get("reason", ""), "Repo activity")
            # Surface review + merge-queue status to the model alongside the raw
            # PR fields so the summary can mention "approved by X" / "in merge queue".
            details = {
                **enriched,
                "review_summary": item.get("review_summary", {}),
                "merge_queue_status": _merge_queue_summary(enriched)
                if item.get("subject_type") == "PullRequest"
                else "n/a",
            }
            # The PR/issue body, stripped of template scaffolding (mellea uses a fixed
            # template); empty when there's no body. Drop it from `details` so the raw
            # templated body doesn't leak in alongside the cleaned version.
            description = strip_pr_template(enriched.get("body"))
            details.pop("body", None)
            render_thunk = m_render.instruct(
                "Summarise this GitHub {{ subject_type }} for a triage inbox.\n"
                "Title: {{ title }}\n"
                "Repository: {{ repo }}\n"
                "Description (template scaffolding already removed): {{ description }}\n"
                "Subject details (JSON): {{ details }}\n"
                "Latest comment (JSON): {{ comment }}\n"
                "Reason the user is seeing this: {{ why }}\n\n"
                "Write a substantive 3-5 sentence summary that gives the user enough "
                "context to decide what to do without clicking through: what the "
                "issue/PR is about, its current state, and any open questions or "
                "blockers. Base it on the Description when one is given (ignore any "
                "leftover checklist or boilerplate), and use concrete details from the "
                "subject and latest comment (reviewers, labels, milestone, CI/merge "
                "state) rather than restating the title. For PRs, note approval status "
                "(who has approved, from review_summary.approved_by) and whether it is "
                "in the merge queue or has auto-merge enabled (merge_queue_status) when "
                "relevant. Then write a one-line 'why you're seeing this' (use the "
                "reason given), and a one-line latest-activity note (who did what, with "
                "a short quoted excerpt if it clarifies the ask).",
                user_variables={
                    "subject_type": str(item.get("subject_type", "")),
                    "title": str(item.get("title", "")),
                    "repo": str(item.get("repo_full_name", "")),
                    "description": description,
                    "details": str(details),
                    "comment": str(comment),
                    "why": str(why),
                },
                model_options={
                    ModelOption.SYSTEM_PROMPT: PREFIX_TEXT,
                    ModelOption.MAX_NEW_TOKENS: ITEM_SUMMARY_MAX_TOKENS,
                },
                format=ItemRender,
            )
            render = _safe_parse_with_fallback(
                render_thunk,
                ItemRender,
                summary=str(item.get("title", "")),
                why_seeing=why,
                latest_activity=str(item.get("updated_at", "")),
            )
            url = item.get("html_url", "")
            rendered[url] = (item, render)

    # Step 5 (delta render): summarise ONLY the new activity for known items.
    with start_session(BACKEND, MODEL_ID, **BACKEND_KWARGS) as m_delta:
        for item in delta_items:
            url = item.get("html_url", "")
            delta_thunk = m_delta.instruct(
                "Summarise ONLY the new activity on this GitHub {{ subject_type }} "
                "since the user last looked. Do NOT re-summarise the item itself.\n"
                "Title: {{ title }}\n"
                "New comments since last run (JSON, oldest first): {{ comments }}\n"
                "New reviews since last run (JSON, oldest first): {{ reviews }}\n\n"
                "Write 2-4 sentences naming who did what: new reviews and their state "
                "(approved / requested changes / commented), and any notable "
                "back-and-forth in the new comments. Quote a short excerpt only when it "
                "clarifies the ask. If there is genuinely little new, say so briefly.",
                user_variables={
                    "subject_type": str(item.get("subject_type", "")),
                    "title": str(item.get("title", "")),
                    "comments": str(item.get("new_comments") or []),
                    "reviews": str(item.get("new_reviews") or []),
                },
                model_options={
                    ModelOption.SYSTEM_PROMPT: PREFIX_TEXT,
                    ModelOption.MAX_NEW_TOKENS: ITEM_SUMMARY_MAX_TOKENS,
                },
                format=ActivityDelta,
            )
            delta = _safe_parse_with_fallback(
                delta_thunk,
                ActivityDelta,
                delta=str(item.get("updated_at", "")),
            )
            delta_rendered[url] = (item, delta)

    # Step 5 (fold): reconcile by html_url. New items are fully rendered; known items with
    # new activity get a dated delta appended to their carried block; existing-but-not-in-
    # delta items are carried over verbatim.
    touched_urls = set(rendered) | set(delta_rendered)
    new_count = sum(1 for url in rendered if url not in existing)
    refreshed_count = len(delta_rendered) + sum(1 for url in rendered if url in existing)
    carried_urls = [url for url in existing if url not in touched_urls]
    carried_over_count = len(carried_urls)

    buckets: dict[str, list[str]] = {
        "high": [],
        "medium": [],
        "low": [],
    }
    high_priority_titles: list[str] = []

    def _place(item: dict[str, Any], block: str) -> None:
        """Route a rendered block into its priority bucket; record High Priority titles."""
        bucket = item.get("bucket", "low")
        if bucket == "high":
            buckets["high"].append(block)
            high_priority_titles.append(str(item.get("title", "")))
        elif bucket == "medium":
            buckets["medium"].append(block)
        else:  # low — flat list
            buckets["low"].append(block)

    # Item heading depth: every item is a level-2 heading directly under its `*` bucket.
    def _item_level(item: dict[str, Any]) -> int:
        return 2

    # Full-render and delta items together, sorted by latest activity (most recent first).
    timestamp = current_timestamp()
    merged: list[tuple[dict[str, Any], str]] = [
        (item, render_item_subtree(item, render, level=_item_level(item)))
        for item, render in rendered.values()
    ]
    for url, (item, delta) in delta_rendered.items():
        # The delta is appended inline under the item heading (no child heading).
        merged.append(
            (
                item,
                render_activity_delta(
                    existing[url]["block"], item, delta, timestamp, level=_item_level(item)
                ),
            )
        )

    for item, block in sorted(
        merged, key=lambda pair: str(pair[0].get("updated_at", "")), reverse=True
    ):
        _place(item, block)

    # Carried-over items keep their original subtree text verbatim (including any :NOTES:
    # the user typed). Per Step 5 untouched items are not re-classified, so we place them
    # under Low Priority by default rather than re-bucketing from the stored drawer. We heal
    # any stale ``*** Update`` heading a pre-fix run left behind so it can never resurface as
    # a subheading, and back-fill an empty :NOTES: line for blocks that predate it.
    for url in carried_urls:
        healed = _normalize_stale_update_headings(existing[url]["block"], level=_item_level({}))
        buckets["low"].append(_ensure_notes_line(healed))

    # Step 6: write the doc (BEFORE marking Done — the irreversible commit point).
    doc = render_inbox_org(buckets, current_timestamp())
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text(doc, encoding="utf-8")

    # Step 7: confirm the write, then mark each folded thread Done (per-thread DELETE).
    if confirm_inbox_written(inbox_path):
        for item in enriched_items:
            thread_id = item.get("id", "")
            if thread_id:
                try:
                    tools.mark_thread_done(thread_id, _host_for(item))
                except tools.GitHubToolError:
                    pass  # leave the thread in the inbox; safe to retry next run

    # Step 8: conversational summary. RunSummary is its own schema -> own session (KB5).
    most_important = (
        "; ".join(high_priority_titles[:3])
        if high_priority_titles
        else "No High Priority items this run."
    )
    with start_session(BACKEND, MODEL_ID, **BACKEND_KWARGS) as m_summary:
        summary_thunk = m_summary.instruct(
            "Write a short, friendly summary of this GitHub inbox update.\n"
            "New items added: {{ new }}\n"
            "Existing items updated with new activity (a dated delta was appended): {{ refreshed }}\n"
            "Items carried over untouched: {{ carried }}\n"
            "Most important High Priority item(s): {{ important }}\n\n"
            "Include a one-line reminder that items stay in the inbox until removed by hand.",
            user_variables={
                "new": str(new_count),
                "refreshed": str(refreshed_count),
                "carried": str(carried_over_count),
                "important": most_important,
            },
            model_options={
                ModelOption.SYSTEM_PROMPT: PREFIX_TEXT,
                ModelOption.MAX_NEW_TOKENS: RUN_SUMMARY_MAX_TOKENS,
            },
            format=RunSummary,
        )
    return _safe_parse_with_fallback(
        summary_thunk,
        RunSummary,
        headline="GitHub inbox updated.",
        new_count=new_count,
        refreshed_count=refreshed_count,
        carried_over_count=carried_over_count,
        most_important=most_important,
        reminder=f"Items stay in {INBOX_PATH} until you remove them by hand.",
    )
