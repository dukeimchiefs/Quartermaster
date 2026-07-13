#!/usr/bin/env bash
# Starts the local Ollama server with its cloud feature (remote inference +
# web search via ollama.com) explicitly disabled — see requirements.txt's
# ollama audit note and CLAUDE.md's no-cloud-inference constraint.
#
# This is the ONLY blessed way to start Ollama for this project. Running
# `ollama serve` directly leaves OLLAMA_NO_CLOUD unset, which defaults to
# cloud features being available.
set -euo pipefail

export OLLAMA_NO_CLOUD=1

echo "Starting Ollama with OLLAMA_NO_CLOUD=1 (cloud/remote inference disabled)..."
exec ollama serve
