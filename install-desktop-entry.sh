#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_dir="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
entry_name="wealthsimple-ledger-inventory.desktop"

mkdir -p "$applications_dir" "$desktop_dir"
sed \
  -e "s|@REPO_ROOT@|$repo_root|g" \
  "$repo_root/resources/$entry_name.in" > "$applications_dir/$entry_name"
cp "$applications_dir/$entry_name" "$desktop_dir/$entry_name"
chmod +x "$applications_dir/$entry_name" "$desktop_dir/$entry_name"
echo "Installed $entry_name for $repo_root"
