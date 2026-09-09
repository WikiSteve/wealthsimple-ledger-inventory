#!/usr/bin/env bash
set -euo pipefail
# Password stays in a private file and the URL fragment (not HTTP query/logs).
exec /usr/bin/python3 - <<'PY'
import os
from pathlib import Path
import webbrowser
from urllib.parse import urlencode
state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state'))) / 'wealthsimple-ledger-inventory/browser'
password = (state/'viewer-password').read_text().strip()
port = int(os.environ.get('BROWSER_CONTROL_VIEWER_PORT', '6083'))
fragment = urlencode({'autoconnect':'true', 'resize':'scale', 'password':password})
webbrowser.open('http://127.0.0.1:' + str(port) + '/vnc.html#' + fragment)
print('Opened private automation desktop viewer.')
PY
