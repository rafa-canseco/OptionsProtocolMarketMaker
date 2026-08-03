#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
SCANNER="$REPO_ROOT/scripts/harness-sensitive-scan.py"
failed=0

cd "$REPO_ROOT"

if [[ ! -f "$SCANNER" ]]; then
  printf 'sensitive-check: missing content scanner: %s\n' "$SCANNER" >&2
  exit 1
fi

# Paths are always checked from the exact index that a commit would consume.
while IFS= read -r -d '' path; do
  case "$path" in
    .env.example|*/.env.example) ;;
    .mcp.json|*/.mcp.json|.env|.env.*|*/.env|*/.env.*|*settings.local.json|*.pem|*.key|*.p12|*.pfx|*keypair*.json|*keystore*.json|*wallet*.json|*secret*.json|data/*|broadcast/*|*/broadcast/*)
      printf 'sensitive-check: forbidden path in index: %s\n' "$path" >&2
      failed=1
      ;;
  esac
done < <(git ls-files --cached -z)

# `--cached` is intentional: this is the exact staged/index content. The second
# scan is a separate development-time policy for tracked working-tree edits.
if ! "$SCANNER" --source index < <(
  git grep --cached -I -l -z -e '' -- . ':(exclude)*.lock' 2>/dev/null || true
); then
  failed=1
fi

if ! "$SCANNER" --source working-tree < <(
  git grep -I -l -z -e '' -- . ':(exclude)*.lock' 2>/dev/null || true
); then
  failed=1
fi

if (( failed != 0 )); then
  printf 'sensitive-check: FAILED; do not commit or push\n' >&2
  exit 1
fi

printf 'sensitive-check: index paths/content and tracked working-tree content passed heuristic checks\n'
