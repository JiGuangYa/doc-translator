#!/usr/bin/env bash
# doc-translator one-click management script (Linux / macOS)
# Usage: ./manage.sh {install|start|stop|restart|status|update}
# Features: idempotent (safe to re-run), PID file + health check, graceful stop, safe update.
set -Eeuo pipefail

# ---------- Constants (kept in sync with app/config.py) ----------
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"

# On Windows-like environments (Git Bash / MSYS / Cygwin) ps/pkill cannot see native
# Windows processes, so delegate process management to the sibling manage.ps1 to
# avoid false-success outcomes.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        exec powershell -NoProfile -ExecutionPolicy Bypass \
            -File "$BASE_DIR/manage.ps1" "$@"
        ;;
esac

HOST="127.0.0.1"
PORT="8765"
VENV_DIR="$BASE_DIR/.venv"
PY="$VENV_DIR/bin/python"
PID_FILE="$BASE_DIR/data/server.pid"
LOG_FILE="$BASE_DIR/data/server.log"
HEALTH_URL="http://$HOST:$PORT/"
START_TIMEOUT=40   # Seconds to wait for the health check after starting.
STOP_TIMEOUT=15    # Seconds to wait for graceful stop.

# ---------- Output helpers ----------
info()  { echo "[info] $*"; }
ok()    { echo "[ok] $*"; }
warn()  { echo "[warn] $*" >&2; }
die()   { echo "[error] $*" >&2; exit 1; }
on_error() {
    warn "Command failed (line $1). If the problem persists, check the log: $LOG_FILE"
}
trap 'on_error $LINENO' ERR

# ---------- Basic detection ----------

find_system_python() {
    # Find a system Python (>= 3.10); only used during install to create the venv.
    local cand
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
                echo "$cand"
                return 0
            fi
        fi
    done
    return 1
}

venv_python_ok() {
    [ -x "$PY" ] && "$PY" -c 'import fastapi, uvicorn' >/dev/null 2>&1
}

ensure_installed() {
    # Idempotent install: skip if the venv is healthy; otherwise (re)create it.
    if venv_python_ok; then
        return 0
    fi
    info "Starting install (virtual environment is missing or incomplete)..."
    local sys_py
    sys_py="$(find_system_python)" || die "Python 3.10+ not found. Install it first: https://www.python.org/downloads/"
    info "Using system Python: $sys_py ($("$sys_py" -V 2>&1))"
    rm -rf "$VENV_DIR.partial"
    "$sys_py" -m venv "$VENV_DIR.partial" || die "Failed to create virtual environment"
    # Build the venv in a temp dir first, then atomically rename — avoids leaving a
    # half-built venv behind if something fails mid-install.
    rm -rf "$VENV_DIR"
    mv "$VENV_DIR.partial" "$VENV_DIR"
    info "Installing dependencies..."
    "$PY" -m pip install --upgrade pip -q || warn "pip upgrade failed, continuing with the existing pip"
    "$PY" -m pip install -r "$BASE_DIR/requirements.txt" -q \
        || die "Dependency install failed; check your network and retry"
    venv_python_ok || die "Post-install self-check failed (fastapi/uvicorn import error)"
    ok "Install complete"
}

port_responds() {
    # Probe the HTTP health endpoint using venv Python (no curl dependency).
    [ -x "$PY" ] || return 1
    "$PY" - "$HEALTH_URL" <<'EOF' >/dev/null 2>&1
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=2)
except Exception:
    raise SystemExit(1)
EOF
}

pid_matches_server() {
    # PID exists and its command line is this deployment's uvicorn (guards against
    # the PID being recycled by an unrelated process).
    local pid="$1"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -q "uvicorn.*app.main:app"
}

server_running() {
    # Running check: the health endpoint is reachable, OR the PID file points at a
    # live process for this deployment.
    if port_responds; then return 0; fi
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    pid_matches_server "$pid"
}

wait_health() {
    local waited=0 cpid
    until port_responds; do
        sleep 1
        waited=$((waited + 1))
        # Process already exited and the port is not up -> startup failed; stop waiting early.
        cpid="$(read_pid)"
        if [ -n "$cpid" ] && ! kill -0 "$cpid" 2>/dev/null; then
            return 1
        fi
        [ "$waited" -ge "$START_TIMEOUT" ] && return 1
    done
    return 0
}

read_pid() { cat "$PID_FILE" 2>/dev/null || true; }

# ---------- Subcommands ----------

cmd_install() {
    ensure_installed
    mkdir -p "$BASE_DIR/data"
    ok "Ready. Start the service: $0 start"
}

cmd_start() {
    if server_running; then
        ok "Service is already running (http://$HOST:$PORT); no need to start again"
        return 0
    fi
    ensure_installed
    mkdir -p "$BASE_DIR/data"
    rm -f "$PID_FILE"
    : > "$LOG_FILE.tmp"
    info "Starting service..."
    # Launch with an absolute path so the process can be identified by command line.
    nohup "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
        >> "$LOG_FILE.tmp" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    disown "$pid" 2>/dev/null || true
    if wait_health; then
        mv -f "$LOG_FILE.tmp" "$LOG_FILE"
        ok "Service started (PID $pid): http://$HOST:$PORT"
    else
        mv -f "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null || true
        warn "Service did not become ready within ${START_TIMEOUT}s. Recent log:"
        tail -n 20 "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE"
        exit 1
    fi
}

cmd_stop() {
    local pid stopped=0
    pid="$(read_pid)"

    if pid_matches_server "$pid"; then
        info "Stopping service (PID $pid)..."
        kill "$pid" 2>/dev/null || true
        local waited=0
        while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$STOP_TIMEOUT" ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "Graceful stop timed out; force-killing..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        stopped=1
    fi

    # Fallback: PID file is gone or stale but this deployment's process is still listening
    # (e.g. after an unclean exit). Match by command line.
    if [ "$stopped" = 0 ] && port_responds; then
        warn "Cannot locate service by PID file, but port $PORT is responding. Cleaning up by command-line match..."
        pkill -TERM -f "$BASE_DIR.*uvicorn app.main:app" 2>/dev/null || true
        sleep 2
        pkill -KILL -f "$BASE_DIR.*uvicorn app.main:app" 2>/dev/null || true
        sleep 1
        if port_responds; then
            die "Port $PORT still responding after cleanup (another process may be holding it); please resolve manually"
        fi
        stopped=1
    fi

    rm -f "$PID_FILE"
    if [ "$stopped" = 1 ]; then
        ok "Service stopped"
    else
        ok "Service is not running"
    fi
    return 0   # do not exit: restart needs to continue after stop.
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    local pid
    pid="$(read_pid)"
    if server_running; then
        ok "running (PID ${pid:-unknown}): http://$HOST:$PORT"
        exit 0
    fi
    info "not running"
    exit 3   # Convention: exit code 3 means "service not running" (for scripted probes).
}

cmd_update() {
    [ -d "$BASE_DIR/.git" ] || die "Not a git repository; cannot auto-update. Please download the new version manually."

    local was_running=0
    server_running && was_running=1

    info "Pulling latest code..."
    local branch
    branch="$(git symbolic-ref --quiet --short HEAD || echo main)"
    git fetch origin --prune || die "git fetch failed; please check your network"

    # Stash any local changes first (protect user edits) and restore them after the update.
    local stashed=0
    if ! git diff --quiet || ! git diff --cached --quiet; then
        info "Uncommitted local changes detected; stashing for safety..."
        git stash push -u -m "manage.sh update $(date +%F-%T)" || die "Failed to stash local changes; aborting update"
        stashed=1
    fi

    if git rev-parse --verify -q "origin/$branch" >/dev/null; then
        git merge --ff-only "origin/$branch" || {
            warn "Cannot fast-forward merge (local branch has diverged from remote)"
            [ "$stashed" = 1 ] && { git stash pop || warn "Stash restore had conflicts; changes remain in the stash"; }
            die "Please resolve the branch divergence manually and retry"
        }
    else
        warn "Remote branch origin/$branch does not exist; skipping code update"
    fi

    if [ "$stashed" = 1 ]; then
        info "Restoring local changes..."
        git stash pop || warn "Stash restore had conflicts; changes remain in the stash (see git stash list)"
    fi

    info "Refreshing dependencies..."
    ensure_installed

    if [ "$was_running" = 1 ]; then
        info "Restarting service to apply the new version..."
        cmd_restart
    else
        ok "Update complete (service was not running; stays stopped)"
    fi
}

# ---------- Entry point ----------
case "${1:-}" in
    install) cmd_install ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    update)  cmd_update ;;
    *)
        echo "doc-translator service manager"
        echo ""
        echo "Usage: $0 {install|start|stop|restart|status|update}"
        echo ""
        echo "  install   Install or repair the virtual environment and dependencies (idempotent)."
        echo "  start     Start the service in the background and wait for the health check (skips if already running)."
        echo "  stop      Graceful stop (no side effects if not running)."
        echo "  restart   Restart the service."
        echo "  status    Show run status (exit code 3 means not running)."
        echo "  update    Pull latest code, refresh deps, and restart if needed."
        exit 1
        ;;
esac
