#!/usr/bin/env bash
# PostToolUse: auto-format edited Python files with Ruff. Never blocks (always exits 0).
# No-ops cleanly before the project is scaffolded (falls back to global ruff if no venv).
command -v jq >/dev/null 2>&1 || exit 0

input="$(cat)"
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"
[ -n "$fp" ] || exit 0
case "$fp" in *.py) ;; *) exit 0 ;; esac
[ -f "$fp" ] || exit 0

dir="$(dirname "$fp")"
# Prefer the project-pinned ruff via uv when a pyproject exists, so project config is honored.
if { [ -f "$dir/pyproject.toml" ] || [ -f "${CLAUDE_PROJECT_DIR:-.}/pyproject.toml" ]; } \
   && command -v uv >/dev/null 2>&1 && uv run ruff --version >/dev/null 2>&1; then
  uv run ruff format "$fp" >/dev/null 2>&1
  uv run ruff check --fix "$fp" >/dev/null 2>&1
  exit 0
fi

if command -v ruff >/dev/null 2>&1; then
  ruff format "$fp" >/dev/null 2>&1
  ruff check --fix "$fp" >/dev/null 2>&1
fi
exit 0
