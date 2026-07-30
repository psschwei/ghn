"""Offline eval harness for the notifications pipeline (plan item 2.16).

There is no live GitHub and no model in an eval run. Instead a scenario fixture supplies
the two things ``run_pipeline`` reaches outside itself for — the network (every call goes
through the ``ghn.tools`` module) and the model (two ``@generative`` slots plus the two
``m.instruct(format=…)`` render calls created via ``start_session``) — and the harness
diffs the Org document the *real* pipeline writes against a checked-in golden.

What that leaves running for real is the entire deterministic core: the priority decision
(``is_assignee`` / ``is_requested_reviewer`` beating the draft demotion, closed/merged
forced low), the flat-list ordering, the fold / dedup by ``html_url``, ``flatten_prose``
sanitisation, the metadata table, priority tags, carried-item decay, and the backup +
write-gate. Only genuine model *prose* and the one fuzzy bucket call are canned. So this
locks machinery, not model quality — and as 2.11 moves priority off the classifier into
deterministic Python, the eval will exercise that real scorer instead of the stub.

Run with ``ghn eval`` (compare) or ``ghn eval --update`` (regenerate goldens).
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from . import pipeline, tools
from .config import GITHUB_COM_HOST
from .loader import parse_notification
from .schemas import ActivityDelta, ItemRender

# Repo layout: ghn/eval.py -> repo root -> tests/{fixtures,baseline}. Eval is a dev command
# run from the checkout; the tests tree is not part of the installed wheel, so an installed
# ``ghn eval`` reports "no fixtures" rather than silently finding nothing.
_TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"
FIXTURES_DIR = _TESTS_DIR / "fixtures"
BASELINE_DIR = _TESTS_DIR / "baseline"

# The reason set for which the real pipeline fetches a latest comment (config mirror) is not
# needed here: the fake tools just return whatever the fixture provides for any call.


# --- deterministic stand-ins for the two @generative slots --------------------

def _fake_filter_mode(filter_mode: str):
    """Stub for ``classify_filter_mode`` — returns the fixture's declared mode verbatim."""

    def _call(_m, *, user_request: str = "") -> str:  # signature mirrors the real slot
        return filter_mode

    return _call


def _fake_bucket(_m, *, reason, pr_state, user_reviewed, latest_review_state, latest_comment_text) -> str:
    """Deterministic stand-in for ``classify_bucket``.

    A faithful, testable transcription of the slot spec in ``slots.py`` — enough to give the
    golden a stable bucket without a model, and a preview of the deterministic scorer 2.11
    will install in its place. Direct involvement (assignee / requested reviewer), closed,
    merged, and draft-with-no-involvement are all decided upstream in ``run_pipeline`` before
    this is ever called, so this only sees the genuinely fuzzy middle.
    """
    if pr_state in ("closed", "merged"):
        return "low"
    if reason == "review_requested" and user_reviewed == "no" and pr_state not in ("draft", "closed", "merged"):
        return "high"
    if reason == "assign":
        return "high"
    if reason == "mention":
        return "high" if "?" in (latest_comment_text or "") else "medium"
    if reason == "author":
        if latest_review_state == "APPROVED":
            return "medium"
        if latest_review_state == "CHANGES_REQUESTED" or "?" in (latest_comment_text or ""):
            return "high"
        return "medium"
    if reason in ("comment", "team_mention", "ci_activity"):
        return "medium"
    return "low"


# --- fake model session -------------------------------------------------------

class _FakeThunk:
    """Stands in for a mellea instruct result: only ``.value`` (JSON text) is read."""

    def __init__(self, value: str) -> None:
        self.value = value


class _FakeSession:
    """Context-manager stand-in for a ``start_session`` handle.

    ``instruct`` dispatches on the requested ``format`` and returns canned JSON: an
    ``ItemRender`` for full items, an ``ActivityDelta`` for delta items. Prose is keyed by the
    item ``title`` threaded through ``user_variables`` so a fixture can feed markdown /
    column-0 leaks to a specific item and lock the sanitised golden.
    """

    def __init__(self, prose_by_title: dict[str, dict[str, str]], delta_by_title: dict[str, str]) -> None:
        self._prose = prose_by_title
        self._delta = delta_by_title

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def instruct(self, _prompt, *, user_variables=None, model_options=None, format=None, **_kw):
        title = str((user_variables or {}).get("title", ""))
        if format is ActivityDelta:
            delta = self._delta.get(title, f"New activity on {title}.")
            return _FakeThunk(ActivityDelta(delta=delta).model_dump_json())
        canned = self._prose.get(title, {})
        summary = canned.get("summary", f"Automated summary for {title}.")
        latest = canned.get("latest_activity", "")
        return _FakeThunk(ItemRender(summary=summary, latest_activity=latest).model_dump_json())


# --- fake tools ---------------------------------------------------------------

def _build_fake_tools(scenario: dict[str, Any]) -> tuple[SimpleNamespace, set[str]]:
    """Build a stand-in for the ``ghn.tools`` module from a scenario.

    Everything is keyed off the raw notifications and their attached fixture data:
    ``enrich`` by subject URL, ``latest_comment`` by comment URL, review data by subject URL.
    Missing data defaults to the same empties the real ``tools`` functions return on error, so
    a fixture only needs to populate the fields that affect its golden. Returns the fake plus
    the set that records every thread the pipeline marks Done.
    """
    logins: dict[str, str] = scenario.get("logins", {GITHUB_COM_HOST: "octdeveloper"})
    notifs: list[dict[str, Any]] = scenario.get("notifications", [])

    raws_by_host: dict[str, list[dict[str, Any]]] = {}
    enrich_by_url: dict[str, dict[str, Any]] = {}
    rsum_by_url: dict[str, dict[str, list[str]]] = {}
    state_by_url: dict[str, dict[str, str]] = {}
    comment_by_curl: dict[str, dict[str, Any]] = {}
    newc_by_url: dict[str, list[dict[str, Any]]] = {}
    newr_by_url: dict[str, list[dict[str, Any]]] = {}

    for n in notifs:
        host = n.get("host", GITHUB_COM_HOST)
        raw = n["raw"]
        raws_by_host.setdefault(host, []).append(raw)
        subject = raw.get("subject") or {}
        surl = subject.get("url", "")
        curl = subject.get("latest_comment_url") or ""
        if "enrich" in n:
            enrich_by_url[surl] = n["enrich"]
        if "review_summary" in n:
            rsum_by_url[surl] = n["review_summary"]
        if "review_state" in n:
            state_by_url[surl] = n["review_state"]
        if "latest_comment" in n and curl:
            comment_by_curl[curl] = n["latest_comment"]
        if "new_comments" in n:
            newc_by_url[surl] = n["new_comments"]
        if "new_reviews" in n:
            newr_by_url[surl] = n["new_reviews"]

    _empty_rsum = {"approved_by": [], "changes_requested_by": [], "commented_by": []}
    done_ids: set[str] = set()

    fake = SimpleNamespace(
        GitHubToolError=tools.GitHubToolError,
        fetch_user_logins=lambda: dict(logins),
        fetch_notifications=lambda host: list(raws_by_host.get(host, [])),
        enrich_subject=lambda subject_url, subject_type, host: dict(enrich_by_url.get(subject_url, {})),
        fetch_review_summary=lambda subject_url, host: dict(rsum_by_url.get(subject_url, _empty_rsum)),
        fetch_review_state=lambda subject_url, host, *, login, exclude: state_by_url.get(
            subject_url, {}
        ).get("others" if exclude else "own", "none"),
        fetch_latest_comment=lambda url, host: dict(comment_by_curl.get(url, {})),
        fetch_new_comments=lambda subject_url, host, *, since: list(newc_by_url.get(subject_url, [])),
        fetch_new_reviews=lambda subject_url, host, *, since: list(newr_by_url.get(subject_url, [])),
        mark_thread_done=lambda thread_id, host: done_ids.add(thread_id),
    )
    return fake, done_ids


# --- one scenario -------------------------------------------------------------

def _expected_done(scenario: dict[str, Any], filter_mode: str) -> set[str]:
    """The thread ids the pipeline should mark Done: every notification passing the filter.

    Empty when the filter admits nothing — the empty-delta path returns before any write and
    marks nothing. Uses the real ``_passes_filter`` so it tracks the pipeline's own rule.
    """
    done: set[str] = set()
    for n in scenario.get("notifications", []):
        host = n.get("host", GITHUB_COM_HOST)
        notif = parse_notification(n["raw"], host)
        if pipeline._passes_filter(notif, filter_mode):
            done.add(notif.get("id", ""))
    return done - {""}


def run_scenario(fixture_path: Path, *, update: bool) -> tuple[bool, str]:
    """Run one fixture through the real pipeline with fakes installed.

    Returns ``(ok, report)``. In update mode the golden is (re)written and ``ok`` is True.
    In compare mode ``ok`` is the doc-matches-golden result and ``report`` is a unified diff
    (or a mark-Done mismatch note) on failure.
    """
    scenario = json.loads(fixture_path.read_text(encoding="utf-8"))
    name = fixture_path.stem
    now = scenario.get("now", "2026-07-30 09:00")
    filter_mode = scenario.get("filter_mode", "all")

    prose_by_title = {n["raw"]["subject"]["title"]: n["prose"] for n in scenario.get("notifications", []) if "prose" in n}
    delta_by_title = {n["raw"]["subject"]["title"]: n["delta_prose"] for n in scenario.get("notifications", []) if "delta_prose" in n}
    fake_tools, done_ids = _build_fake_tools(scenario)

    with tempfile.TemporaryDirectory() as td:
        inbox = Path(td) / "github.org"
        if scenario.get("existing_inbox"):
            inbox.write_text(scenario["existing_inbox"], encoding="utf-8")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(pipeline, "tools", fake_tools))
            stack.enter_context(mock.patch.object(pipeline, "start_session", lambda *a, **k: _FakeSession(prose_by_title, delta_by_title)))
            stack.enter_context(mock.patch.object(pipeline, "classify_filter_mode", _fake_filter_mode(filter_mode)))
            stack.enter_context(mock.patch.object(pipeline, "classify_bucket", _fake_bucket))
            stack.enter_context(mock.patch.object(pipeline, "INBOX_PATH", str(inbox)))
            stack.enter_context(mock.patch.object(pipeline, "current_timestamp", lambda: now))
            pipeline.run_pipeline(user_request="")

        produced = inbox.read_text(encoding="utf-8") if inbox.exists() else ""

    golden_path = BASELINE_DIR / f"{name}.org"
    if update:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(produced, encoding="utf-8")
        return True, f"wrote {golden_path.relative_to(_TESTS_DIR.parent)}"

    if not golden_path.exists():
        return False, f"no golden at {golden_path} — run `ghn eval --update` first"

    golden = golden_path.read_text(encoding="utf-8")
    expected = _expected_done(scenario, filter_mode)
    if done_ids != expected:
        return False, f"mark-Done mismatch: expected {sorted(expected)}, marked {sorted(done_ids)}"

    if produced == golden:
        return True, "ok"
    diff = "".join(
        difflib.unified_diff(
            golden.splitlines(keepends=True),
            produced.splitlines(keepends=True),
            fromfile=f"golden/{name}.org",
            tofile=f"produced/{name}.org",
        )
    )
    return False, diff


# --- CLI ----------------------------------------------------------------------

def run_eval(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ghn eval", description="Run the offline pipeline eval.")
    parser.add_argument("--update", action="store_true", help="Regenerate the golden .org files instead of comparing.")
    parser.add_argument("--filter", dest="name_filter", default=None, help="Only run scenarios whose file stem contains this substring.")
    args = parser.parse_args(argv)

    if not FIXTURES_DIR.exists():
        print(f"ghn eval: no fixtures directory at {FIXTURES_DIR}", file=sys.stderr)
        return 1

    fixtures = sorted(FIXTURES_DIR.glob("*.json"))
    if args.name_filter:
        fixtures = [f for f in fixtures if args.name_filter in f.stem]
    if not fixtures:
        print("ghn eval: no matching fixtures", file=sys.stderr)
        return 1

    failures = 0
    for fx in fixtures:
        ok, report = run_scenario(fx, update=args.update)
        if ok:
            print(f"  PASS  {fx.stem}  {report if args.update else ''}".rstrip())
        else:
            failures += 1
            print(f"  FAIL  {fx.stem}")
            print("\n".join("        " + line for line in report.splitlines()))

    total = len(fixtures)
    verb = "updated" if args.update else "checked"
    print(f"\nghn eval: {total} {verb}, {failures} failed")
    return 1 if failures else 0
