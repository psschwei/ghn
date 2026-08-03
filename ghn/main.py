"""CLI entry point for the GitHub notifications inbox skill.

Synchronous one-shot: gather the user's request, run the pipeline, print the summary.
GitHub data is fetched internally by the pipeline via the authenticated ``gh`` CLI
(P4 — tools provide input), so the only user-facing input is the natural-language
request, which carries the optional filter intent ("only PRs", "review requests", ...).
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .llama_server import LlamaServerError, spawned_llama_server
from .pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
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
    # `ghn eval [...]` is a dev subcommand (offline pipeline eval), not a notification
    # request — dispatch it before argparse so its own flags (--update, --filter) don't
    # collide with the free-form request parser, and so `ghn "only PRs"` is untouched.
    if argv and argv[0] == "eval":
        from .eval import run_eval

        return run_eval(argv[1:])

    # `ghn clean` is likewise a subcommand, not a request: it prunes merged/closed/draft items
    # from the inbox doc (offline, no fetch, no model). Dispatched before argparse so the
    # free-form request parser is untouched.
    if argv and argv[0] == "clean":
        from .clean import run_clean

        return run_clean(argv[1:])

    args = parser.parse_args(argv)

    user_request = " ".join(args.request).strip()

    # When GHN_LLAMA_SPAWN is on, stand up a local llama-server for the duration of this
    # run and point the pipeline at its OpenAI-compatible endpoint; the context manager
    # tears the process down on exit (including on error), so a run never orphans a server.
    if config.LLAMA_SPAWN:
        try:
            with spawned_llama_server() as base_url:
                summary = run_pipeline(user_request=user_request, base_url=base_url)
        except LlamaServerError as exc:
            print(f"ghn: {exc}", file=sys.stderr)
            return 1
    else:
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
