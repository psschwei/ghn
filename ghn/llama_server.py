"""Spawn-per-run lifecycle for a self-hosted ``llama.cpp`` ``llama-server``.

When ``GHN_LLAMA_SPAWN`` is on, ``main.py`` uses :func:`spawned_llama_server` to stand up a
single ``llama-server`` process for the duration of one pipeline run, wait until its
``/health`` endpoint reports ready, hand the pipeline an OpenAI-compatible ``/v1`` base URL,
and then tear the process down — even if the pipeline raises.

One model serves both the summary and classifier roles, so this is one process on one port;
see :mod:`ghn.config` for the ``GHN_LLAMA_*`` knobs.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from . import config


class LlamaServerError(RuntimeError):
    """Raised when llama-server cannot be started or fails to become healthy."""


def _build_argv(*, binary: str, model: str, host: str, port: int, extra: str) -> list[str]:
    """Assemble the llama-server command line.

    ``model`` is treated as a Hugging Face repo spec (passed via ``-hf``) when it looks like
    ``owner/repo`` without a local ``.gguf`` suffix; otherwise it's a local model-file path
    (``-m``). Free-form ``extra`` flags are appended verbatim (shlex-split).
    """
    argv = [binary, '--host', host, '--port', str(port)]
    is_hf_spec = '/' in model and not model.lower().endswith('.gguf')
    argv += ['-hf', model] if is_hf_spec else ['-m', model]
    if extra.strip():
        argv += shlex.split(extra)
    return argv


def _health_url(host: str, port: int) -> str:
    return f'http://{host}:{port}/health'


def _base_url(host: str, port: int) -> str:
    return f'http://{host}:{port}/v1'


def _poll_health(url: str) -> bool:
    """Return True once the health endpoint answers 200, False otherwise (any error)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (localhost only)
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


@contextlib.contextmanager
def spawned_llama_server(
    *,
    binary: str | None = None,
    model: str | None = None,
    port: int | None = None,
    host: str = '127.0.0.1',
    health_timeout: int | None = None,
    extra_args: str | None = None,
) -> Iterator[str]:
    """Start ``llama-server``, yield its OpenAI-compatible base URL, then stop it.

    All parameters default to the corresponding :mod:`ghn.config` value. Raises
    :class:`LlamaServerError` if no model is configured, the binary can't be launched, the
    process exits before becoming healthy, or ``/health`` doesn't pass within the timeout.
    The process is always terminated on exit (including when the ``with`` body raises), so a
    failed run never orphans a server.
    """
    binary = binary if binary is not None else config.LLAMA_BINARY
    model = model if model is not None else config.LLAMA_MODEL
    port = port if port is not None else config.LLAMA_PORT
    health_timeout = (
        health_timeout if health_timeout is not None else config.LLAMA_HEALTH_TIMEOUT
    )
    extra_args = extra_args if extra_args is not None else config.LLAMA_ARGS

    if not model or not str(model).strip():
        raise LlamaServerError(
            'No model configured for llama-server. Set GHN_LLAMA_MODEL (a local .gguf path '
            "or a Hugging Face repo spec like 'ibm-granite/granite-4.1-8b-GGUF')."
        )

    argv = _build_argv(
        binary=binary, model=str(model), host=host, port=port, extra=extra_args
    )

    try:
        # Inherit stdout/stderr so the user sees load progress. start_new_session puts the
        # child in its own process group so a stray Ctrl-C to the CLI doesn't race our
        # explicit teardown below.
        proc = subprocess.Popen(argv, start_new_session=True)  # noqa: S603
    except FileNotFoundError as exc:
        raise LlamaServerError(
            f'Could not launch llama-server binary {binary!r}: {exc}. '
            'Is llama.cpp installed and on PATH (or set GHN_LLAMA_BINARY)?'
        ) from exc
    except OSError as exc:
        raise LlamaServerError(f'Failed to start llama-server: {exc}') from exc

    try:
        health = _health_url(host, port)
        deadline = time.monotonic() + health_timeout
        while True:
            # Fail fast if the server died during load rather than waiting out the timeout.
            exit_code = proc.poll()
            if exit_code is not None:
                raise LlamaServerError(
                    f'llama-server exited with code {exit_code} before becoming healthy '
                    f'(command: {shlex.join(argv)}).'
                )
            if _poll_health(health):
                break
            if time.monotonic() >= deadline:
                raise LlamaServerError(
                    f'llama-server did not become healthy within {health_timeout}s '
                    f'(polling {health}).'
                )
            time.sleep(1.0)

        yield _base_url(host, port)
    finally:
        _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort shutdown: SIGTERM, wait, then SIGKILL. Never raises."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)
