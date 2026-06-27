#!/usr/bin/env bash
# PreToolUse guard for Mockingbird. Exit 2 = block the tool call and tell Claude why.
# Covers: secret/.env file writes, off-stack package managers (stack is uv + Ruff only),
# destructive git, and force-adding/staging secrets. Fails open if jq is missing.
command -v jq >/dev/null 2>&1 || exit 0

input="$(cat)"
tool="$(printf '%s' "$input" | jq -r '.tool_name // empty')"

block() { echo "BLOCKED by .claude/hooks/guard.sh: $1" >&2; exit 2; }

case "$tool" in
  Write|Edit|MultiEdit)
    fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"
    [ -n "$fp" ] || exit 0
    base="$(basename "$fp")"
    case "$base" in
      .env.example|.env.sample|.env.template) ;;  # templates are allowed
      .env|.env.*)
        block "writing secret env file '$base'. Real secrets go in .env (gitignored); edit .env.example for the template." ;;
    esac
    case "$base" in
      *.pem|*.key|id_rsa|id_rsa.*|id_ed25519|id_ed25519.*|credentials|credentials.json|*.p12|*.pfx)
        block "writing a credential/key file '$base'. Keys do not belong in the repo." ;;
      *secret*|*Secret*)
        block "writing '$base' — the name implies secrets. Rename it or store the value in .env." ;;
    esac
    ;;
  Bash)
    cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"
    [ -n "$cmd" ] || exit 0

    # Off-stack package managers. `uv pip ...` is allowed (preceded by "uv "); bare pip is not.
    if printf '%s' "$cmd" | grep -Eq '(^|[;&|]|&&|\|\|)[[:space:]]*(npm|npx|yarn|pnpm|poetry|pipenv|conda|pip3?)([[:space:]]|$)'; then
      block "off-stack tool. Mockingbird is uv + Ruff only — use 'uv add' / 'uv run'. (Browser AudioWorklet glue is hand-authored JS, no npm.)"
    fi

    # Destructive git.
    printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+push[[:space:]].*(--force([^-]|$)|-f([[:space:]]|$))' \
      && block "git push --force rewrites remote history. Use --force-with-lease only if you must, after confirming."
    printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+reset[[:space:]]+--hard' \
      && block "git reset --hard discards work irreversibly. Stash or commit first."
    printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+branch[[:space:]]+-D' \
      && block "git branch -D force-deletes a branch. Use -d (safe) or confirm explicitly."
    printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+clean[[:space:]]+-[a-zA-Z]*f' \
      && block "git clean -f deletes untracked files irreversibly."

    # Staging secrets to git (allow .env.example/.sample/.template).
    if printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+add\b' \
       && printf '%s' "$cmd" | grep -Eq '\.env\b' \
       && ! printf '%s' "$cmd" | grep -Eq '\.env\.(example|sample|template)\b'; then
      block "staging a .env secret. Only .env.example belongs in git."
    fi
    ;;
esac
exit 0
