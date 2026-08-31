#!/usr/bin/env bash
set -euo pipefail

unit_name="${BROWSER_CONTROL_UNIT:-wealthsimple-ledger-browser}"
systemctl --user stop "$unit_name.service"
echo "Stopped $unit_name.service"
