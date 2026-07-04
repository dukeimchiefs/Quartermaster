"""Ollama wrapper — local-only inference client.

Must set HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1, WANDB_MODE=disabled and
never make outbound network calls. See CLAUDE.md's Non-Negotiable Privacy &
Governance Constraints.

Development Priority #5 (CLAUDE.md).
"""

from __future__ import annotations


class OllamaClient:
    def __init__(self, model: str):
        raise NotImplementedError("llm/client.py: Development Priority #5")
