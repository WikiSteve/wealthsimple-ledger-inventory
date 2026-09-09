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

The default launcher now places Chromium on a separate virtual display. Install
`xvfb metacity x11vnc novnc websockify xauth`, then open its browser-tab viewer:

```bash
./scripts/browser-control-view.sh
```

The viewer can be closed without stopping captures. It listens only on localhost
(6083; VNC5903; debugging9223) and uses a generated private VNC password. Closing
the viewer does not hide/minimize the remote Chromium window. You can watch,
but do not interact during capture. Use `browser-control-stop.sh` to stop the
whole desktop. `BROWSER_CONTROL_MODE=desktop ./scripts/browser-control-launch.sh`
restores the old visible-window mode after stopping the virtual service.
Private `~/.local/state/wealthsimple-ledger-inventory/browser/launch.env` may
select an existing profile with `BROWSER_CONTROL_PROFILE`; never run two browsers
against that same profile. Keep viewer/password/profile files out of Git.

```bash
./scripts/launch_gui.sh
```

The GUI can download fresh 12-month Activity and all-account Holdings CSVs
before running the audit. Leave the controlled browser alone during capture.

Fresh Activity downloads pass a hash/size-bound download receipt to the audit.
This allows the settled-activity fast path even when Wealthsimple omits its
CSV as-of footer. Pending orders and browser-only terminal events are still
checked live. Arbitrary file modification times are never freshness evidence;
invalid/expired receipts fall back to browser detail capture. A download receipt
does not override an explicitly stale broker as-of timestamp.

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

## Sell-quantity coverage

Each bundle includes `sell-order-coverage.json` and `.md`; the account summaries
and ChatGPT handoff include the same assessment. Outcomes are fully covered,
partially uncovered, no sell coverage, excess open-sell quantity, or uncertain.
Uncovered quantities are informational, not an instruction to place an exit.
Open sells exceeding a holding do not prove an oversale has executed.

The parser keeps entered, filled and remaining quantities distinct. It uses an
explicit `Remaining quantity` label or derives the remainder from explicit
`Entered quantity` and `Filled quantity` labels. Original quantity alone is not
remaining quantity. Labels and raw inputs are retained in versioned provenance;
they are parser evidence, not a broker signature. Unverified legacy remaining
values stay unknown on rebuild, with the old value retained as unverified data.
The existing `quantity` column remains the entered-size display.

For ordinary **Pending** orders, coverage uses the displayed entered quantity
unless partial-fill or conflicting evidence is present. This reporting convention
is recorded as `pending_entered_quantity` in `coverage_quantity_bases`; it does
not overwrite raw fill/remaining fields or interpret null as zero. Explicit
remaining quantity or visible original-minus-filled evidence takes priority.
Partial-fill, cancellation/replacement, malformed quantity and conflicting
metadata cases do not use this fallback. Account/security, identity and complete
inventory checks still apply. This policy replaces the earlier blanket uncertainty
for entered-only Pending orders; existing saved reports are not changed automatically.

Serial order-detail capture also records optional, whitelisted React order
metadata. An exact opened card must link its external order ID, account ID and
security ID to one unambiguous order record before that identity is used.
Raw submitted/fill quantities (including null) stay separate from the visible
quantity evidence; this integration does not infer remaining quantity from
hidden fields. Missing or conflicting metadata retains identity uncertainty.
This private UI structure is optional, not a supported Wealthsimple API.

Coverage arithmetic uses exact decimal strings. It excludes terminal orders,
keeps pending cancellations active, refuses ambiguous duplicates and instrument
matches, and separates accounts. A price/quote currency is not inferred from a
CAD settlement total. Unknown remaining quantities never become an exact
uncovered count; independently known excess can still be reported as a lower
bound. Market orders without security identity/quote evidence may be uncertain.

Open-order discovery scans the all-status Activity feed rather than relying on
the Pending quick filter to include partial fills. Only active-order disclosure
panels are opened. If Clear is absent, the collector expands the six known
filter sections and verifies unchecked account/type/holding/status boxes,
unpressed quick filters, and the All timeframe. It saves that observation in
`logs/activity-filter-state.json` and restores the disclosure layout. Missing
or changed controls remain unverified; absence of Clear alone proves nothing.
An unverified filter state, rejected card, incomplete detail, or
bounded scan without a stable bottom prevents a confident exact shortfall.
The scan is bounded, not an unlimited historical crawl. Captures are not atomic:
holdings and orders can change between observations. New field layouts outside
the recognized labels remain uncertain until supported by captured evidence.

See [the research and acceptance contract](docs/SELL_COVERAGE_RESEARCH.md).

The ChatGPT handoff also includes observed pending EFT-deposit availability:
deposit amount, explicitly displayed instant availability, their difference,
estimated completion, observation time and disclosure evidence path. The
collector opens at most ten existing pending deposit cards after the Activity
scan; it never initiates a deposit. Ambiguous or missing evidence stays unknown.
The difference is not automatically subtracted from reconciliation residuals:
initial instant availability is not proof of the current hold or of inclusion
in the displayed account value. Rebuilds retain original observation times.

`deposit-residual-comparison.json`, account summaries and the ChatGPT handoff
compare the original residual with a single evidenced pending CAD deposit's
initial-availability gap in the same account. Exact equality is labelled
“Numerically reconciled under the pending-deposit interpretation”; otherwise
the signed remaining difference is displayed. This does not change the original
residual, reconciliation status, or warning. Missing/legacy currency or status
evidence, incomplete order inventory or account components, inconsistent amounts,
and multiple unresolved deposits prevent a numeric comparison. No tolerance is
used to call a nonzero difference an exact match, and no broker-confirmed current
hold is claimed. Existing bundles are never updated automatically.

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
