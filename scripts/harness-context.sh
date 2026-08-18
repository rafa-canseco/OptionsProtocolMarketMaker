#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-show}"
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

if [[ "$MODE" != "show" && "$MODE" != "--check" && "$MODE" != "--workspace-root" ]]; then
  printf 'usage: %s [show|--check|--workspace-root]\n' "$0" >&2
  exit 2
fi

if [[ ! -f "$REPO_ROOT/AGENTS.md" ]]; then
  printf 'marketMaker context: missing local protocol: %s\n' \
    "$REPO_ROOT/AGENTS.md" >&2
  exit 1
fi

workspace_root=""
candidate="$REPO_ROOT"
while :; do
  parent="$(dirname -- "$candidate")"
  if [[ "$parent" == "$candidate" ]]; then
    break
  fi
  candidate="$parent"
  if [[ -x "$candidate/harness/bin/context" ]]; then
    workspace_root="$candidate"
    break
  fi
done

if [[ -z "$workspace_root" ]]; then
  if [[ "$MODE" == "--workspace-root" ]]; then
    printf 'marketMaker context: workspace root unavailable in standalone checkout\n' >&2
    exit 1
  fi
  printf 'marketMaker context: standalone checkout; using local AGENTS.md\n'
  exit 0
fi

failed=0
resolved_paths=()

if [[ ! -f "$workspace_root/AGENTS.md" ]]; then
  printf 'marketMaker context: missing workspace protocol: %s\n' \
    "$workspace_root/AGENTS.md" >&2
  failed=1
fi

while IFS= read -r line; do
  [[ "$line" == "  "* ]] || continue
  relative_path="${line#  }"
  if [[ "$relative_path" == "marketMaker/AGENTS.md" ]]; then
    resolved_path="$REPO_ROOT/AGENTS.md"
  else
    resolved_path="$workspace_root/$relative_path"
  fi
  resolved_paths+=("$resolved_path")
  if [[ ! -f "$resolved_path" ]]; then
    printf 'marketMaker context: missing routed path: %s\n' \
      "$resolved_path" >&2
    failed=1
  fi
done < <("$workspace_root/harness/bin/context" marketMaker)

if (( ${#resolved_paths[@]} == 0 )); then
  printf 'marketMaker context: workspace router returned no paths\n' >&2
  failed=1
fi
if (( failed != 0 )); then
  exit 1
fi

if [[ "$MODE" == "--workspace-root" ]]; then
  printf '%s\n' "$workspace_root"
  exit 0
fi

printf 'marketMaker context: workspace=%s\n' "$workspace_root"

if [[ "$MODE" == "show" ]]; then
  printf '  %s\n' "$workspace_root/AGENTS.md"
  for resolved_path in "${resolved_paths[@]}"; do
    printf '  %s\n' "$resolved_path"
  done
fi
