#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export OLLAMA_HOST=127.0.0.1:11435
export OLLAMA_CONTEXT_LENGTH=65536
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_KEEP_ALIVE=15s

if [[ -x .runtime/ollama-0.40.15/ollama ]]; then
  exec .runtime/ollama-0.40.15/ollama serve
fi
exec ollama serve
