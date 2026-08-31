# AI Bootstrap Guide

This file is the fastest safe orientation path for any coding agent. Read it,
`README.md`, `docs/DANGER_MAP.md`, and `docs/ACCEPTANCE_GATES.md` before editing.

## Objective

Maintain a Linux GUI and CLI that collect and reconcile **read-only** evidence
from an authenticated Wealthsimple Chromium session. Correctness and honest
uncertainty matter more than producing a superficially clean report.

## Non-negotiable invariants

1. Never click, call, or automate Buy, Sell, Review order, Submit, Confirm,
   Modify order, Cancel order, Transfer, Deposit, Withdraw, or account settings.
2. Never put credentials, cookies, browser profiles, real exports, screenshots,
   capture bundles, or populated user context in the repository.
3. Bind Chrome remote debugging to `127.0.0.1`, never a LAN address.
4. Browser data is authoritative for live cash and pending orders. Official CSV
   exports independently control settled activity and holdings.
5. Incomplete evidence must produce a warning or blocker. Never turn a parser
   miss into a confident zero.
6. Do not interact with the controlled browser during a capture unless the
   human explicitly authorizes a read-only diagnostic session.

## Bootstrap on Debian-family Linux

```bash
sudo apt install chromium chromium-driver python3 python3-venv python3-tk
python3 -m venv "$HOME/.local/share/wealthsimple-ledger-inventory/venv"
"$HOME/.local/share/wealthsimple-ledger-inventory/venv/bin/pip" install \
  -r requirements.txt pytest
```

If the distro names ChromeDriver differently, ensure a compatible
`chromedriver` is on `PATH`. Browser and driver major versions must match.

## First AI actions

Run these before changing code:

```bash
git status --short
python3 -m compileall -q src scripts
python3 -m pytest -q
```

Then inspect imports and the smallest relevant test surface with `rg`. Preserve
unrelated user changes. Make narrow patches, add a regression test for each bug,
and rerun the focused test followed by the full suite.

## Local runtime

```bash
./scripts/browser-control-launch.sh
./scripts/launch_gui.sh
```

The dedicated profile lives under the user's XDG data directory, not Git. The
default Python interpreter is:

```text
~/.local/share/wealthsimple-ledger-inventory/venv/bin/python
```

Override it with `WEALTHSIMPLE_BROWSER_PYTHON`. Override optional private facts
with `WEALTHSIMPLE_ACCOUNT_CONTEXT`. To keep a dedicated profile elsewhere,
set `BROWSER_CONTROL_PROFILE` before launching Chromium.

## Architecture and evidence flow

```text
Dedicated Chromium (localhost CDP)
        |
        +--> live accounts / cash / holdings / pending orders / activity cards
        |
Official Activity + Holdings CSV exports
        |
        v
full_account_inventory.py
        |
        +--> normalization + strict reconciliation
        +--> safety/click/performance logs
        +--> Markdown + JSON + CSV + screenshots + ZIP bundle in /tmp
```

Selenium attaches to an existing browser through localhost port 9223 by
default. Set `BROWSER_CONTROL_PORT` before launching either component to use a
different port. It does not launch an authenticated session itself. Most
parser and reconciliation work is pure Python and testable without network or
browser access.

## Public/private boundary

Safe for Git: source, scripts, schemas, documentation, and identity-free
regression fixtures.

Keep private outside Git:

- Chromium profile and cookies
- downloaded Activity/Holdings CSVs
- screenshots, bundles, reports, and ledgers
- populated `user-account-context.json`
- API keys or AI-review transcripts containing portfolio data

Before committing, inspect both staged paths and staged content:

```bash
git diff --cached --name-only
git diff --cached
rg -n -i 'api[_ -]?key|password|secret|token|account number|cookie' . \
  --glob '!AI_BOOTSTRAP.md' --glob '!README.md' --glob '!docs/**'
```

Do not weaken a safety guard merely to make a changed web page pass. Capture
the new read-only shape, write a synthetic regression fixture, and update the
smallest parser or selector that restores evidence quality.
