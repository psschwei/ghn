"""ghn — a Mellea-compiled GitHub notifications inbox skill.

Maintains a living Org-mode inbox document (``~/org/github.org`` by default; override
with the ``GITHUB_INBOX_PATH`` environment variable) by folding
the unread-notification delta from GitHub into the existing doc, then marking the
folded threads Done. Compiled from ``skills/github-notifications/SKILL.md`` by melleafy.
"""

from .pipeline import run_pipeline

__all__ = ["run_pipeline"]
