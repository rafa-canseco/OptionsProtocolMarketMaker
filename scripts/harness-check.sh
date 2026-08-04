#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-fast}"
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

if [[ "$MODE" != "doctor" && "$MODE" != "fast" && "$MODE" != "full" ]]; then
  printf 'usage: %s <doctor|fast|full>\n' "$0" >&2
  exit 2
fi

sandbox_root=""

cleanup() {
  case "$sandbox_root" in
    /tmp/marketmaker-harness.*) rm -rf -- "$sandbox_root" ;;
    *) printf 'marketMaker harness: refusing unexpected cleanup path\n' >&2 ;;
  esac
}

sandbox_root="$(mktemp -d /tmp/marketmaker-harness.XXXXXX)"
case "$sandbox_root" in
  /tmp/marketmaker-harness.*) ;;
  *)
    rmdir -- "$sandbox_root" 2>/dev/null || true
    printf 'marketMaker harness: invalid sandbox path\n' >&2
    exit 1
    ;;
esac
trap cleanup EXIT INT TERM

if [[ "${HARNESS_TEST_FORCE_SETUP_FAILURE:-}" == "1" ]]; then
  printf 'marketMaker harness: forced setup failure\n' >&2
  false
fi

if ! command -v uv >/dev/null 2>&1; then
  printf 'marketMaker harness: uv is required\n' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf 'marketMaker harness: python3 is required\n' >&2
  exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  printf 'marketMaker harness: git is required\n' >&2
  exit 1
fi
if [[ ! -f "$REPO_ROOT/uv.lock" ]]; then
  printf 'marketMaker harness: uv.lock is required\n' >&2
  exit 1
fi

uv_bin="$(command -v uv)"
python3_bin="$(command -v python3)"
git_bin="$(command -v git)"
clean_path="$(dirname "$python3_bin"):$(dirname "$git_bin"):/usr/bin:/bin"

mkdir -p \
  "$sandbox_root/home" \
  "$sandbox_root/tmp" \
  "$sandbox_root/cache" \
  "$sandbox_root/uv-cache"

test_private_key="$(
  /usr/bin/env -i \
    PATH="$clean_path" \
    HOME="$sandbox_root/home" \
    TMPDIR="$sandbox_root/tmp" \
    UV_CACHE_DIR="$sandbox_root/uv-cache" \
    UV_OFFLINE=1 \
    "$uv_bin" run --frozen python -c \
      'import hashlib; print(hashlib.sha256(b"market-maker-harness-test-identity").hexdigest())'
)"

clean_env=(
  /usr/bin/env -i
  "PATH=$clean_path"
  "HOME=$sandbox_root/home"
  "TMPDIR=$sandbox_root/tmp"
  "XDG_CACHE_HOME=$sandbox_root/cache"
  "UV_CACHE_DIR=$sandbox_root/uv-cache"
  "UV_OFFLINE=1"
  "PYTHON_DOTENV_DISABLED=1"
  "PYTHONHASHSEED=0"
  "PYTHONUTF8=1"
  "TZ=UTC"
  "HARNESS_SANITIZED=1"
  "HARNESS_SANDBOX_ROOT=$sandbox_root"
  "APP_ENV=test"
  "ASSETS=eth"
  "BACKEND_URL=http://127.0.0.1:9"
  "HEDGE_MODE=simulate"
  "MM_API_KEY=harness-offline"
  "MM_PRIVATE_KEY=$test_private_key"
  "RPC_URL=http://127.0.0.1:9"
  "FUND_ALLOCATOR_ENABLED=false"
  "FUND_OPERATIONS_KEEPER_ENABLED=false"
  "COVERED_CALL_ALLOCATOR_ENABLED=false"
  "COVERED_CALL_OPERATIONS_KEEPER_ENABLED=false"
  "SOLANA_PRIVATE_KEY="
  "SOLANA_QUOTE_PUBLISHING_ENABLED=false"
  "SUPABASE_KEY="
  "SUPABASE_URL="
  "TRADE_LOG_PATH=/dev/null"
)

run_clean() {
  "${clean_env[@]}" "$@"
}

cd "$REPO_ROOT"

run_clean "$REPO_ROOT/scripts/harness-context.sh" --check
run_clean "$REPO_ROOT/scripts/harness-sensitive-check.sh"

# Fail closed if env -i ever stops isolating inherited credentials, proxy routes,
# or runtime paths. Only variable names are included in assertion failures.
run_clean "$uv_bin" run --frozen python - <<'PY'
import os
from pathlib import Path

for forbidden_name in (
    "AWS_SECRET_ACCESS_KEY",
    "B1N428_PRODUCTION_SENTINEL",
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HTTPS_PROXY",
    "RAILWAY_TOKEN",
):
    assert forbidden_name not in os.environ, f"inherited variable: {forbidden_name}"

sandbox_root = Path(os.environ["HARNESS_SANDBOX_ROOT"])
assert Path(os.environ["HOME"]) == sandbox_root / "home"
assert Path(os.environ["TMPDIR"]) == sandbox_root / "tmp"
assert Path(os.environ["XDG_CACHE_HOME"]) == sandbox_root / "cache"
assert Path(os.environ["UV_CACHE_DIR"]) == sandbox_root / "uv-cache"
assert os.environ["BACKEND_URL"] == "http://127.0.0.1:9"
assert os.environ["RPC_URL"] == "http://127.0.0.1:9"
assert os.environ["HEDGE_MODE"] == "simulate"
assert os.environ["TRADE_LOG_PATH"] == "/dev/null"
PY

python_version="$(
  run_clean "$uv_bin" run --frozen python -c \
    'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)"
printf 'marketMaker harness: python=%s mode=%s\n' "$python_version" "$MODE"

if [[ "$MODE" == "doctor" ]]; then
  exit 0
fi

runtime_artifact="$REPO_ROOT/data/trade_history.jsonl"
if [[ -e "$runtime_artifact" ]]; then
  printf 'marketMaker harness: unexpected runtime artifact: %s\n' \
    "$runtime_artifact" >&2
  exit 1
fi

status=0

printf '==> Ruff format\n'
run_clean "$uv_bin" run --frozen ruff format --check src tests scripts || status=1

printf '==> Ruff lint\n'
run_clean "$uv_bin" run --frozen ruff check src tests scripts || status=1

if [[ "$MODE" == "fast" ]]; then
  printf '==> Pytest (fast/offline)\n'
  run_clean "$uv_bin" run --frozen pytest -q -m \
    'not integration and not network' || status=1
else
  printf '==> Pytest (full)\n'
  run_clean "$uv_bin" run --frozen pytest -q || status=1
fi

if [[ -e "$runtime_artifact" ]]; then
  printf 'marketMaker harness: runtime artifact created: %s\n' \
    "$runtime_artifact" >&2
  status=1
fi

if (( status != 0 )); then
  printf 'marketMaker harness: %s checks failed; see evidence above\n' "$MODE" >&2
  exit 1
fi

printf 'marketMaker harness: %s checks passed\n' "$MODE"
