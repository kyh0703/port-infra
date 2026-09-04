#!/usr/bin/env bash
set -euo pipefail

compose_command="${COMPOSE:-docker compose}"
cli=( ${compose_command} --profile telephony run --rm --no-deps livekit-cli )

run_cli() {
  "${cli[@]}" "$@"
}

wait_for_livekit() {
  local retries="${SIP_PROVISION_RETRIES:-30}"
  local interval="${SIP_PROVISION_INTERVAL_SECONDS:-2}"
  local attempt

  for ((attempt = 1; attempt <= retries; attempt += 1)); do
    if run_cli sip inbound list --json >/dev/null 2>&1; then
      return 0
    fi
    sleep "${interval}"
  done

  echo "LiveKit SIP API did not become ready after ${retries} attempts." >&2
  return 1
}

resource_id() {
  jq -r --arg name "$1" '
    .. | objects | select(.name? == $name) |
      (.sipTrunkId? // .sip_trunk_id? // .trunkId? // .trunk_id? //
       .sipDispatchRuleId? // .sip_dispatch_rule_id? // .id?)
  ' | head -n 1
}

wait_for_livekit

inbound_list="$(run_cli sip inbound list --json 2>/dev/null || printf '{}')"
if inbound_id="$(resource_id port-local-inbound <<<"${inbound_list}")" && [[ -n "${inbound_id}" && "${inbound_id}" != null ]]; then
  echo "inbound trunk exists: ${inbound_id}"
else
  inbound_result="$(run_cli sip inbound create /sip/inbound-trunk.json)"
  echo "created inbound trunk:"
  echo "${inbound_result}"
fi

outbound_list="$(run_cli sip outbound list --json 2>/dev/null || printf '{}')"
if outbound_id="$(resource_id port-local-outbound <<<"${outbound_list}")" && [[ -n "${outbound_id}" && "${outbound_id}" != null ]]; then
  echo "outbound trunk exists: ${outbound_id}"
else
  outbound_result="$(run_cli sip outbound create /sip/outbound-trunk.json)"
  echo "created outbound trunk:"
  echo "${outbound_result}"
fi

dispatch_list="$(run_cli sip dispatch list --json 2>/dev/null || printf '{}')"
if dispatch_id="$(resource_id port-local-agent-dispatch <<<"${dispatch_list}")" && [[ -n "${dispatch_id}" && "${dispatch_id}" != null ]]; then
  echo "dispatch rule exists: ${dispatch_id}"
else
  dispatch_result="$(run_cli sip dispatch create /sip/dispatch-rule.json)"
  echo "created dispatch rule (agent: voice-agent):"
  echo "${dispatch_result}"
fi

echo "SIP provisioning complete; rerunning this command is safe."
