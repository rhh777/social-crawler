#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DEFAULT_ENV_FILE="$PROJECT_ROOT/.env.local"
# Keep the former .env convention as a migration fallback for existing clones.
if [[ ! -f "$DEFAULT_ENV_FILE" && -f "$PROJECT_ROOT/.env" ]]; then
  DEFAULT_ENV_FILE="$PROJECT_ROOT/.env"
fi
# Use the same parser as the CLI/Web entry points. Never source credentials as
# shell code. Re-exec once, before computing any environment-backed defaults.
if [[ "${_CRAWLER_CONFIG_LOADED:-}" != "$PROJECT_ROOT" ]] && \
   [[ -n "${CRAWLER_ENV_FILE:-}" || -f "$DEFAULT_ENV_FILE" ]]; then
  command -v uv >/dev/null 2>&1 || { echo "uv is required." >&2; exit 1; }
  export CRAWLER_ENV_FILE="${CRAWLER_ENV_FILE:-$DEFAULT_ENV_FILE}"
  uv_options=(--locked)
  [[ "${CRAWLER_SKIP_SETUP:-0}" == "1" ]] && uv_options=(--no-sync)
  for argument in "$@"; do
    if [[ "$argument" == "--skip-setup" ]]; then
      uv_options=(--no-sync)
    fi
  done
  exec uv run --project "$PROJECT_ROOT" "${uv_options[@]}" \
    python "$PROJECT_ROOT/src/social_crawler/config.py" --exec \
    env "_CRAWLER_CONFIG_LOADED=$PROJECT_ROOT" bash "${BASH_SOURCE[0]}" "$@"
fi
LAUNCH_DIR="$(pwd -P)"
RUNTIME_DIR="${CRAWLER_ROOT:-${CRAWLER_LOCAL_DIR:-$LAUNCH_DIR/social-crawler-local}}"
WEB_HOST="${CRAWLER_WEB_HOST:-127.0.0.1}"
WEB_PORT="${CRAWLER_WEB_PORT:-8765}"
WORKERS="${CRAWLER_WORKERS:-2}"
SKIP_SETUP="${CRAWLER_SKIP_SETUP:-0}"

usage() {
  cat <<'EOF'
Usage: scripts/start_local.sh [options]

Start Social Crawler, its local PostgreSQL server, and collection workers
without Docker. Runtime files are kept together below the launch directory.

Options:
  --dir PATH       Runtime directory (default: ./social-crawler-local)
  --port PORT      Web console port (default: 8765)
  --workers COUNT  Collection worker count (default: 2)
  --skip-setup     Skip uv sync and Playwright browser installation
  -h, --help       Show this help

Environment equivalents:
  CRAWLER_ROOT (legacy: CRAWLER_LOCAL_DIR), CRAWLER_WEB_HOST, CRAWLER_WEB_PORT,
  CRAWLER_WORKERS, CRAWLER_SKIP_SETUP. Defaults are loaded from the project
  .env.local (or CRAWLER_ENV_FILE). Set CRAWLER_DATABASE_URL to use an existing
  database. A legacy project .env is used only when .env.local is absent.
EOF
}

while (($#)); do
  case "$1" in
    --dir)
      [[ $# -ge 2 ]] || { echo "--dir requires a path" >&2; exit 2; }
      RUNTIME_DIR="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "--port requires a value" >&2; exit 2; }
      WEB_PORT="$2"
      shift 2
      ;;
    --workers)
      [[ $# -ge 2 ]] || { echo "--workers requires a value" >&2; exit 2; }
      WORKERS="$2"
      shift 2
      ;;
    --skip-setup)
      SKIP_SETUP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$WEB_PORT" =~ ^[0-9]+$ ]] && ((WEB_PORT >= 1 && WEB_PORT <= 65535)) || {
  echo "Invalid web port: $WEB_PORT" >&2
  exit 2
}
[[ "$WORKERS" =~ ^[0-9]+$ ]] && ((WORKERS >= 1)) || {
  echo "Invalid worker count: $WORKERS" >&2
  exit 2
}

command -v uv >/dev/null 2>&1 || {
  echo "uv is required. Install it from https://docs.astral.sh/uv/ first." >&2
  exit 1
}

mkdir -p "$RUNTIME_DIR"
RUNTIME_DIR="$(cd "$RUNTIME_DIR" && pwd -P)"
DATA_DIR="$RUNTIME_DIR/data"
DB_DIR="$DATA_DIR/postgres"
DB_URL_FILE="$DATA_DIR/postgres.url"
LOG_DIR="$RUNTIME_DIR/logs"
LOCK_DIR="$RUNTIME_DIR/.launcher.lock"

mkdir -p \
  "$DATA_DIR/cookies" \
  "$DATA_DIR/web" \
  "$DB_DIR" \
  "$RUNTIME_DIR/profiles" \
  "$RUNTIME_DIR/artifacts" \
  "$LOG_DIR"
chmod 700 "$RUNTIME_DIR" "$DATA_DIR" "$DB_DIR" "$DATA_DIR/cookies" \
  "$DATA_DIR/web" "$RUNTIME_DIR/profiles" "$RUNTIME_DIR/artifacts" "$LOG_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  old_pid=""
  [[ -f "$LOCK_DIR/pid" ]] && old_pid="$(<"$LOCK_DIR/pid")"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "A local launcher is already using $RUNTIME_DIR (pid $old_pid)." >&2
    exit 1
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || {
    echo "Cannot recover stale launcher lock: $LOCK_DIR" >&2
    exit 1
  }
  mkdir "$LOCK_DIR"
fi
printf '%s\n' "$$" >"$LOCK_DIR/pid"

POSTGRES_PID=""
WEB_PID=""
CLEANED_UP=0

cleanup() {
  local status=$?
  if ((CLEANED_UP)); then
    return
  fi
  CLEANED_UP=1
  trap - EXIT INT TERM

  if [[ -n "$WEB_PID" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
    kill -TERM "$WEB_PID" 2>/dev/null || true
    wait "$WEB_PID" 2>/dev/null || true
  fi
  if [[ -n "$POSTGRES_PID" ]] && kill -0 "$POSTGRES_PID" 2>/dev/null; then
    kill -TERM "$POSTGRES_PID" 2>/dev/null || true
    wait "$POSTGRES_PID" 2>/dev/null || true
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ((status != 0)); then
    echo "Local Social Crawler stopped with status $status." >&2
  else
    echo "Local Social Crawler stopped."
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

if [[ "$SKIP_SETUP" != "1" ]]; then
  echo "Preparing Python dependencies..."
  (
    cd "$PROJECT_ROOT"
    uv sync --locked
    uv run playwright install chromium
  )
fi

POSTGRES_LOG="$LOG_DIR/postgres.log"
if [[ -z "${CRAWLER_DATABASE_URL:-}" ]]; then
  rm -f "$DB_URL_FILE"
  echo "Starting local PostgreSQL..."
  (
    cd "$PROJECT_ROOT"
    exec uv run --no-project --python 3.11 --with pgserver==0.1.4 \
      "$PROJECT_ROOT/scripts/local_postgres.py" \
      --directory "$DB_DIR" \
      --url-file "$DB_URL_FILE"
  ) >>"$POSTGRES_LOG" 2>&1 &
  POSTGRES_PID=$!

  attempt=0
  while [[ ! -s "$DB_URL_FILE" ]]; do
    if ! kill -0 "$POSTGRES_PID" 2>/dev/null; then
      echo "Local PostgreSQL failed to start. Log: $POSTGRES_LOG" >&2
      tail -n 30 "$POSTGRES_LOG" >&2 || true
      exit 1
    fi
    ((attempt += 1))
    if ((attempt >= 300)); then
      echo "Timed out waiting for local PostgreSQL. Log: $POSTGRES_LOG" >&2
      exit 1
    fi
    sleep 0.1
  done
  chmod 600 "$DB_URL_FILE"

  DATABASE_URL="$(<"$DB_URL_FILE")"
  if [[ -z "$DATABASE_URL" ]]; then
    echo "Local PostgreSQL produced an empty connection URL." >&2
    exit 1
  fi
else
  DATABASE_URL="$CRAWLER_DATABASE_URL"
  echo "Using configured PostgreSQL connection."
fi

echo "Runtime directory: $RUNTIME_DIR"
echo "Web console:      http://$WEB_HOST:$WEB_PORT"
echo "PostgreSQL log:   $POSTGRES_LOG"
echo "Press Ctrl-C to stop the Web service and local PostgreSQL."

(
  cd "$PROJECT_ROOT"
  export CRAWLER_DATABASE_URL="$DATABASE_URL"
  export CRAWLER_ARTIFACTS_DIR="${CRAWLER_ARTIFACTS_DIR:-$RUNTIME_DIR/artifacts}"
  exec uv run social-crawler-web \
    --host "$WEB_HOST" \
    --port "$WEB_PORT" \
    --workers "$WORKERS" \
    --root "$RUNTIME_DIR"
) &
WEB_PID=$!

wait "$WEB_PID"
