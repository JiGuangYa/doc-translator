#!/usr/bin/env bash
# doc-translator one-click installer (Linux / macOS)
#
# One-line install (curl or wget):
#   curl -fsSL https://raw.githubusercontent.com/ilysom0611/doc-translator/main/install.sh | bash
#   wget -qO- https://raw.githubusercontent.com/ilysom0611/doc-translator/main/install.sh | bash
#
# Optional environment variables:
#   DT_DIR=~/doc-translator    Install directory (default: ~/doc-translator)
#   DT_BRANCH=main             Branch to install (default: main)
#   DT_START=1                 Start the service in the background after install (default: 0, install only)
set -Eeuo pipefail

REPO_URL="${DT_REPO:-https://github.com/ilysom0611/doc-translator.git}"
BRANCH="${DT_BRANCH:-main}"
DEST="${DT_DIR:-$HOME/doc-translator}"

log()  { printf '\033[32m[install]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[install]\033[0m error: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- Pre-flight checks ----------
command -v git >/dev/null 2>&1 || fail "git not found; please install it first (apt/yum install git)"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || fail "python3 not found; please install Python 3.10+"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python version must be >= 3.10 (current: $("$PY" -V 2>&1))"

# ---------- Clone / update source ----------
if [ -d "$DEST/.git" ]; then
    log "Destination exists; pulling latest code: $DEST"
    git -C "$DEST" fetch --depth 1 origin "$BRANCH"
    git -C "$DEST" reset --hard "origin/$BRANCH"
else
    log "Cloning repository to $DEST"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DEST" \
        || fail "Clone failed; please check your network and the repository URL"
fi
cd "$DEST"

# ---------- Virtual environment + dependencies ----------
log "Creating virtual environment and installing dependencies (versions are pinned)"
"$PY" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip -q
./.venv/bin/python -m pip install -r requirements.txt -q \
    || fail "Dependency install failed; please check your network"

# LibreOffice is an optional enhancement (high-fidelity rendering). Its absence does not block install.
if ! command -v soffice >/dev/null 2>&1 && [ ! -x /usr/bin/soffice ]; then
    log "Note: LibreOffice not detected. High-fidelity rendering will fall back to browser-side preview."
    log "      Optional install: yum install -y libreoffice-headless libreoffice-writer libreoffice-impress libreoffice-calc wqy-microhei-fonts"
fi

# ---------- Startup instructions / optional autostart ----------
cat <<EOF

✅ Install complete: $DEST

How to start:
  cd $DEST && ./manage.sh start     # run in the background (use ./manage.sh stop to stop)
  ./manage.sh status                # show run status

Then open http://127.0.0.1:8765 in your browser. On first visit you will be guided to set the admin password.

EOF

if [ "${DT_START:-0}" = "1" ]; then
    log "Starting service in the background..."
    ./manage.sh start
fi
