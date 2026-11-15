#!/usr/bin/env bash
# Forced command for the CI deploy key (see deploy/README.md). The key can run
# exactly one thing, `deploy <40-char commit sha>`, whatever the client asks for.
set -euo pipefail

read -r verb sha extra <<<"${SSH_ORIGINAL_COMMAND:-}" || true
if [[ ${verb:-} != deploy || ! ${sha:-} =~ ^[0-9a-f]{40}$ || -n ${extra:-} ]]; then
  echo "usage: deploy <40-char commit sha>" >&2
  exit 2
fi
exec "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/deploy.sh" "$sha"
