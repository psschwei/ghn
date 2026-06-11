"""CLI entry point for the GitHub notifications inbox skill.

Synchronous one-shot: gather the user's request, run the pipeline, print the summary.
GitHub data is fetched internally by the pipeline via the authenticated ``gh`` CLI
(P4 — tools provide input), so the only user-facing input is the natural-language
request, which carries the optional filter intent ("only PRs", "review requests", ...).
"""

from __future__ import annotations

import argparse
import sys

from .pipeline import run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ghn",
        description=(
            "Fold the unread GitHub notification delta into your inbox doc "
            "(~/org/github.org by default; override with GITHUB_INBOX_PATH), "
            "then mark those threads Done."
        ),
    )
    parser.add_argument(
        "request",
        nargs="*",
        help=(
            "Natural-language request, e.g. 'update my notifications', 'only PRs', "
            "'review requests'. Omit for a full update with no filter."
        ),
    )
    args = parser.parse_args()

    user_request = " ".join(args.request).strip()

    summary = run_pipeline(user_request=user_request)

    print(summary.headline)
    print(
        f"  New: {summary.new_count}  "
        f"Refreshed: {summary.refreshed_count}  "
        f"Carried over: {summary.carried_over_count}"
    )
    print(f"  Most important: {summary.most_important}")
    print(f"  {summary.reminder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
