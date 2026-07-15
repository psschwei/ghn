import os
import tomllib
from typing import Final

# === C1: Identity & Behavioral Context ===
PREFIX_TEXT: Final[str] = "You maintain a single living Org-mode document — ~/org/github.org — that is the user's offline GitHub notifications inbox. The doc IS the inbox and persists across runs. GitHub's /notifications feed is a delta of what changed since the last run, NOT the source of truth for the doc's full contents. Fold the delta into the existing doc, then mark those notifications Done so they don't resurface unless something new happens. Be concise, factual, and reflect the current state of each subject."
# PROVENANCE: SKILL.md:16-41

# === C2: Operating Rules ===
HIGH_PRIORITY_REASONS: Final[str] = 'assign,review_requested,mention,author,comment'
# PROVENANCE: SKILL.md:202-204

SKIP_LOW_PRIORITY_COMMENT_FETCH: Final[bool] = True
# PROVENANCE: SKILL.md:202-204

FORBIDDEN_BULK_MARK_READ_NOTE: Final[str] = 'Never use PUT /notifications -f read=true (or the GitHub Enterprise equivalent): it marks EVERYTHING read at once and strands notifications. Mark Done per-thread with DELETE /notifications/threads/{id} only.'
# PROVENANCE: SKILL.md:394-400

ORG_TITLE: Final[str] = 'GitHub Inbox'
# PROVENANCE: SKILL.md:263-264

EMPTY_BUCKET_PLACEHOLDER: Final[str] = '/Nothing right now./'
# PROVENANCE: SKILL.md:315-317

# === C3: User Facts ===
# Inbox doc location. Override with GITHUB_INBOX_PATH; defaults to ~/org/github.org.
# Leading ~ is expanded so the default resolves to the user's home directory.
INBOX_PATH: Final[str] = os.path.expanduser(
    os.environ.get('GITHUB_INBOX_PATH', '~/org/github.org')
)
# PROVENANCE: SKILL.md:18-30

GITHUB_COM_HOST: Final[str] = 'github.com'
# PROVENANCE: SKILL.md:45-53

# Optional GitHub Enterprise host to check alongside github.com.
# Resolution order (first non-empty wins; default is None == github.com only):
#   1. GITHUB_ENTERPRISE_HOST environment variable
#   2. ~/.config/ghn/config.toml -> [github] enterprise_host
#      (or a top-level enterprise_host key)
# The TOML file is uncommitted user config, so it works the same whether run from
# the repo or after `uv tool install` (it lives outside the install/working tree).
_CONFIG_PATH: Final[str] = os.path.expanduser(
    '~/.config/ghn/config.toml'
)


def _load_enterprise_host() -> str | None:
    env = os.environ.get('GITHUB_ENTERPRISE_HOST', '').strip()
    if env:
        return env
    try:
        with open(_CONFIG_PATH, 'rb') as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    github = data.get('github')
    host = (github or {}).get('enterprise_host') if isinstance(github, dict) else None
    if not host:
        host = data.get('enterprise_host')
    host = (host or '').strip() if isinstance(host, str) else None
    return host or None


GITHUB_ENTERPRISE_HOST: Final[str | None] = _load_enterprise_host()
# PROVENANCE: SKILL.md:45-53

# Single source of truth for which GitHub hosts to query: github.com plus the
# configured GitHub Enterprise host, if any.
GITHUB_HOSTS: Final[tuple[str, ...]] = (
    (GITHUB_COM_HOST, GITHUB_ENTERPRISE_HOST)
    if GITHUB_ENTERPRISE_HOST
    else (GITHUB_COM_HOST,)
)

# === C8: Runtime Environment ===
# Mellea backend to drive the models. Defaults to local Ollama; override with GHN_BACKEND
# to point at any backend mellea supports ('ollama', 'hf', 'openai', 'watsonx', 'litellm').
# Use 'openai' with GHN_BASE_URL to talk to OpenAI itself or any OpenAI-compatible server
# (vLLM, LiteLLM proxy, a hosted gateway, etc.).
BACKEND: Final[str] = os.environ.get('GHN_BACKEND', 'ollama')

# Optional endpoint + credentials, threaded into every start_session() as backend kwargs.
# GHN_BASE_URL works for both the Ollama and OpenAI-compatible backends (both accept
# base_url); leave it unset to use the backend's own default (Ollama -> localhost:11434,
# OpenAI -> the public API). GHN_API_KEY is only meaningful for the OpenAI/LiteLLM backends,
# so it's only forwarded for those — Ollama's constructor rejects an unexpected kwarg.
# The OpenAI backend also falls back to the standard OPENAI_API_KEY env var when unset.
_BASE_URL: Final[str | None] = os.environ.get('GHN_BASE_URL') or None
_API_KEY: Final[str | None] = os.environ.get('GHN_API_KEY') or None


def _build_backend_kwargs() -> dict[str, str]:
    kwargs: dict[str, str] = {}
    if _BASE_URL:
        kwargs['base_url'] = _BASE_URL
    if _API_KEY and BACKEND in ('openai', 'litellm'):
        kwargs['api_key'] = _API_KEY
    return kwargs


# Extra keyword args forwarded verbatim to the mellea backend constructor via
# start_session(BACKEND, MODEL_ID, **BACKEND_KWARGS). Empty by default (pure Ollama).
BACKEND_KWARGS: Final[dict[str, str]] = _build_backend_kwargs()

# Granite size drives summary quality: the 3B model hallucinates details and drops
# key context under the dense, multi-clause summary prompts. 8B is the default; bump to
# granite4.1:30b for the best quality, or set GHN_MODEL_ID back to granite4.1:3b to compare.
MODEL_ID: Final[str] = os.environ.get('GHN_MODEL_ID', 'granite4.1:8b')

# Classification (filter mode, priority bucket) is a small fixed-label pick, not prose —
# 3B handles it fine, so we keep it on the cheaper/faster model rather than paying 8B
# latency. Override with GHN_CLASSIFIER_MODEL_ID (e.g. to match MODEL_ID for comparison).
CLASSIFIER_MODEL_ID: Final[str] = os.environ.get('GHN_CLASSIFIER_MODEL_ID', 'granite4.1:3b')

LOOP_BUDGET: Final[int] = 3

# Generation budgets (Ollama num_predict). Without these the backend falls back to
# its small default, which truncates summaries to a bare-bones sentence or two.
# Override per-deployment with GHN_ITEM_SUMMARY_MAX_TOKENS / GHN_RUN_SUMMARY_MAX_TOKENS.
ITEM_SUMMARY_MAX_TOKENS: Final[int] = int(
    os.environ.get('GHN_ITEM_SUMMARY_MAX_TOKENS', '1024')
)
RUN_SUMMARY_MAX_TOKENS: Final[int] = int(
    os.environ.get('GHN_RUN_SUMMARY_MAX_TOKENS', '512')
)
