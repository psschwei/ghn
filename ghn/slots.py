"""@generative slots for the GitHub notifications inbox pipeline.

Two CLASSIFY slots requiring natural-language judgment:
  * classify_filter_mode (elem_006) — interpret the user's request into a filter mode.
  * classify_bucket (elem_017) — decide which inbox bucket an enriched item belongs in.

Per KB6, ``m``/``context`` and other reserved names are never declared as parameters;
``m`` is passed as the first positional argument at call time in ``pipeline.py``.
Each slot body is ``...`` and every docstring sets ``result`` explicitly.
"""

from __future__ import annotations

from typing import Literal

from mellea import generative


@generative
def classify_filter_mode(user_request: str) -> Literal["all", "issues", "prs", "review_requests"]:
    """Classify the user's GitHub notification request into a single filter mode.

    Set `result` to one of:
      - "issues" if the user asked for only issues (e.g. "only issues", "just issues").
      - "prs" if the user asked for only pull requests (e.g. "only PRs", "just PRs").
      - "review_requests" if the user asked specifically for PRs awaiting their review
        (e.g. "review requests", "PRs I need to review").
      - "all" if the user did not restrict the set (any general "update my notifications"
        / "check my github" / "what needs my attention" request, or an empty request).

    When the request is ambiguous or empty, set `result` to "all".
    """
    ...


@generative
def classify_bucket(
    reason: str,
    subject_type: str,
    pr_state: str,
    user_reviewed: str,
    latest_review_state: str,
    latest_comment_text: str,
) -> Literal["action_required", "should_check", "fyi"]:
    """Classify a single enriched notification into one inbox bucket.

    Inputs describe the notification: `reason` is the GitHub notification reason
    (assign, review_requested, mention, author, comment, team_mention, subscribed,
    state_change, ci_activity); `subject_type` is Issue or PullRequest; `pr_state`
    summarises the PR (e.g. "open", "draft", "closed", "merged", or "n/a" for issues);
    `user_reviewed` is "yes"/"no" for whether the user already submitted a review;
    `latest_review_state` is the latest review state from others (e.g. "APPROVED",
    "CHANGES_REQUESTED", or "none"); `latest_comment_text` is the most recent comment body
    (may be empty).

    Set `result` to one of:
      - "action_required" if ANY of:
          * reason is "review_requested" AND user_reviewed is "no" AND the PR is not
            draft, not closed, and not merged;
          * reason is "assign";
          * reason is "mention" AND the latest comment contains a question or request
            directed at the user;
          * reason is "author" AND the latest comment is a question or a review requesting
            changes on the user's PR — but NOT if latest_review_state is "APPROVED".
      - "should_check" if:
          * reason is "author" with new activity (comments, approvals);
          * reason is "comment" and the conversation is ongoing;
          * reason is "mention" but it is informational (not a direct question);
          * reason is "team_mention";
          * reason is "ci_activity" on the user's own PR and CI is failing.
      - "fyi" for everything else (subscribed repo activity, state changes on watched things).

    Any closed or merged issue/PR, and any draft PR, always belongs in "fyi"
    regardless of reason — there is no live action to take on it.

    An approved PR the user authored belongs in "should_check", never "action_required".
    """
    ...
