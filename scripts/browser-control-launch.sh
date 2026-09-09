#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/wealthsimple-ledger-inventory/browser"
if [[ -f "$state_dir/launch.env" ]]; then
  source "$state_dir/launch.env"
fi
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
  if systemctl --user is-active --quiet "$unit_name.service"; then
    echo "Viewer: $script_dir/browser-control-view.sh"
  fi
  exit 0
fi

if [[ "${BROWSER_CONTROL_MODE:-virtual}" == virtual ]]; then
  for required in Xvfb metacity x11vnc websockify xauth; do
    command -v "$required" >/dev/null || { echo "Missing $required (install virtual desktop dependencies)." >&2; exit 1; }
  done
  systemd-run --user --unit="$unit_name" --collect --property=UMask=0077 \
    --property="StandardOutput=append:$log_file" --property="StandardError=append:$log_file" \
    /usr/bin/python3 "$script_dir/browser-virtual-session.py" \
    --state "$state_dir" --profile "$profile_dir" --browser "$browser_bin" --port "$port" \
    --viewer-port "${BROWSER_CONTROL_VIEWER_PORT:-6083}" \
    --vnc-port "${BROWSER_CONTROL_VNC_PORT:-5903}" --display "${BROWSER_CONTROL_DISPLAY:-93}" >/dev/null
else
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
fi

for _ in {1..80}; do
  if ! systemctl --user is-active --quiet "$unit_name.service"; then
    echo "Browser service exited; see $log_file" >&2
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$port/json/version" >/dev/null 2>&1; then
    echo "Started controlled browser on 127.0.0.1:$port"
    echo "Open viewer when wanted: $script_dir/browser-control-view.sh"
    exit 0
  fi
  sleep 0.5
done

echo "Browser started but its debugging endpoint did not become ready; see $log_file" >&2
exit 1
