#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
pin=b200f5bc60e31588ba2cb60e288029c4a54ba5fa
if [[ ! -d .runtime/hermes/.git ]]; then
  git clone --depth 1 --branch v0.22.7 https://github.com/NousResearch/hermes-agent.git .runtime/hermes
fi
if [[ "$(git -C .runtime/hermes rev-parse HEAD)" != "$pin" ]]; then
  echo "Hermes checkout does not match the pinned v0.22.7 commit." >&2
  exit 1
fi
UV_CACHE_DIR="$PWD/.runtime/hermes-cache" uv sync --project .runtime/hermes --python 3.14 --locked --no-dev \
  --extra messaging --extra ddgs
