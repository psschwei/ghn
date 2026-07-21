# Setup — ghn

## §1 Install

This project is managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Verify the import:

```bash
uv run python -c "from ghn import run_pipeline; print('ok')"
```

## §3 Model backend (C8)

By default this package uses the Mellea **ollama** backend with two models: **`granite4.1:8b`**
for summaries/rendering and the cheaper **`granite4.1:3b`** for fixed-label classification
(filter mode + priority bucket). Defaults are declared in `ghn/config.py`; every knob is
env-overridable, so you don't need to edit the source.

### Local Ollama (default)

1. Install and start [Ollama](https://ollama.com/).
2. Pull both models:

   ```bash
   ollama pull granite4.1:8b
   ollama pull granite4.1:3b
   ```

3. Confirm they are available:

   ```bash
   ollama list | grep granite4.1
   ```

### Configuration knobs

| Env var | Default | Purpose |
| --- | --- | --- |
| `GHN_BACKEND` | `ollama` | Mellea backend: `ollama`, `hf`, `openai`, `watsonx`, `litellm`. |
| `GHN_MODEL_ID` | `granite4.1:8b` | Model for summaries / rendering. |
| `GHN_CLASSIFIER_MODEL_ID` | `granite4.1:3b` | Cheaper model for classification. |
| `GHN_BASE_URL` | *(backend default)* | Endpoint URL. Works for Ollama (e.g. a remote GPU box) **and** the OpenAI-compatible backends. |
| `GHN_API_KEY` | *(unset)* | API key; forwarded only for the `openai` / `litellm` backends (ignored by Ollama). |

The model-id strings are passed through verbatim — there's no tag-format validation — so
when you point at a non-Ollama endpoint, set the model ids to names that endpoint serves.

Every knob above can also be set persistently in `~/.config/ghn/config.toml` instead of
the environment — see [§3c](#3c-config-file-configtoml) below.

### Hosted / OpenAI-compatible endpoint

To talk to OpenAI, a LiteLLM proxy, vLLM, or any OpenAI-compatible gateway, use the
`openai` backend with a base URL and key:

```bash
export GHN_BACKEND=openai
export GHN_BASE_URL=https://your-endpoint/v1
export GHN_API_KEY=sk-...
export GHN_MODEL_ID=claude-opus-4-8            # whatever the endpoint serves
export GHN_CLASSIFIER_MODEL_ID=claude-haiku-4-5-20251001
```

(For Anthropic's native API rather than an OpenAI-compatible proxy, use
`GHN_BACKEND=litellm` with a `claude-*` model id.)

The inbox doc is written to `~/org/github.org` by default. Set the `GITHUB_INBOX_PATH`
environment variable to write it elsewhere (a leading `~` is expanded). Its parent
directory is created automatically on the first write.

## §3a Tool prerequisite — GitHub CLI (C6 `real_impl`)

All GitHub access goes through the authenticated **`gh` CLI** (`real_impl` tools in
`tools.py`). No API token is embedded in this package — `gh` supplies its own credentials.

1. Install the [GitHub CLI](https://cli.github.com/).
2. Authenticate each host you use:

   ```bash
   gh auth login                                  # github.com
   gh auth login --hostname your.enterprise.host  # GitHub Enterprise (if configured)
   ```

3. Confirm:

   ```bash
   gh api /user --jq .login
   gh api /user --hostname your.enterprise.host --jq .login   # if configured
   ```

A host that is not authenticated is simply skipped at runtime, so a github.com-only user
works without any GitHub Enterprise setup.

### §3b GitHub Enterprise host (optional)

By default only `github.com` is checked. To also check a GitHub Enterprise instance,
configure its host — no repo edit required, so it survives `uv tool install`:

- **Config file** (recommended) — create an uncommitted
  `~/.config/ghn/config.toml`:

  ```toml
  [github]
  enterprise_host = "your.enterprise.host"
  ```

- **Environment variable** — overrides the file when set:

  ```bash
  export GITHUB_ENTERPRISE_HOST=your.enterprise.host
  ```

The host is resolved once at startup (env var first, then the config file). Leave both
unset for a github.com-only setup.

### §3c Config file (`config.toml`)

Every knob is resolvable three ways, in order: **environment variable → `config.toml`
→ built-in default**. So env vars still win for one-off overrides, while `config.toml`
gives you a persistent, location-independent setup (unlike a cwd-based `.env`, it's read
from `~/.config/ghn/config.toml` no matter which directory you launch `ghn` from).

A full `~/.config/ghn/config.toml`:

```toml
[github]
enterprise_host = "your.enterprise.host"   # GITHUB_ENTERPRISE_HOST
inbox_path      = "~/org/github.org"       # GITHUB_INBOX_PATH (leading ~ expanded)

[backend]
backend  = "openai"                        # GHN_BACKEND
base_url = "http://localhost:1234/v1"      # GHN_BASE_URL (e.g. an LM Studio server)
api_key  = "lm-studio"                     # GHN_API_KEY (openai / litellm only)

[model]
model_id                = "granite4.1:8b"  # GHN_MODEL_ID
classifier_model_id     = "granite4.1:3b"  # GHN_CLASSIFIER_MODEL_ID
item_summary_max_tokens = 1024             # GHN_ITEM_SUMMARY_MAX_TOKENS
run_summary_max_tokens  = 512              # GHN_RUN_SUMMARY_MAX_TOKENS
```

Every key is optional — omit a section or key to keep its default. The file is parsed
once at startup; a missing or malformed file is ignored (defaults apply).

> **Safety note**: `tools.py` enforces a method allowlist of `GET` and `DELETE` only. The
> bulk mark-all-read call (`PUT /notifications -f read=true`) — which would strand
> notifications — is structurally unreachable. Threads are marked Done one at a time with
> `DELETE /notifications/threads/{id}`, and only after the inbox doc has been written.
