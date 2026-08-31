# Wealthsimple Ledger Inventory

A Linux desktop tool that reads an already signed-in Wealthsimple web session
and produces a local, auditable inventory of accounts, holdings, cash, pending
orders, and activity. It reconciles live browser evidence with optional
Wealthsimple Activity and Holdings CSV exports and writes Markdown, JSON, CSV,
screenshots, and a ZIP evidence bundle.

This is an independent community project. It is not affiliated with, endorsed
by, or supported by Wealthsimple.

## Safety boundary

The collector is intentionally read-only. It may navigate pages, select
filters, download reports, and open or close disclosure panels. It must never
place, preview, modify, cancel, or submit an order; transfer money; deposit;
withdraw; or change account settings. The navigation guard and click log make
that boundary testable. See [the danger map](docs/DANGER_MAP.md) and
[acceptance gates](docs/ACCEPTANCE_GATES.md).

The program does not accept or store a Wealthsimple password. Authentication
happens directly in a dedicated Chromium profile outside the repository. That
profile, exported CSVs, screenshots, reports, and generated bundles contain
personal financial data and are excluded from Git.

## Quick start (Ubuntu, Debian, or Linux Mint)

```bash
sudo apt install chromium chromium-driver python3 python3-venv python3-tk
git clone https://github.com/WikiSteve/wealthsimple-ledger-inventory.git
cd wealthsimple-ledger-inventory
python3 -m venv "$HOME/.local/share/wealthsimple-ledger-inventory/venv"
"$HOME/.local/share/wealthsimple-ledger-inventory/venv/bin/pip" install -r requirements.txt
./scripts/browser-control-launch.sh
```

Sign in to Wealthsimple in the new Chromium window, then launch the GUI:

```bash
./scripts/launch_gui.sh
```

The GUI can download fresh 12-month Activity and all-account Holdings CSVs
before running the audit. Leave the controlled browser alone during capture.

Install a desktop/menu shortcut with:

```bash
./install-desktop-entry.sh
```

## Command line

```bash
python3 scripts/run_full_inventory.py --mode FULL
```

To supply exports explicitly:

```bash
python3 scripts/run_full_inventory.py --mode FULL \
  --activity-export "$HOME/Downloads/activities-export-YYYY-MM-DD.csv" \
  --holdings-export "$HOME/Downloads/holdings-report-YYYY-MM-DD.csv"
```

Output is created under `/tmp/wealthsimple-full-account-inventory-*`. The GUI
can browse completed bundles and their reports.

## Optional private account context

The collector can annotate facts that cannot be inferred safely from balances,
such as a user-reported USD-account conversion grace period. Copy
`config/user-account-context.example.json` to:

```text
~/.config/wealthsimple-ledger-inventory/user-account-context.json
```

Alternatively set `WEALTHSIMPLE_ACCOUNT_CONTEXT` to another private path.
Never commit the populated file.

## Tests

```bash
python3 -m pytest -q
```

The test fixtures are identity-free browser-text shapes used to protect parser
behaviour. They contain no credentials, account numbers, cookies, or browser
profile data.

## Project map

- `scripts/wealthsimple_gui.py` — Tk desktop app and report browser
- `src/full_account_inventory.py` — capture, parsing, reconciliation, reports
- `src/browser_control_preflight.py` — browser/driver compatibility check
- `scripts/download_wealthsimple_*_csv.py` — official export download flows
- `schemas/` — normalized inventory JSON schemas
- `tests/` — browser-free regression tests
- `AI_BOOTSTRAP.md` — provider-neutral setup and maintenance guide for AI tools

## Limitations

Wealthsimple can change its private web UI without notice. A capture should
fail honestly with a warning or blocker when evidence is incomplete; it should
not silently invent a zero balance, empty holding list, or successful
reconciliation. Review generated reports before relying on them.

Licensed under the MIT License.
