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

This package uses the Mellea **ollama** backend with model **`granite4.1:3b`** (declared
in `ghn/config.py` as `BACKEND` and `MODEL_ID`).

1. Install and start [Ollama](https://ollama.com/).
2. Pull the model:

   ```bash
   ollama pull granite4.1:3b
   ```

3. Confirm it is available:

   ```bash
   ollama list | grep granite4.1
   ```

To use a different backend or model, edit `BACKEND` / `MODEL_ID` in
`ghn/config.py`.

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

> **Safety note**: `tools.py` enforces a method allowlist of `GET` and `DELETE` only. The
> bulk mark-all-read call (`PUT /notifications -f read=true`) — which would strand
> notifications — is structurally unreachable. Threads are marked Done one at a time with
> `DELETE /notifications/threads/{id}`, and only after the inbox doc has been written.
