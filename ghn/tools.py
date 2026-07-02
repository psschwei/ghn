"""C6 tool implementations — thin, governed wrappers around the ``gh`` CLI.

Disposition: real_impl (dependency_plan.json dep_002, dep_004, dep_005, dep_006).

All GitHub access goes through the authenticated ``gh`` binary. We never embed
credentials: ``gh`` reads its own token from the environment / keychain. Every host
passed in is validated against ``ALLOWED_HOSTS`` and every HTTP method against
``ALLOWED_METHODS`` before a subprocess is launched, so the forbidden bulk
``PUT /notifications -f read=true`` mark-all-read call (elem_025) is structurally
unreachable from this module.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .config import GITHUB_HOSTS

# Command allowlist — only these hosts and HTTP methods may ever be invoked.
# Hosts come from config (github.com plus a configured GitHub Enterprise host, if any),
# so the enterprise host is reachable only when the user has opted in.
ALLOWED_HOSTS: tuple[str, ...] = GITHUB_HOSTS
ALLOWED_METHODS: tuple[str, ...] = ("GET", "DELETE")
GH_TIMEOUT: int = 30


class GitHubToolError(RuntimeError):
    """Raised when a gh CLI invocation fails or is disallowed."""


def _validate_host(host: str) -> None:
    if host not in ALLOWED_HOSTS:
        raise GitHubToolError(f"Host {host!r} not in allowlist {ALLOWED_HOSTS}")


def _gh(
    args: list[str],
    *,
    host: str = "github.com",
    method: str = "GET",
) -> str:
    """Run a single ``gh`` CLI command and return its stdout.

    ``args`` is the gh sub-command (e.g. ``["api", "/notifications", "--paginate"]``).
    ``host`` selects the GitHub host; for any host other than github.com the
    ``--hostname`` flag is appended. ``method`` is validated against the allowlist —
    only GET and DELETE are permitted, which makes the forbidden bulk mark-all-read
    PUT call impossible to construct here.
    """
    _validate_host(host)
    if method not in ALLOWED_METHODS:
        raise GitHubToolError(f"HTTP method {method!r} not in allowlist {ALLOWED_METHODS}")

    cmd = ["gh", *args]
    if method == "DELETE":
        cmd += ["--method", "DELETE"]
    if host != "github.com":
        cmd += ["--hostname", host]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except FileNotFoundError as exc:  # gh not installed
        raise GitHubToolError("gh CLI not found on PATH — install GitHub CLI") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubToolError(f"gh command timed out after {GH_TIMEOUT}s: {' '.join(cmd)}") from exc

    if proc.returncode != 0:
        raise GitHubToolError(f"gh command failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _gh_json(args: list[str], *, host: str = "github.com") -> Any:
    """Run a gh api command and parse its stdout as JSON (object or array)."""
    out = _gh(args, host=host, method="GET").strip()
    if not out:
        return None
    return json.loads(out)


def _gh_json_lines(args: list[str], *, host: str = "github.com") -> list[Any]:
    """Run a gh api command whose --jq emits one JSON value per line (e.g. ``.[]``)."""
    out = _gh(args, host=host, method="GET")
    items: list[Any] = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


# --- elem_004: identify the user on each configured host ----------------------

def fetch_user_logins() -> dict[str, str]:
    """Fetch the authenticated user's login from each available GitHub host.

    Returns a mapping host -> login. A host that errors (e.g. not authenticated)
    is simply omitted, so a user with only github.com access still works.
    """
    logins: dict[str, str] = {}
    for host in ALLOWED_HOSTS:
        try:
            login = _gh(["api", "/user", "--jq", ".login"], host=host).strip()
        except GitHubToolError:
            continue
        if login:
            logins[host] = login
    return logins


# --- elem_007: fetch the unread-notification delta ----------------------------

def fetch_notifications(host: str) -> list[dict[str, Any]]:
    """Fetch unread (delta) notifications from a single host with pagination."""
    return _gh_json_lines(
        ["api", "/notifications", "--paginate", "--jq", ".[]"],
        host=host,
    )


# --- elem_011/012/014: enrich a notification's subject ------------------------

_PR_JQ = (
    "{html_url: .html_url, state: .state, merged: .merged, draft: .draft, "
    "user: .user.login, requested_reviewers: [.requested_reviewers[].login], "
    "assignees: [.assignees[].login], labels: [.labels[].name], "
    "milestone: .milestone.title, review_comments: .review_comments, "
    "comments: .comments, mergeable_state: .mergeable_state, "
    "auto_merge: (.auto_merge != null), "
    "auto_merge_enabled_by: .auto_merge.enabled_by.login, "
    "body: .body, created_at: .created_at}"
)

_ISSUE_JQ = (
    "{html_url: .html_url, state: .state, user: .user.login, "
    "assignees: [.assignees[].login], labels: [.labels[].name], "
    "milestone: .milestone.title, comments: .comments, "
    "body: .body, created_at: .created_at}"
)

# Release and Discussion subjects have no assignees/milestone, so the issue jq
# (which does ``[.assignees[].login]``) errors with "cannot iterate over: null".
# These minimal projections pull only fields those endpoints actually return — most
# importantly ``html_url``, which points at the github.com web page (releases/tag/…,
# discussions/…) rather than the api.github.com subject URL from the notification.
_RELEASE_JQ = (
    "{html_url: .html_url, state: (if .draft then \"draft\" "
    "elif .prerelease then \"prerelease\" else \"published\" end), "
    "user: .author.login, body: .body, created_at: .created_at}"
)

_DISCUSSION_JQ = (
    "{html_url: .html_url, state: .state, user: .user.login, "
    "labels: [(.labels // [])[].name], body: .body, created_at: .created_at}"
)


def enrich_pull_request(subject_url: str, host: str) -> dict[str, Any]:
    """Fetch the projected PR fields for a notification subject (elem_012)."""
    return _gh_json(["api", subject_url, "--jq", _PR_JQ], host=host) or {}


def enrich_issue(subject_url: str, host: str) -> dict[str, Any]:
    """Fetch the projected Issue fields for a notification subject (elem_014)."""
    return _gh_json(["api", subject_url, "--jq", _ISSUE_JQ], host=host) or {}


def enrich_release(subject_url: str, host: str) -> dict[str, Any]:
    """Fetch the projected Release fields for a notification subject.

    The subject URL is ``repos/{o}/{r}/releases/{id}``; the response carries the
    ``html_url`` of the release page (``…/releases/tag/{tag}``).
    """
    return _gh_json(["api", subject_url, "--jq", _RELEASE_JQ], host=host) or {}


def enrich_discussion(subject_url: str, host: str) -> dict[str, Any]:
    """Fetch the projected Discussion fields for a notification subject.

    The subject URL is ``repos/{o}/{r}/discussions/{number}``; the response carries
    the ``html_url`` of the discussion page (``…/discussions/{number}``).
    """
    return _gh_json(["api", subject_url, "--jq", _DISCUSSION_JQ], host=host) or {}


def enrich_subject(subject_url: str, subject_type: str, host: str) -> dict[str, Any]:
    """Dispatch enrichment by subject type (elem_011).

    Returns ``{"inaccessible": True}`` if the subject URL is unreachable (e.g. 404
    because the repo was deleted or access was lost) so the caller can render it as
    "inaccessible" and continue (Privacy note, SKILL.md §Important notes).
    """
    try:
        if subject_type == "PullRequest":
            return enrich_pull_request(subject_url, host)
        if subject_type == "Release":
            return enrich_release(subject_url, host)
        if subject_type == "Discussion":
            return enrich_discussion(subject_url, host)
        return enrich_issue(subject_url, host)
    except (GitHubToolError, json.JSONDecodeError):
        return {"inaccessible": True}


# --- elem_013: review-state enrichment ----------------------------------------

def fetch_review_state(subject_url: str, host: str, *, login: str, exclude: bool) -> str:
    """Fetch the latest review state for a PR (elem_013).

    When ``exclude`` is False, returns the user's own latest review state (filtering
    reviews to ``login``) — used to decide whether the user still needs to review.
    When ``exclude`` is True, returns the latest review state from anyone *other than*
    ``login`` (the PR author) — an "APPROVED" here means an authored PR needs no
    further action. Returns "none" when there is no matching review or on error.
    """
    op = "!=" if exclude else "=="
    jq = f'[.[] | select(.user.login {op} "{login}") | .state] | last'
    try:
        state = _gh_json(["api", f"{subject_url}/reviews", "--jq", jq], host=host) or ""
    except (GitHubToolError, json.JSONDecodeError):
        return "none"
    return str(state) if state else "none"


# --- review summary: who approved / reviewed (additive to fetch_review_state) -

def fetch_review_summary(subject_url: str, host: str) -> dict[str, list[str]]:
    """Summarise a PR's reviews by their authors' latest review state.

    Hits ``{subject_url}/reviews`` and collapses each reviewer's review history to
    their *most recent* review (re-reviews: the last one wins), then groups the
    logins by terminal state:

      - ``approved_by``           — reviewers whose latest review is APPROVED
      - ``changes_requested_by``  — reviewers whose latest review is CHANGES_REQUESTED
      - ``commented_by``          — reviewers who left a review comment but neither
                                    approved nor blocked (state COMMENTED)

    This lets the summary distinguish "approved by X" from "X looked but hasn't
    approved". Returns empty lists on error so callers can render "—" and continue.
    """
    jq = (
        "[ group_by(.user.login)[] | (sort_by(.submitted_at) | last) ] | {"
        'approved_by: [.[] | select(.state == "APPROVED") | .user.login], '
        'changes_requested_by: [.[] | select(.state == "CHANGES_REQUESTED") | .user.login], '
        'commented_by: [.[] | select(.state == "COMMENTED") | .user.login]'
        "}"
    )
    empty: dict[str, list[str]] = {
        "approved_by": [],
        "changes_requested_by": [],
        "commented_by": [],
    }
    try:
        result = _gh_json(["api", f"{subject_url}/reviews", "--jq", jq], host=host)
    except (GitHubToolError, json.JSONDecodeError):
        return empty
    return result if isinstance(result, dict) else empty


# --- elem_015: latest comment -------------------------------------------------

def fetch_latest_comment(latest_comment_url: str, host: str) -> dict[str, Any]:
    """Fetch the latest comment (author, body, created_at) for a notification (elem_015).

    Returns an empty dict if there is no comment URL or the fetch fails.
    """
    if not latest_comment_url:
        return {}
    jq = "{author: .user.login, body: .body, created_at: .created_at}"
    try:
        return _gh_json(["api", latest_comment_url, "--jq", jq], host=host) or {}
    except (GitHubToolError, json.JSONDecodeError):
        return {}


# --- new-activity delta: comments + reviews since the per-item cutoff ----------

def _issue_comments_url(subject_url: str) -> str:
    """Derive the issue-comments collection URL from a notification subject URL.

    PR subjects are ``…/pulls/{n}``; their conversation comments live under the
    *issues* path (``…/issues/{n}/comments``). Issue subjects are ``…/issues/{n}``.
    """
    base = subject_url.replace("/pulls/", "/issues/")
    return f"{base}/comments"


def fetch_new_comments(subject_url: str, host: str, *, since: str) -> list[dict[str, Any]]:
    """Fetch conversation comments created after ``since`` (ISO-8601 UTC) (new-activity delta).

    Uses the ``?since=`` query param so the date filtering happens server-side. Works for
    both PRs and Issues (PR conversation comments live on the issues endpoint). Returns
    ``[{author, body, created_at}]`` ordered oldest-first, or ``[]`` on error / no subject.
    """
    if not subject_url or not since:
        return []
    url = f"{_issue_comments_url(subject_url)}?since={since}"
    jq = "[.[] | {author: .user.login, body: .body, created_at: .created_at}]"
    try:
        result = _gh_json(["api", url, "--paginate", "--jq", jq], host=host)
    except (GitHubToolError, json.JSONDecodeError):
        return []
    return result if isinstance(result, list) else []


def fetch_new_reviews(subject_url: str, host: str, *, since: str) -> list[dict[str, Any]]:
    """Fetch PR reviews submitted after ``since`` (ISO-8601 UTC) (new-activity delta).

    The reviews endpoint has no ``since`` param, so we project each review and filter by
    ``submitted_at`` in jq. Returns ``[{author, state, submitted_at, body}]`` ordered
    oldest-first, or ``[]`` on error / no subject. Issues have no reviews -> ``[]``.
    """
    if not subject_url or not since:
        return []
    jq = (
        f'[.[] | select(.submitted_at != null and .submitted_at > "{since}") '
        "| {author: .user.login, state: .state, submitted_at: .submitted_at, body: .body}]"
    )
    try:
        result = _gh_json(
            ["api", f"{subject_url}/reviews", "--paginate", "--jq", jq], host=host
        )
    except (GitHubToolError, json.JSONDecodeError):
        return []
    return result if isinstance(result, list) else []


# --- elem_024/025: mark a thread Done (destructive) ---------------------------

def mark_thread_done(thread_id: str, host: str) -> None:
    """Mark a single notification thread Done via per-thread DELETE (elem_024).

    Uses ``DELETE /notifications/threads/{id}`` — which removes the thread from the
    GitHub inbox — never ``PATCH`` (read-only) and never the forbidden bulk
    ``PUT /notifications -f read=true`` mark-all-read call (elem_025). The method
    allowlist in ``_gh`` enforces that only DELETE is constructible here.
    """
    _gh(
        ["api", f"/notifications/threads/{thread_id}"],
        host=host,
        method="DELETE",
    )
