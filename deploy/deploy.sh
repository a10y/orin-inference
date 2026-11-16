#!/usr/bin/env bash
# Roll the server out to a commit on origin/main: drain, update, restart, health
# check, and roll back automatically if the new version doesn't come up.
#
# Usage: deploy/deploy.sh <40-char commit sha>
# Normally invoked by CI through deploy/ssh-entry.sh (see deploy/README.md).
#
# Output is shown in public CI logs, so this script never prints host names, IPs
# or server logs; those stay in data/deploy.log and data/server.log on the device.
#
# Settings (environment, or infer.env in the app directory):
#   INFER_SERVICE          systemd user unit to restart       (orin-inference)
#   INFER_PORT             port the server listens on         (8765)
#   INFER_DATA             data directory                     (<app>/data)
#   DEPLOY_BRANCH          branch deploys must come from      (main)
#   DEPLOY_DRAIN_TIMEOUT   seconds to wait for a running job  (1800)
#   DEPLOY_HEALTH_TIMEOUT  seconds to wait for the new server (180)
#   DEPLOY_INSTALL_UNIT    copy orin-inference.service into ~/.config/systemd/user (1)
#
# `git reset` below can rewrite this file while it runs, and bash reads scripts
# lazily, so deploys run from a private copy (see the re-exec at the bottom).
set -euo pipefail

main() {
  local target=${1:-}
  [[ $target =~ ^[0-9a-f]{40}$ ]] || { echo "usage: $0 <40-char commit sha>" >&2; exit 2; }

  APP=$DEPLOY_APP_DIR
  cd "$APP"
  # shellcheck disable=SC1091
  if [[ -f infer.env ]]; then set -a; . ./infer.env; set +a; fi
  SERVICE=${INFER_SERVICE:-orin-inference}
  DATA=${INFER_DATA:-$APP/data}
  PORT=${INFER_PORT:-8765}
  BRANCH=${DEPLOY_BRANCH:-main}
  DRAIN_TIMEOUT=${DEPLOY_DRAIN_TIMEOUT:-1800}
  HEALTH_TIMEOUT=${DEPLOY_HEALTH_TIMEOUT:-180}
  INSTALL_UNIT=${DEPLOY_INSTALL_UNIT:-1}
  UV=$(command -v uv || echo "$HOME/.local/bin/uv")
  export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}

  mkdir -p "$DATA"
  exec > >(tee -a "$DATA/deploy.log") 2>&1
  log "deploy of ${target:0:12} requested ($(date -u +%FT%TZ))"

  exec 9>"$DATA/deploy.lock"
  flock -w 3600 9 || die "another deploy is still running"

  git fetch --quiet origin "$BRANCH"
  git cat-file -e "${target}^{commit}" 2>/dev/null || die "unknown commit ${target:0:12}"
  git merge-base --is-ancestor "$target" "origin/$BRANCH" || die "${target:0:12} is not on origin/$BRANCH"
  [[ -z $(git status --porcelain --untracked-files=no) ]] || die "working tree has local changes; refusing to overwrite them"

  local current
  current=$(git rev-parse HEAD)
  if [[ $current == "$target" ]]; then
    log "already at ${target:0:12}"
    healthy "$target" 30 && { log "server is healthy"; exit 0; }
    log "server unhealthy; restarting"
    restart
    healthy "$target" "$HEALTH_TIMEOUT" || die "server still unhealthy after restart"
    exit 0
  fi
  if git merge-base --is-ancestor "$target" "$current"; then
    log "${target:0:12} is older than the running ${current:0:12}; nothing to do"
    exit 0
  fi

  drain
  log "updating ${current:0:12} -> ${target:0:12}"
  git log --oneline --no-decorate "$current..$target" | sed 's/^/  /' | head -50
  checkout "$target"
  if apply_changes "$current" && restart && healthy "$target" "$HEALTH_TIMEOUT"; then
    log "deployed ${target:0:12}"
    exit 0
  fi

  log "new version failed to install or start; rolling back to ${current:0:12}"
  log "details are in data/server.log and data/deploy.log on the device"
  checkout "$current"
  if apply_changes "$target" && restart && healthy "$current" "$HEALTH_TIMEOUT"; then
    die "rolled back to ${current:0:12}; deploy of ${target:0:12} failed"
  fi
  die "ROLLBACK FAILED: server is not healthy at ${current:0:12}"
}

log() { printf '[deploy] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

base_url() { printf 'http://%s:%s' "$(tailscale ip -4 | head -n1)" "$PORT"; }

status_json() { curl -fsS -m 5 "$(base_url)/api/status" 2>/dev/null; }

drain() {
  # The worker checks for this file before starting each job, so no new job
  # starts while we wait for the running one (if any) to finish.
  touch "$DATA/PAUSE"
  trap 'rm -f "$DATA/PAUSE" "$DEPLOY_SELF_COPY"' EXIT
  local deadline=$((SECONDS + DRAIN_TIMEOUT)) busy announced=0
  while :; do
    busy=$(status_json | jq -r '((.counts.running // 0) + (.counts.canceling // 0))' 2>/dev/null || echo 0)
    [[ $busy == 0 ]] && break
    ((announced)) || { log "waiting for the running job to finish (up to ${DRAIN_TIMEOUT}s)"; announced=1; }
    ((SECONDS < deadline)) || die "timed out waiting for the running job; re-run the deploy later"
    sleep 10
  done
  log "worker is idle"
}

checkout() {
  git checkout --quiet "$BRANCH" 2>/dev/null || git checkout --quiet -b "$BRANCH"
  git reset --quiet --hard "$1"
}

apply_changes() {
  local from=$1
  if ! git diff --quiet "$from" HEAD -- requirements.txt; then
    install_deps || return 1
  fi
  local unit="$HOME/.config/systemd/user/$SERVICE.service"
  if [[ $INSTALL_UNIT == 1 ]] && ! cmp -s orin-inference.service "$unit"; then
    log "updating systemd unit"
    mkdir -p "$(dirname "$unit")" && cp orin-inference.service "$unit" && systemctl --user daemon-reload || return 1
  fi
}

install_deps() {
  log "requirements.txt changed; updating Python packages"
  local py=.venv/bin/python torch_v tv_v
  # torch/torchvision come from the Jetson CUDA index; pin them so a requirement
  # change can never swap in a CPU-only build from PyPI.
  torch_v=$("$py" -c 'import torch; print(torch.__version__)') || return 1
  tv_v=$("$py" -c 'import torchvision; print(torchvision.__version__)') || return 1
  "$UV" pip install --quiet -p "$py" -r requirements.txt \
    --overrides <(printf 'torch==%s\ntorchvision==%s\n' "$torch_v" "$tv_v") || { log "package install failed"; return 1; }
  "$py" -c 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)' \
    || { log "CUDA-enabled torch is not importable after the package update"; return 1; }
}

restart() {
  log "restarting $SERVICE"
  systemctl --user restart "$SERVICE" || { log "systemctl restart failed"; return 1; }
}

healthy() {
  local want=$1 deadline=$((SECONDS + $2)) got
  while ((SECONDS < deadline)); do
    got=$(status_json | jq -r '.version' 2>/dev/null || true)
    if [[ $got == "$want" ]] && curl -fsS -m 5 -o /dev/null "$(base_url)/api/models"; then
      return 0
    fi
    sleep 3
  done
  return 1
}

if [[ -z ${DEPLOY_SELF_COPY:-} ]]; then
  copy=$(mktemp "${TMPDIR:-/tmp}/orin-deploy.XXXXXX")
  cp "${BASH_SOURCE[0]}" "$copy"
  DEPLOY_SELF_COPY=$copy DEPLOY_APP_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd) exec bash "$copy" "$@"
fi
trap 'rm -f "$DEPLOY_SELF_COPY"' EXIT
main "$@"
