"""Ollama wrapper — local-only inference client.

Must set HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1, WANDB_MODE=disabled and
never make outbound network calls. See CLAUDE.md's Non-Negotiable Privacy &
Governance Constraints.

Ollama (the server, a separate Homebrew install from the `ollama` PyPI
package this module imports) ships a "cloud" feature as of v0.31.1 — remote
inference and web search proxied through ollama.com, gated by the
OLLAMA_NO_CLOUD env var on the *server* process. A client library can't
inspect how its server was launched, so this wrapper enforces the two
things it CAN verify from the client side (see requirements.txt's ollama
audit note for the full picture):

  - the host is loopback (127.0.0.1/localhost/::1) — never a remote address
  - the model tag doesn't end in "-cloud" — those route to ollama.com's
    hosted inference even through a local daemon, regardless of
    OLLAMA_NO_CLOUD

The operator is still responsible for starting `ollama serve` with
OLLAMA_NO_CLOUD=1.

Development Priority #5 (CLAUDE.md).
"""

from __future__ import annotations

from urllib.parse import urlparse

import ollama

DEFAULT_MODEL = "llama3.1:8b"
"""If tool-calling reliability or hallucination issues keep showing up
despite grounding fixes and low narration temperature (see llm/tools.py's
_NARRATION_TEMPERATURE), CLAUDE.md names Qwen 2.5 14B as the fallback model
to try — larger models generally follow tool-calling instructions more
reliably, at the cost of slower responses and more RAM on the workstation.
Not switched by default; `ollama pull` the tag first if you do."""

DEFAULT_HOST = "http://127.0.0.1:11434"

_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


class RemoteInferenceBlocked(RuntimeError):
    """Raised when configuration would route inference off-box."""


def _assert_loopback_host(host: str) -> None:
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if parsed.hostname not in _LOOPBACK_HOSTNAMES:
        raise RemoteInferenceBlocked(
            f"Ollama host {host!r} is not loopback. CLAUDE.md forbids cloud/"
            "remote inference — only 127.0.0.1/localhost/::1 is allowed."
        )


def _assert_local_model_tag(model: str) -> None:
    if model.endswith("-cloud"):
        raise RemoteInferenceBlocked(
            f"Model tag {model!r} is an Ollama cloud model — it routes to "
            "ollama.com's hosted inference even via a local daemon. Use a "
            "plain local tag (e.g. 'llama3.1:8b')."
        )


class OllamaClient:
    """Thin wrapper around the `ollama` package, scoped to one model.

    CLAUDE.md's LLM Layer is "one model, two prompts" — this class only
    owns the model + chat/tool-calling round trip; callers choose which
    system prompt (schedule_builder.md vs callout_handler.md) to send.
    """

    def __init__(self, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST) -> None:
        _assert_loopback_host(host)
        _assert_local_model_tag(model)
        self.model = model
        self.host = host
        self._client = ollama.Client(host=host)

    def chat(self, messages, *, tools=None, format=None, **kwargs):
        return self._client.chat(
            model=self.model,
            messages=messages,
            tools=tools,
            format=format,
            **kwargs,
        )
