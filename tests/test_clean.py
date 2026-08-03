"""Offline unit tests for ``ghn clean`` (loader.strip_finished_items).

Stdlib ``unittest`` only — no pytest, no new deps, matching the eval harness ethos. Run with
``uv run python -m unittest tests.test_clean`` (or under pytest if it's installed).
"""

from __future__ import annotations

import unittest

from ghn.clean import REMOVE_STATES
from ghn.loader import read_existing_inbox, strip_finished_items  # noqa: F401 (re-parse check)


def _item(url: str, state: str, *, tag: str = "low", notes: str = "") -> str:
    """Build a minimal item subtree with a State metadata row, matching render_item_subtree."""
    return (
        f"* acme/widgets: item {url.rsplit('/', 1)[-1]} :{tag}:\n"
        "  :PROPERTIES:\n"
        f"  :URL:  {url}\n"
        "  :HOST: github.com\n"
        "  :LAST_SEEN: 2026-07-30T08:00:00Z\n"
        f"  :NOTES: {notes}\n".rstrip() + "\n"
        "  :END:\n"
        f"  {url}\n"
        "\n"
        "  | Field | Value |\n"
        "  |-------+-------|\n"
        f"  | State | {state} |\n"
        "  | Author | someone |\n"
        "\n"
        "  A one line summary.\n"
    )


_HEADER = "#+TITLE: GitHub Inbox\n#+DATE: 2026-07-30 09:00\n\n"


class StripFinishedItemsTest(unittest.TestCase):
    def test_removes_merged_closed_draft_keeps_open(self) -> None:
        doc = _HEADER + "\n".join(
            [
                _item("https://github.com/acme/widgets/pull/1", "merged"),
                _item("https://github.com/acme/widgets/issues/2", "closed"),
                _item("https://github.com/acme/widgets/pull/3", "draft"),
                _item("https://github.com/acme/widgets/issues/4", "open", tag="high"),
            ]
        )
        new_text, removed = strip_finished_items(doc, REMOVE_STATES)

        # Only the open item survives.
        surviving = read_existing_inbox_from_text(new_text)
        self.assertEqual(
            set(surviving), {"https://github.com/acme/widgets/issues/4"}
        )
        # All three finished items are reported with their state.
        self.assertEqual(len(removed), 3)
        self.assertEqual(
            {(url, state) for _, url, state in removed},
            {
                ("https://github.com/acme/widgets/pull/1", "merged"),
                ("https://github.com/acme/widgets/issues/2", "closed"),
                ("https://github.com/acme/widgets/pull/3", "draft"),
            },
        )
        # The doc header is preserved intact (including #+DATE — the fallback cutoff).
        self.assertIn("#+TITLE: GitHub Inbox", new_text)
        self.assertIn("#+DATE: 2026-07-30 09:00", new_text)

    def test_preserves_notes_verbatim(self) -> None:
        note = "check back 2026-08-01, blocked on infra"
        doc = _HEADER + "\n".join(
            [
                _item("https://github.com/acme/widgets/pull/1", "merged"),
                _item("https://github.com/acme/widgets/issues/2", "open", notes=note),
            ]
        )
        new_text, removed = strip_finished_items(doc, REMOVE_STATES)
        self.assertEqual(len(removed), 1)
        self.assertIn(f":NOTES: {note}", new_text)

    def test_no_op_when_nothing_finished(self) -> None:
        doc = _HEADER + _item("https://github.com/acme/widgets/issues/4", "open")
        new_text, removed = strip_finished_items(doc, REMOVE_STATES)
        self.assertEqual(removed, [])
        # Normalised to a single trailing newline, otherwise unchanged.
        self.assertEqual(new_text, doc.rstrip() + "\n")

    def test_item_without_state_row_is_kept(self) -> None:
        stateless = (
            "* acme/widgets: legacy item :low:\n"
            "  :PROPERTIES:\n"
            "  :URL:  https://github.com/acme/widgets/issues/9\n"
            "  :HOST: github.com\n"
            "  :END:\n"
            "  https://github.com/acme/widgets/issues/9\n"
            "\n"
            "  A summary with no metadata table.\n"
        )
        doc = _HEADER + stateless
        new_text, removed = strip_finished_items(doc, REMOVE_STATES)
        self.assertEqual(removed, [])
        self.assertIn("https://github.com/acme/widgets/issues/9", new_text)

    def test_trailing_newline_convention(self) -> None:
        doc = _HEADER + _item("https://github.com/acme/widgets/pull/1", "merged")
        new_text, _ = strip_finished_items(doc, REMOVE_STATES)
        self.assertTrue(new_text.endswith("\n"))
        self.assertFalse(new_text.endswith("\n\n"))


def read_existing_inbox_from_text(text: str) -> dict:
    """Parse an inbox doc held in memory by round-tripping it through a temp file."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "github.org"
        p.write_text(text, encoding="utf-8")
        return read_existing_inbox(p)


if __name__ == "__main__":
    unittest.main()
