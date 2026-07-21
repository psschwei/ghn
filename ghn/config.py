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

# === Config file loader ===
# Every runtime knob can be set three ways; resolution order (first wins):
#   1. The environment variable (e.g. GHN_MODEL_ID)
#   2. ~/.config/ghn/config.toml -> [section] key
#   3. The hardcoded default below
# The TOML file is uncommitted user config living outside the working tree, so it works
# the same whether run from the repo or after `uv tool install` (location-independent,
# unlike a cwd-based dotenv). Sections: [github] (enterprise_host, inbox_path),
# [backend] (backend, base_url, api_key), [model] (model_id, classifier_model_id,
# item_summary_max_tokens, run_summary_max_tokens).
_CONFIG_PATH: Final[str] = os.path.expanduser('~/.config/ghn/config.toml')


def _load_toml() -> dict:
    try:
        with open(_CONFIG_PATH, 'rb') as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# Parsed once at import; empty dict if the file is missing or malformed.
_TOML: Final[dict] = _load_toml()


def _cfg(env: str, section: str, key: str, default):
    """Resolve a setting: env var > config.toml [section].key > default.

    Empty / whitespace-only values (env or TOML) are treated as unset so they fall
    through to the next source rather than blanking a real default.
    """
    raw = os.environ.get(env)
    if raw is not None and raw.strip() != '':
        return raw
    sect = _TOML.get(section)
    if isinstance(sect, dict):
        val = sect.get(key)
        if val is not None and not (isinstance(val, str) and val.strip() == ''):
            return val
    return default


# === C3: User Facts ===
# Inbox doc location. env GITHUB_INBOX_PATH > [github] inbox_path > ~/org/github.org.
# Leading ~ is expanded so the default resolves to the user's home directory.
INBOX_PATH: Final[str] = os.path.expanduser(
    _cfg('GITHUB_INBOX_PATH', 'github', 'inbox_path', '~/org/github.org')
)
# PROVENANCE: SKILL.md:18-30

GITHUB_COM_HOST: Final[str] = 'github.com'
# PROVENANCE: SKILL.md:45-53


# Optional GitHub Enterprise host to check alongside github.com.
# env GITHUB_ENTERPRISE_HOST > [github] enterprise_host > top-level enterprise_host
# (legacy, kept for back-compat) > None (github.com only).
def _load_enterprise_host() -> str | None:
    host = _cfg('GITHUB_ENTERPRISE_HOST', 'github', 'enterprise_host', None)
    if not host:
        # Legacy: top-level enterprise_host key (pre-[github]-section configs).
        legacy = _TOML.get('enterprise_host')
        host = legacy if isinstance(legacy, str) else None
    host = host.strip() if isinstance(host, str) else None
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
# (or [backend] backend) to point at any backend mellea supports ('ollama', 'hf',
# 'openai', 'watsonx', 'litellm'). Use 'openai' with GHN_BASE_URL to talk to OpenAI
# itself or any OpenAI-compatible server (vLLM, LiteLLM proxy, a hosted gateway, etc.).
BACKEND: Final[str] = _cfg('GHN_BACKEND', 'backend', 'backend', 'ollama')

# Optional endpoint + credentials, threaded into every start_session() as backend kwargs.
# base_url works for both the Ollama and OpenAI-compatible backends (both accept it);
# leave it unset to use the backend's own default (Ollama -> localhost:11434, OpenAI ->
# the public API). api_key is only meaningful for the OpenAI/LiteLLM backends, so it's
# only forwarded for those — Ollama's constructor rejects an unexpected kwarg. The OpenAI
# backend also falls back to the standard OPENAI_API_KEY env var when unset.
_BASE_URL: Final[str | None] = _cfg('GHN_BASE_URL', 'backend', 'base_url', None) or None
_API_KEY: Final[str | None] = _cfg('GHN_API_KEY', 'backend', 'api_key', None) or None


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
MODEL_ID: Final[str] = _cfg('GHN_MODEL_ID', 'model', 'model_id', 'granite4.1:8b')

# Classification (filter mode, priority bucket) is a small fixed-label pick, not prose —
# 3B handles it fine, so we keep it on the cheaper/faster model rather than paying 8B
# latency. Override with GHN_CLASSIFIER_MODEL_ID (e.g. to match MODEL_ID for comparison).
CLASSIFIER_MODEL_ID: Final[str] = _cfg(
    'GHN_CLASSIFIER_MODEL_ID', 'model', 'classifier_model_id', 'granite4.1:3b'
)

LOOP_BUDGET: Final[int] = 3

# Generation budgets (Ollama num_predict). Without these the backend falls back to
# its small default, which truncates summaries to a bare-bones sentence or two.
# Override with GHN_ITEM_SUMMARY_MAX_TOKENS / GHN_RUN_SUMMARY_MAX_TOKENS or the
# [model] item_summary_max_tokens / run_summary_max_tokens keys. int() accepts both a
# TOML integer and a string env value.
ITEM_SUMMARY_MAX_TOKENS: Final[int] = int(
    _cfg('GHN_ITEM_SUMMARY_MAX_TOKENS', 'model', 'item_summary_max_tokens', 1024)
)
RUN_SUMMARY_MAX_TOKENS: Final[int] = int(
    _cfg('GHN_RUN_SUMMARY_MAX_TOKENS', 'model', 'run_summary_max_tokens', 512)
)

# === Self-hosted llama.cpp (spawn-per-run) ===
# Optionally have ghn stand up its own `llama-server` instance for the duration of a run,
# then tear it down. The server exposes an OpenAI-compatible /v1 endpoint, so when spawn is
# on, main.py runs the pipeline with backend='openai' pointed at the local server (see
# run_pipeline's base_url override). One model serves both the summary and classifier roles;
# set GHN_MODEL_ID and GHN_CLASSIFIER_MODEL_ID to the same served name.
# Trade-off: the model's weights load cold on every run — there is no resident daemon like
# Ollama. Fine for occasional/manual runs; revisit a persistent server for tight loops.
# env GHN_LLAMA_* > [llama] key > default.


def _cfg_bool(env: str, section: str, key: str, default: bool) -> bool:
    """Resolve a boolean setting. Accepts TOML booleans and truthy strings ('1', 'true',
    'yes', 'on', case-insensitive); everything else is False."""
    val = _cfg(env, section, key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')


# Master switch: when true, main.py spawns llama-server around the pipeline run.
LLAMA_SPAWN: Final[bool] = _cfg_bool('GHN_LLAMA_SPAWN', 'llama', 'spawn', False)

# The llama-server executable (on PATH or an absolute path).
LLAMA_BINARY: Final[str] = _cfg('GHN_LLAMA_BINARY', 'llama', 'binary', 'llama-server')

# The model to serve: a local .gguf path OR a Hugging Face repo spec (passed as `-hf`, e.g.
# 'ibm-granite/granite-4.1-8b-GGUF'). Required when LLAMA_SPAWN is on; main.py errors
# clearly if it's empty. A value containing '/' with no '.gguf' suffix is treated as an -hf
# spec, otherwise as a local model-file path.
LLAMA_MODEL: Final[str | None] = _cfg('GHN_LLAMA_MODEL', 'llama', 'model', None) or None

# Port llama-server listens on (localhost only). base_url becomes http://127.0.0.1:{port}/v1.
LLAMA_PORT: Final[int] = int(_cfg('GHN_LLAMA_PORT', 'llama', 'port', 8080))

# Seconds to wait for /health to pass before giving up. A large MoE loads cold slowly, so
# the default is generous.
LLAMA_HEALTH_TIMEOUT: Final[int] = int(
    _cfg('GHN_LLAMA_HEALTH_TIMEOUT', 'llama', 'health_timeout', 300)
)

# Free-form extra flags appended to the llama-server command, split with shlex (e.g.
# '-ngl 99 -c 8192 --jinja'). Empty by default.
LLAMA_ARGS: Final[str] = _cfg('GHN_LLAMA_ARGS', 'llama', 'args', '')
