#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
role=${1:-caller}
case "$role" in
  caller|agent) ;;
  *) echo 'Usage: scripts/pjsua.sh [caller|agent] [pjsua options] [sip:number@asterisk]' >&2; exit 2 ;;
esac
if [[ $# -gt 0 ]]; then shift; fi
if [[ ! -f "$root/asterisk/local/$role.cfg" ]]; then
  echo 'Run make pjsua-setup first.' >&2
  exit 1
fi
mkdir -p "$root/pjsua/artifacts"
terminal=(-i)
if [[ -t 0 && -t 1 ]]; then terminal+=(-t); fi
exec docker --context "${DOCKER_CONTEXT:-colima}" run --rm --init "${terminal[@]}" \
  --name "port-pjsua-$role" \
  --network "${PJSUA_NETWORK:-infra_default}" \
  --mount "type=bind,source=$root/asterisk/local/$role.cfg,target=/config/account.cfg,readonly" \
  --mount "type=bind,source=$root/pjsua/artifacts,target=/artifacts" \
  port-pjsua:2.17 --config-file=/config/account.cfg "$@"
