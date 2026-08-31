#!/usr/bin/env bash
set -euo pipefail

state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/wealthsimple-ledger-inventory/browser"
profile_dir="${BROWSER_CONTROL_PROFILE:-${XDG_DATA_HOME:-$HOME/.local/share}/wealthsimple-ledger-inventory/chromium-profile}"
log_file="$state_dir/chromium.log"
port="${BROWSER_CONTROL_PORT:-9223}"
browser_bin="${BROWSER_CONTROL_BROWSER:-$(command -v chromium || command -v google-chrome || true)}"
unit_name="${BROWSER_CONTROL_UNIT:-wealthsimple-ledger-browser}"

if [[ -z "$browser_bin" ]]; then
  echo "Chromium or Google Chrome is required." >&2
  exit 1
fi

mkdir -p "$state_dir" "$profile_dir"

if curl -fsS "http://127.0.0.1:$port/json/version" >/dev/null 2>&1; then
  echo "Controlled browser already running on 127.0.0.1:$port"
  exit 0
fi

systemd-run --user --unit="$unit_name" --collect \
  --property="StandardOutput=append:$log_file" \
  --property="StandardError=append:$log_file" \
  "$browser_bin" \
    --user-data-dir="$profile_dir" \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port="$port" \
    --remote-allow-origins=* \
    --no-first-run \
    --no-default-browser-check \
    --disable-session-crashed-bubble \
    --new-window about:blank >/dev/null

for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:$port/json/version" >/dev/null 2>&1; then
    echo "Started controlled browser on 127.0.0.1:$port"
    exit 0
  fi
  sleep 0.5
done

echo "Browser started but its debugging endpoint did not become ready; see $log_file" >&2
exit 1
