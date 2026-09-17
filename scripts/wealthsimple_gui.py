#!/usr/bin/env python3
"""Desktop launcher and report viewer for the read-only Wealthsimple inventory.

This intentionally contains no brokerage actions.  It only starts the existing
read-only capture runner, tails its output, and opens local evidence bundles.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.browser_control_preflight import inspect_browser_control


DEFAULT_BROWSER_PYTHON = Path.home() / ".local" / "share" / "wealthsimple-ledger-inventory" / "venv" / "bin" / "python"
RUNNER = ROOT / "scripts" / "run_full_inventory.py"
ACTIVITY_EXPORT_DOWNLOADER = ROOT / "scripts" / "download_wealthsimple_activity_csv.py"
BUNDLE_PREFIX = "wealthsimple-full-account-inventory-"
CURRENT_YEAR_ACTIVITY_JSON = "recent-activity-current-year.json"
GUI_STATE_PATH = Path.home() / ".local" / "state" / "wealthsimple-ledger-inventory" / "gui.json"
BROWSER_CONTROL_LAUNCH_COMMAND = (str(ROOT / "scripts" / "browser-control-launch.sh"),)


@dataclass(frozen=True)
class Bundle:
    directory: Path
    manifest: dict[str, Any]

    @property
    def label(self) -> str:
        status = self.manifest.get("status", "UNKNOWN")
        created = self.manifest.get("generated_at") or self.directory.name.removeprefix(BUNDLE_PREFIX)
        # A copied/rebuilt bundle can share an ISO timestamp to the second with
        # its source. Include the directory identity so the picker cannot drop
        # one of two distinct evidence bundles.
        return f"{created}  [{status}]  {self.directory.name.removeprefix(BUNDLE_PREFIX)}"


def browser_python() -> Path:
    configured = os.environ.get("WEALTHSIMPLE_BROWSER_PYTHON")
    return Path(configured) if configured else DEFAULT_BROWSER_PYTHON


def discover_bundles(root: Path = Path("/tmp")) -> list[Bundle]:
    bundles: list[Bundle] = []
    for directory in root.glob(f"{BUNDLE_PREFIX}*"):
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        bundles.append(Bundle(directory=directory, manifest=manifest))
    # Rebuilt bundles preserve source filesystem metadata, so directory mtime
    # can put an old source ahead of a newer repaired ledger. The manifest's
    # generated timestamp is the authoritative run ordering.
    return sorted(
        bundles,
        key=lambda item: (str(item.manifest.get("generated_at") or ""), item.directory.name),
        reverse=True,
    )


def build_capture_command(
    mode: str,
    activity_export: Path | None = None,
    holdings_export: Path | None = None,
) -> list[str]:
    if mode not in {"FAST", "FULL"}:
        raise ValueError(f"Unsupported audit mode: {mode}")
    command = [str(browser_python()), str(RUNNER), "--mode", mode]
    if activity_export is not None:
        command.extend(["--activity-export", str(activity_export)])
    if holdings_export is not None:
        command.extend(["--holdings-export", str(holdings_export)])
    return command


def build_fresh_activity_capture_command(
    mode: str,
) -> list[str]:
    """Download, validate, and hand fresh Activity and Holdings CSVs to the audit."""
    if mode not in {"FAST", "FULL"}:
        raise ValueError(f"Unsupported audit mode: {mode}")
    command = [
        str(browser_python()),
        str(ACTIVITY_EXPORT_DOWNLOADER),
        "--activities-12-months",
        "--download-holdings",
        "--run-audit",
        "--audit-mode",
        mode,
    ]
    return command


def save_export_paths(activity: str, holdings: str, path: Path = GUI_STATE_PATH) -> bool:
    """Remember the two chosen export paths; report success without raising.

    Only these two paths are stored - no credential, token, account identifier
    or bundle content. Remembering a choice is a convenience, so a read-only
    config directory or a full disk must never be able to abort a capture.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"activity_export": activity.strip(), "holdings_export": holdings.strip()}, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def latest_export_downloads(directory: Path) -> dict[str, Path]:
    """Newest recognizable activity/holdings CSV in a directory.

    Backs the explicit "Use latest downloads" button. A file removed between
    the glob and the stat is skipped rather than raising inside a Tk callback.
    """
    found: dict[str, Path] = {}
    if not directory.is_dir():
        return found
    for kind, pattern in (("activity", "activities-export-*.csv"), ("holdings", "holdings-report-*.csv")):
        newest: Path | None = None
        newest_mtime = -1.0
        for candidate in directory.glob(pattern):
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = candidate, mtime
        if newest is not None:
            found[kind] = newest
    return found


def activity_csv_from_stdout_line(line: str) -> Path | None:
    cleaned = line.strip()
    if cleaned.startswith("ACTIVITY_CSV=") and len(cleaned) > len("ACTIVITY_CSV="):
        return Path(cleaned[len("ACTIVITY_CSV="):])
    return None


def holdings_csv_from_stdout_line(line: str) -> Path | None:
    cleaned = line.strip()
    if cleaned.startswith("HOLDINGS_CSV=") and len(cleaned) > len("HOLDINGS_CSV="):
        return Path(cleaned[len("HOLDINGS_CSV="):])
    return None


def latest_automated_activity_export(root: Path = Path("/tmp")) -> Path | None:
    """Return the newest validated-then-downloaded Activity CSV, if any."""
    candidates = list(root.glob("wealthsimple-csv-downloads-*/activities-export-*.csv"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def browser_control_ready(timeout: float = 1.5) -> bool:
    """Check endpoint availability and browser/driver compatibility."""
    return inspect_browser_control(timeout=timeout).ready


def browser_control_launch_command() -> list[str]:
    """Return the local-only command that starts the dedicated browser lane."""
    return list(BROWSER_CONTROL_LAUNCH_COMMAND)


def report_path(bundle: Bundle) -> Path:
    preferred = bundle.directory / "all-accounts-summary.md"
    return preferred if preferred.exists() else bundle.directory / "inventory-report.md"


def run_dir_from_stdout_line(line: str) -> Path | None:
    cleaned = line.strip()
    if cleaned.startswith("/tmp/") and cleaned.endswith(".zip"):
        return Path(cleaned[:-4])
    return None


class WealthsimpleAuditApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=16)
        self.master = master
        self.pack(fill="both", expand=True)
        self.process: subprocess.Popen[str] | None = None
        self.browser_operation = False
        self.downloaded_activity_csv: Path | None = None
        self.downloaded_holdings_csv: Path | None = None
        self.active_job = "capture"
        self.output_queue: queue.Queue[str | None] = queue.Queue()
        self.current_bundle: Bundle | None = None
        self.capture_run_dir: Path | None = None
        self.bundle_choice = tk.StringVar()
        self.activity_filter = tk.StringVar(value="All statuses")
        self.activity_export_path = tk.StringVar()
        self.holdings_export_path = tk.StringVar()
        self.download_fresh_activity = tk.BooleanVar(value=True)
        self.status_text = tk.StringVar(value="Ready. The app will only run read-only inventory captures.")
        self.browser_status_text = tk.StringVar(value="Controlled browser: checking")
        self._bundle_by_label: dict[str, Bundle] = {}
        self._load_export_paths()
        self._build()
        self.refresh_bundles()
        self.refresh_browser_status()

    def _build(self) -> None:
        self.master.title("Wealthsimple Ledger Inventory")
        self.master.minsize(980, 680)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        safety = ttk.LabelFrame(self, text="Safety boundary", padding=12)
        safety.grid(row=0, column=0, sticky="ew")
        safety.columnconfigure(0, weight=1)
        ttk.Label(
            safety,
            text=(
                "This tool only reads the logged-in Wealthsimple browser and writes local evidence bundles. "
                "It cannot place, preview, edit, cancel, submit, transfer, or modify brokerage activity."
            ),
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        controls.columnconfigure(6, weight=1)
        ttk.Label(controls, text="Capture:").grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(controls, text="Run read-only audit", command=self.start_capture)
        self.run_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(controls, text="Refresh reports", command=self.refresh_bundles).grid(row=0, column=2, padx=8)
        self.launch_browser_button = ttk.Button(
            controls,
            text="Open controllable browser",
            command=self.launch_controlled_browser,
        )
        self.launch_browser_button.grid(row=0, column=3, padx=(0, 8))
        self.close_browser_button = ttk.Button(controls, text="Close controllable browser", command=self.close_controlled_browser)
        self.close_browser_button.grid(row=0, column=4, padx=8)
        ttk.Label(controls, textvariable=self.browser_status_text).grid(row=0, column=5, padx=(12, 8), sticky="e")
        ttk.Label(controls, textvariable=self.status_text).grid(row=0, column=6, sticky="e")

        notebook = ttk.Notebook(self)
        notebook.grid(row=2, column=0, sticky="nsew")
        run_tab = ttk.Frame(notebook, padding=12)
        report_tab = ttk.Frame(notebook, padding=12)
        ledger_tab = ttk.Frame(notebook, padding=12)
        activity_tab = ttk.Frame(notebook, padding=12)
        checks_tab = ttk.Frame(notebook, padding=12)
        handoff_tab = ttk.Frame(notebook, padding=12)
        notebook.add(run_tab, text="Capture")
        notebook.add(report_tab, text="Reports")
        notebook.add(ledger_tab, text="Ledger")
        notebook.add(activity_tab, text="Activity")
        notebook.add(checks_tab, text="Checks")
        notebook.add(handoff_tab, text="ChatGPT handoff")
        self._build_capture_tab(run_tab)
        self._build_report_tab(report_tab)
        self._build_ledger_tab(ledger_tab)
        self._build_activity_tab(activity_tab)
        self._build_checks_tab(checks_tab)
        self._build_handoff_tab(handoff_tab)

    def _build_capture_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        ttk.Label(
            parent,
            text=(
                "Before starting: sign in to Wealthsimple in the existing browser window. "
                "During capture, leave the browser alone. The tool uses safe navigation, account cards, Activity, "
                "and order-detail disclosure panes only."
            ),
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        exports = ttk.LabelFrame(parent, text="Export evidence", padding=10)
        exports.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        exports.columnconfigure(1, weight=1)
        ttk.Label(exports, text="Activity CSV:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        ttk.Entry(exports, textvariable=self.activity_export_path).grid(row=0, column=1, sticky="ew", pady=(0, 4))
        ttk.Button(exports, text="Choose...", command=lambda: self._choose_export("activity")).grid(row=0, column=2, padx=(8, 0), pady=(0, 4))
        ttk.Label(exports, text="Holdings CSV:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(exports, textvariable=self.holdings_export_path).grid(row=1, column=1, sticky="ew")
        ttk.Button(exports, text="Choose...", command=lambda: self._choose_export("holdings")).grid(row=1, column=2, padx=(8, 0))
        ttk.Button(exports, text="Use latest downloads", command=self._choose_latest_downloads).grid(row=0, column=3, rowspan=2, padx=(12, 0), sticky="ns")
        self.activity_download_button = ttk.Button(
            exports,
            text="Download 12-month Activity CSV",
            command=self.start_activity_download,
        )
        self.activity_download_button.grid(row=0, column=4, rowspan=2, padx=(12, 0), sticky="ns")
        ttk.Checkbutton(
            exports,
            text="Download fresh Activity and Holdings CSVs before audit",
            variable=self.download_fresh_activity,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(
            exports,
            text="The fresh Activity export controls settled history. The browser remains authoritative for live cash and pending orders; a selected Holdings CSV independently checks positions.",
            wraplength=800,
            justify="left",
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(parent, text="Capture log", font=("TkDefaultFont", 10, "bold")).grid(row=2, column=0, sticky="w", pady=(14, 4))
        self.log = tk.Text(parent, wrap="word", state="disabled", height=24)
        self.log.grid(row=3, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, command=self.log.yview)
        scrollbar.grid(row=3, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _build_report_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        chooser = ttk.Frame(parent)
        chooser.grid(row=0, column=0, sticky="ew")
        chooser.columnconfigure(1, weight=1)
        ttk.Label(chooser, text="Evidence bundle:").grid(row=0, column=0, sticky="w")
        self.bundle_combo = ttk.Combobox(chooser, textvariable=self.bundle_choice, state="readonly")
        self.bundle_combo.grid(row=0, column=1, sticky="ew", padx=8)
        self.bundle_combo.bind("<<ComboboxSelected>>", lambda _event: self.select_bundle())
        ttk.Button(chooser, text="Open report", command=self.open_report).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(chooser, text="Open folder", command=self.open_folder).grid(row=0, column=3)

        self.summary = tk.Text(parent, wrap="word", height=10, state="disabled")
        self.summary.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        ttk.Label(parent, text="Captured accounts", font=("TkDefaultFont", 10, "bold")).grid(row=2, column=0, sticky="w")
        columns = ("account", "value", "cash", "cash_cad", "cash_usd", "holdings", "orders", "details")
        self.account_table = ttk.Treeview(parent, columns=columns, show="headings", height=10)
        headings = {
            "account": "Account",
            "value": "Total value",
            "cash": "Total cash (CAD aggregate)",
            "cash_cad": "Native available CAD",
            "cash_usd": "Native available USD",
            "holdings": "Holdings",
            "orders": "Open orders",
            "details": "Detail confirmed",
        }
        widths = {"account": 145, "value": 170, "cash": 165, "cash_cad": 145, "cash_usd": 135, "holdings": 90, "orders": 105, "details": 135}
        for column in columns:
            self.account_table.heading(column, text=headings[column])
            self.account_table.column(column, width=widths[column], anchor="center")
        self.account_table.grid(row=3, column=0, sticky="nsew")

    def _build_ledger_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        tables = ttk.Notebook(parent)
        tables.grid(row=0, column=0, sticky="nsew")
        holdings_frame = ttk.Frame(tables, padding=8)
        orders_frame = ttk.Frame(tables, padding=8)
        tables.add(holdings_frame, text="Holdings")
        tables.add(orders_frame, text="Open orders")
        self.holdings_table = self._make_table(
            holdings_frame,
            ("account", "ticker", "security", "quantity", "price", "market_value", "currency"),
            {"account": "Account", "ticker": "Ticker", "security": "Security", "quantity": "Quantity", "price": "Price", "market_value": "Market value", "currency": "Currency"},
            {"account": 145, "ticker": 80, "security": 270, "quantity": 95, "price": 115, "market_value": 130, "currency": 90},
        )
        self.orders_table = self._make_table(
            orders_frame,
            ("account", "ticker", "side", "quantity", "limit", "submitted", "total", "currency", "expiry", "confirmed"),
            {"account": "Account", "ticker": "Ticker", "side": "Side", "quantity": "Quantity", "limit": "Limit", "submitted": "Submitted", "total": "Estimated total", "currency": "Currency", "expiry": "Expiry", "confirmed": "Evidence"},
            {"account": 130, "ticker": 70, "side": 95, "quantity": 85, "limit": 105, "submitted": 170, "total": 135, "currency": 85, "expiry": 170, "confirmed": 130},
        )

    def _build_activity_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="Show:").grid(row=0, column=0, sticky="w")
        self.activity_combo = ttk.Combobox(controls, textvariable=self.activity_filter, state="readonly", width=24)
        self.activity_combo.grid(row=0, column=1, padx=8, sticky="w")
        self.activity_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_activity_table())
        ttk.Label(
            controls,
            text="Pending is shown here for context but never counted as completed activity.",
        ).grid(row=0, column=2, padx=(12, 0), sticky="w")
        self.activity_table = self._make_table(
            parent,
            ("status", "account", "ticker", "activity", "side", "total", "currency", "date", "evidence"),
            {"status": "Status", "account": "Account", "ticker": "Ticker", "activity": "Activity", "side": "Side", "total": "Total", "currency": "Currency", "date": "Date/time", "evidence": "Evidence"},
            {"status": 135, "account": 130, "ticker": 75, "activity": 140, "side": 80, "total": 130, "currency": 80, "date": 150, "evidence": 140},
            row=1,
        )
        self._activity_rows: list[dict[str, Any]] = []

    def _build_checks_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.checks_text = tk.Text(parent, wrap="word", state="disabled")
        self.checks_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, command=self.checks_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.checks_text.configure(yscrollcommand=scrollbar.set)

    def _build_handoff_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="Paste this into ChatGPT with the ZIP if deeper evidence is needed.").grid(row=0, column=0, sticky="w")
        ttk.Button(controls, text="Copy handoff", command=self.copy_handoff).grid(row=0, column=1, padx=(12, 0))
        self.handoff_text = tk.Text(parent, wrap="word", state="disabled")
        self.handoff_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, command=self.handoff_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.handoff_text.configure(yscrollcommand=scrollbar.set)

    def _make_table(self, parent: ttk.Frame, columns: tuple[str, ...], headings: dict[str, str], widths: dict[str, int], row: int = 0) -> ttk.Treeview:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(row, weight=1)
        table = ttk.Treeview(parent, columns=columns, show="headings")
        for column in columns:
            table.heading(column, text=headings[column])
            table.column(column, width=widths[column], anchor="w")
        table.grid(row=row, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(parent, orient="vertical", command=table.yview)
        vertical.grid(row=row, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=table.xview)
        horizontal.grid(row=row + 1, column=0, sticky="ew")
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return table

    def append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _load_export_paths(self) -> None:
        try:
            saved = json.loads(GUI_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.activity_export_path.set(str(saved.get("activity_export") or ""))
        self.holdings_export_path.set(str(saved.get("holdings_export") or ""))

    def _save_export_paths(self) -> None:
        save_export_paths(self.activity_export_path.get(), self.holdings_export_path.get())

    def _choose_export(self, kind: str) -> None:
        selected = filedialog.askopenfilename(
            title=f"Choose Wealthsimple {kind} export CSV",
            initialdir=str(Path.home() / "Downloads"),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not selected:
            return
        if kind == "activity":
            self.activity_export_path.set(selected)
        else:
            self.holdings_export_path.set(selected)
        self._save_export_paths()

    def _choose_latest_downloads(self) -> None:
        """Explicit user action: pick newest recognizable exports, then show their paths."""
        downloads = Path.home() / "Downloads"
        selected = latest_export_downloads(downloads)
        if "activity" in selected:
            self.activity_export_path.set(str(selected["activity"]))
        if "holdings" in selected:
            self.holdings_export_path.set(str(selected["holdings"]))
        self._save_export_paths()
        for kind, path in sorted(selected.items()):
            self.append_log(f"Selected {kind} export: {path}")
        chosen_count = len(selected)
        if chosen_count:
            self.status_text.set(f"Selected {chosen_count} latest export file(s); review the visible paths before running.")
        else:
            messagebox.showinfo("No matching exports", f"No activities-export or holdings-report CSV was found in:\n{downloads}")

    @staticmethod
    def _selected_export(value: str, label: str) -> Path | None:
        cleaned = value.strip()
        if not cleaned:
            return None
        path = Path(cleaned).expanduser()
        if not path.is_file():
            raise ValueError(f"{label} file does not exist:\n{path}")
        if path.suffix.lower() != ".csv":
            # The chooser offers an "All files" filter, so a statement PDF or a
            # spreadsheet can be picked by mistake. Reject it here with a clear
            # message instead of handing an unreadable file to the runner.
            raise ValueError(f"{label} must be a .csv export:\n{path}")
        return path

    def start_capture(self) -> None:
        if getattr(self, "browser_operation", False):
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Capture running", "A capture is already running. Wait for it to complete.")
            return
        browser_status = inspect_browser_control()
        if not browser_status.ready:
            self.refresh_browser_status()
            messagebox.showerror(
                "Controlled browser is not ready",
                browser_status.message + "\n\nSign in to Wealthsimple in that browser, then try again.",
            )
            return
        interpreter = browser_python()
        if not interpreter.is_file():
            messagebox.showerror("Browser environment missing", f"Cannot find browser Python:\n{interpreter}")
            return
        if not RUNNER.is_file():
            messagebox.showerror("Runner missing", f"Cannot find runner:\n{RUNNER}")
            return
        fresh_activity = self.download_fresh_activity.get()
        if fresh_activity and not ACTIVITY_EXPORT_DOWNLOADER.is_file():
            messagebox.showerror(
                "Exporter missing",
                f"Cannot find Activity CSV exporter:\n{ACTIVITY_EXPORT_DOWNLOADER}",
            )
            return
        try:
            activity_export = (
                None
                if fresh_activity
                else self._selected_export(
                    self.activity_export_path.get(), "Activity export"
                )
            )
            holdings_export = (
                None
                if fresh_activity
                else self._selected_export(self.holdings_export_path.get(), "Holdings export")
            )
        except ValueError as exc:
            messagebox.showerror("Export file unavailable", str(exc))
            return
        self._save_export_paths()
        command = (
            build_fresh_activity_capture_command("FULL")
            if fresh_activity
            else build_capture_command("FULL", activity_export, holdings_export)
        )
        self.capture_run_dir = None
        self.downloaded_activity_csv = None
        self.downloaded_holdings_csv = None
        self.active_job = "capture"
        self.run_button.configure(state="disabled")
        self.activity_download_button.configure(state="disabled")
        self.launch_browser_button.configure(state="disabled")
        self.status_text.set(
            "Downloading fresh Activity and Holdings CSVs, then auditing"
            if fresh_activity
            else "Full capture running"
        )
        self.append_log("$ " + " ".join(command))
        if fresh_activity:
            self.append_log(
                "Fresh Activity and Holdings CSVs will be validated and handed "
                "directly to the audit as independent controls."
            )
        elif activity_export is None and holdings_export is None:
            self.append_log("No CSV exports selected: duplicate completed controls may remain unresolved in browser-only evidence.")
        else:
            attached = ", ".join(path.name for path in (activity_export, holdings_export) if path is not None)
            self.append_log(f"Export evidence attached: {attached}")
        self.append_log("Read-only capture started. Do not interact with the Wealthsimple browser until it finishes.")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.run_button.configure(state="normal")
            self.activity_download_button.configure(state="normal")
            self.launch_browser_button.configure(state="normal")
            self.status_text.set("Could not start capture")
            messagebox.showerror("Capture failed to start", repr(exc))
            return
        threading.Thread(target=self._read_process_output, daemon=True).start()
        self.after(150, self._drain_process_output)

    def start_activity_download(self) -> None:
        """Run only the explicit read-only 12-month Activity CSV exporter."""
        if getattr(self, "browser_operation", False):
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Task running", "Wait for the current read-only task to complete.")
            return
        browser_status = inspect_browser_control()
        if not browser_status.ready:
            self.refresh_browser_status()
            messagebox.showerror("Controlled browser is not ready", browser_status.message)
            return
        interpreter = browser_python()
        if not interpreter.is_file() or not ACTIVITY_EXPORT_DOWNLOADER.is_file():
            messagebox.showerror("Exporter missing", "The browser Python or Activity CSV exporter could not be found.")
            return
        self.active_job = "activity_export"
        self.capture_run_dir = None
        self.downloaded_activity_csv = None
        self.downloaded_holdings_csv = None
        self.run_button.configure(state="disabled")
        self.activity_download_button.configure(state="disabled")
        self.launch_browser_button.configure(state="disabled")
        self.status_text.set("Downloading 12-month Activity CSV")
        command = [str(interpreter), str(ACTIVITY_EXPORT_DOWNLOADER), "--activities-12-months"]
        self.append_log("$ " + " ".join(command))
        self.append_log("Read-only export download started. The export wizard may open briefly; do not use the browser until it finishes.")
        try:
            self.process = subprocess.Popen(
                command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as exc:
            self.process = None
            self.run_button.configure(state="normal")
            self.activity_download_button.configure(state="normal")
            self.launch_browser_button.configure(state="normal")
            self.status_text.set("Could not start Activity CSV download")
            messagebox.showerror("Export failed to start", repr(exc))
            return
        threading.Thread(target=self._read_process_output, daemon=True).start()
        self.after(150, self._drain_process_output)

    def open_browser_viewer(self) -> None:
        """Open the detachable local desktop viewer; do not alter capture state."""
        password_file = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "wealthsimple-ledger-inventory/browser/viewer-password"
        if not password_file.is_file():
            messagebox.showinfo("Browser viewer", "Launch the virtual controlled browser first.")
            return
        subprocess.Popen([str(ROOT / "scripts/browser-control-view.sh")])

    def launch_controlled_browser(self) -> None:
        """Start only the local dedicated browser service without navigating it."""
        if getattr(self, "browser_operation", False):
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Capture running", "Wait for the current read-only task to finish before starting the browser.")
            return
        self.browser_operation = True
        self.launch_browser_button.configure(state="disabled")
        self.status_text.set("Starting controlled browser")
        command = browser_control_launch_command()
        self.append_log("$ " + " ".join(command))
        threading.Thread(target=self._launch_controlled_browser_worker, args=(command,), daemon=True).start()

    def _launch_controlled_browser_worker(self, command: list[str]) -> None:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            self.after(0, lambda: self._finish_controlled_browser_launch(result.returncode, result.stdout, result.stderr))
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.after(0, lambda error=str(exc): self._finish_controlled_browser_launch(1, "", error))

    def _finish_controlled_browser_launch(self, return_code: int, stdout: str, stderr: str) -> None:
        self.browser_operation = False
        self.launch_browser_button.configure(state="normal")
        output = (stdout + stderr).strip()
        if output:
            self.append_log(output)
        status = inspect_browser_control()
        self.refresh_browser_status()
        if return_code == 0 and status.ready:
            self.status_text.set("Controlled browser ready. Sign in to Wealthsimple if prompted.")
            self.open_browser_viewer()
            return
        self.status_text.set("Controlled browser did not start")
        messagebox.showerror("Controlled browser did not start", output or status.message)

    def close_controlled_browser(self) -> None:
        """Stop the dedicated service, retaining its persistent browser profile."""
        if getattr(self, "browser_operation", False):
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Capture running", "Wait for the current read-only task to finish before closing the browser.")
            return
        if not messagebox.askokcancel("Close controllable browser", "Stop the browser and its entire virtual desktop? Saved logins and the browser profile will be kept. Any capture started outside this app must finish first."):
            return
        self.browser_operation = True
        self.status_text.set("Stopping browser and virtual desktop…")
        threading.Thread(target=self._close_controlled_browser_worker, daemon=True).start()

    def _close_controlled_browser_worker(self) -> None:
        try:
            result = subprocess.run([str(ROOT / "scripts/browser-control-stop.sh")], capture_output=True, text=True, timeout=40, check=False)
            self.after(0, lambda: self._finish_controlled_browser_close(result.returncode, result.stdout + result.stderr))
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.after(0, lambda error=str(exc): self._finish_controlled_browser_close(1, error))

    def _finish_controlled_browser_close(self, return_code: int, output: str) -> None:
        self.browser_operation = False
        self.refresh_browser_status()
        if output.strip():
            self.append_log(output.strip())
        if return_code == 0:
            self.status_text.set("Browser and virtual desktop stopped. Saved profile retained.")
        else:
            self.status_text.set("Could not stop the controlled browser")
            messagebox.showerror("Browser stop failed", output)

    def refresh_browser_status(self) -> None:
        status = inspect_browser_control()
        if status.ready:
            self.browser_status_text.set(status.message)
        else:
            self.browser_status_text.set("Controlled browser: not ready")

    def _read_process_output(self) -> None:
        assert self.process is not None
        if self.process.stdout is not None:
            for line in self.process.stdout:
                self.output_queue.put(line)
        # EOF can arrive just before Popen has published returncode. Wait for
        # the child before sending the completion sentinel so a successful
        # audit cannot be reported as stopped merely because returncode was
        # still None when the Tk polling loop handled the queue.
        self.process.wait()
        self.output_queue.put(None)

    def _drain_process_output(self) -> None:
        finished = False
        while True:
            try:
                line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                finished = True
                continue
            self.append_log(line)
            discovered = run_dir_from_stdout_line(line)
            if discovered is not None:
                self.capture_run_dir = discovered
            announced = activity_csv_from_stdout_line(line)
            if announced is not None:
                self.downloaded_activity_csv = announced
            holdings_announced = holdings_csv_from_stdout_line(line)
            if holdings_announced is not None:
                self.downloaded_holdings_csv = holdings_announced
        if not finished:
            self.after(150, self._drain_process_output)
            return
        return_code = self.process.returncode if self.process else 1
        self.process = None
        self.run_button.configure(state="normal")
        self.activity_download_button.configure(state="normal")
        self.launch_browser_button.configure(state="normal")
        if self.active_job == "activity_export":
            csv_path = self.downloaded_activity_csv
            if csv_path is None or not csv_path.is_file():
                csv_path = latest_automated_activity_export()
            if return_code == 0 and csv_path is not None:
                self.activity_export_path.set(str(csv_path))
                self._save_export_paths()
                self.status_text.set("Activity CSV downloaded and selected")
                self.append_log(f"Validated Activity CSV selected: {csv_path}")
                messagebox.showinfo("Activity CSV ready", f"Downloaded and selected:\n{csv_path}")
            else:
                self.status_text.set(f"Activity CSV download stopped: code {return_code}")
                messagebox.showerror("Activity CSV download stopped", "Review the export log. No CSV was selected.")
            self.active_job = "capture"
            return
        manifest = self._load_json(self.capture_run_dir / "manifest.json", {}) if self.capture_run_dir else {}
        manifest_status = manifest.get("status")
        if return_code == 0 and manifest_status in {"OK", "WARN", None}:
            if (
                self.downloaded_activity_csv is not None
                and self.downloaded_activity_csv.is_file()
            ):
                self.activity_export_path.set(
                    str(self.downloaded_activity_csv)
                )
                self._save_export_paths()
                self.append_log(
                    "Fresh Activity CSV preserved and selected: "
                    f"{self.downloaded_activity_csv}"
                )
            if (
                self.downloaded_holdings_csv is not None
                and self.downloaded_holdings_csv.is_file()
            ):
                self.holdings_export_path.set(str(self.downloaded_holdings_csv))
                self._save_export_paths()
                self.append_log(
                    "Fresh Holdings CSV preserved and selected: "
                    f"{self.downloaded_holdings_csv}"
                )
            self.status_text.set(f"Capture finished: {manifest_status or 'OK'}")
            self.append_log("Capture finished. Refreshing local reports.")
            self.refresh_bundles(select_dir=self.capture_run_dir)
            messagebox.showinfo("Read-only capture complete", f"The evidence bundle and local report are ready. Status: {manifest_status or 'OK'}.")
        else:
            self.status_text.set(f"Capture stopped: {manifest_status or f'code {return_code}'}")
            blockers = manifest.get("blockers") or []
            reason = "\n".join(f"- {item}" for item in blockers[:4]) or "Review the capture log and generated bundle."
            self.append_log("Capture did not finish cleanly. " + reason.replace("\n", " "))
            self.refresh_bundles(select_dir=self.capture_run_dir)
            messagebox.showerror("Read-only capture stopped", reason)

    def refresh_bundles(self, select_dir: Path | None = None) -> None:
        bundles = discover_bundles()
        self._bundle_by_label = {bundle.label: bundle for bundle in bundles}
        labels = list(self._bundle_by_label)
        self.bundle_combo.configure(values=labels)
        target = None
        if select_dir:
            target = next((bundle.label for bundle in bundles if bundle.directory == select_dir), None)
        if target is None and labels:
            target = labels[0]
        if target:
            self.bundle_choice.set(target)
            self.select_bundle()
        else:
            self.bundle_choice.set("")
            self.current_bundle = None
            self._set_summary("No local evidence bundles found yet. Run a read-only audit first.")
            self._clear_account_table()
            self._clear_table(self.holdings_table)
            self._clear_table(self.orders_table)
            self._clear_table(self.activity_table)
            self._set_text(self.checks_text, "No audit bundle selected.")
            self._set_text(self.handoff_text, "No audit bundle selected.")

    def select_bundle(self) -> None:
        bundle = self._bundle_by_label.get(self.bundle_choice.get())
        if bundle is None:
            return
        self.current_bundle = bundle
        manifest = bundle.manifest
        accounts = self._load_json(bundle.directory / "account-balances.json", [])
        counts = {
            "holdings": sum((manifest.get("holdings_count_by_account") or {}).values()),
            "orders": sum((manifest.get("open_orders_count_by_account") or {}).values()),
            "detail": sum((manifest.get("detail_confirmed_orders_count_by_account") or {}).values()),
            "row_only": sum((manifest.get("row_only_orders_count_by_account") or {}).values()),
        }
        warnings = manifest.get("warnings") or []
        blockers = manifest.get("blockers") or []
        performance = self._load_json(bundle.directory / "logs" / "performance.json", {})
        text = (
            f"Status: {manifest.get('status', 'UNKNOWN')}\n"
            f"Captured: {', '.join(manifest.get('accounts_seen') or []) or 'none'}\n"
            f"Holdings: {counts['holdings']}    Open orders: {counts['orders']}    "
            f"Detail-confirmed: {counts['detail']}    Row-only: {counts['row_only']}\n"
            f"Read-only confirmation: {manifest.get('read_only_confirmation', False)}\n"
            f"Warnings: {len(warnings)}    Blockers: {len(blockers)}\n"
            f"Bundle: {bundle.directory}"
        )
        if warnings:
            residual_warnings = [
                item for item in warnings
                if "residual" in item.lower() or item.startswith("account total")
            ]
            completeness_warnings = [
                item for item in warnings
                if "completeness" in item.lower()
                or "filter defaults" in item.lower()
                or item.startswith("historical evidence gap")
                or "pending-transaction count" in item.lower()
            ]
            other = [item for item in warnings if item not in residual_warnings + completeness_warnings]
            text += "\n\nUnexplained residuals / completeness (always shown):\n" + "\n".join(
                f"- {item}" for item in (residual_warnings + completeness_warnings) or ["None"]
            )
            if other:
                text += "\n\nTop other warnings:\n" + "\n".join(f"- {item}" for item in other[:5])
        if blockers:
            text += "\n\nBlockers:\n" + "\n".join(f"- {item}" for item in blockers[:5])
        if performance:
            text += "\n\nCapture timing:\n" + "\n".join(
                f"- {name}: {values.get('elapsed_seconds')}s"
                + (f" ({values.get('milliseconds_per_item')} ms/item)" if values.get("milliseconds_per_item") is not None else "")
                for name, values in performance.items()
            )
        self._set_summary(text)
        self._render_accounts(accounts, manifest)
        self._render_ledger(bundle)
        self._render_activity(bundle)
        self._render_checks(bundle)
        handoff_path = bundle.directory / "next-message-for-chatgpt.md"
        handoff = handoff_path.read_text(encoding="utf-8", errors="replace") if handoff_path.exists() else self._build_handoff_fallback(bundle)
        self._set_text(self.handoff_text, handoff)

    def _load_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _set_summary(self, text: str) -> None:
        self._set_text(self.summary, text)

    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _clear_account_table(self) -> None:
        for item in self.account_table.get_children():
            self.account_table.delete(item)

    def _render_accounts(self, accounts: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
        self._clear_account_table()
        holdings_count = manifest.get("holdings_count_by_account") or {}
        order_count = manifest.get("open_orders_count_by_account") or {}
        detail_count = manifest.get("detail_confirmed_orders_count_by_account") or {}
        for account in accounts:
            name = account.get("account") or account.get("displayed_account_name") or "Unknown"
            cash = account.get("available_cash") or account.get("available_to_trade") or "Not captured"
            value = account.get("total_account_value") or "Not captured"
            if account.get("total_account_value_status") == "directly_visible_home_card_currency_unlabeled" and value != "Not captured":
                value = f"{value} (currency unlabeled)"
            self.account_table.insert(
                "",
                "end",
                values=(
                    name, value, cash,
                    account.get("available_cash_cad") or "Not captured",
                    account.get("available_cash_usd") or "Not captured",
                    holdings_count.get(name, 0), order_count.get(name, 0), detail_count.get(name, 0),
                ),
            )

    def _render_ledger(self, bundle: Bundle) -> None:
        self._clear_table(self.holdings_table)
        self._clear_table(self.orders_table)
        holdings = self._load_json(bundle.directory / "holdings-all-accounts.json", [])
        orders = self._load_json(bundle.directory / "open-orders-all-accounts.json", [])
        for holding in holdings:
            self.holdings_table.insert(
                "",
                "end",
                values=(
                    holding.get("account"), holding.get("ticker"), holding.get("security_name") or "",
                    holding.get("quantity") or "", holding.get("current_price") or "",
                    holding.get("market_value") or "", holding.get("market_value_currency") or "",
                ),
            )
        for order in orders:
            self.orders_table.insert(
                "",
                "end",
                values=(
                    order.get("account"), order.get("ticker"), order.get("side_label") or order.get("side") or "",
                    order.get("quantity") or "", order.get("limit_price") or "",
                    " ".join(part for part in [order.get("submitted_date"), order.get("submitted_time")] if part) or "Not shown",
                    order.get("estimated_total") or order.get("estimated_cost_or_proceeds") or "",
                    order.get("order_currency") or "", order.get("expiry") or "",
                    order.get("confirmation_level") or order.get("detail_status") or "",
                ),
            )

    def _render_activity(self, bundle: Bundle) -> None:
        grouped = self._load_json(bundle.directory / "recent-activity-by-status.json", None)
        if isinstance(grouped, dict):
            self._activity_rows = [row for rows in grouped.values() for row in rows]
        else:
            activity_path = bundle.directory / CURRENT_YEAR_ACTIVITY_JSON
            if not activity_path.exists():
                activity_path = bundle.directory / "recent-activity-since-2026-06-17.json"
            self._activity_rows = self._load_json(activity_path, [])
        statuses = sorted({row.get("activity_bucket") or row.get("status") or "Unknown" for row in self._activity_rows})
        self.activity_combo.configure(values=["All statuses", *statuses])
        self.activity_filter.set("All statuses")
        self._refresh_activity_table()

    def _refresh_activity_table(self) -> None:
        self._clear_table(self.activity_table)
        selected = self.activity_filter.get()
        for row in self._activity_rows:
            bucket = row.get("activity_bucket") or row.get("status") or "Unknown"
            if selected != "All statuses" and bucket != selected:
                continue
            self.activity_table.insert(
                "",
                "end",
                values=(
                    bucket, row.get("account") or "", row.get("ticker") or "",
                    row.get("activity_type") or row.get("side_label") or "", row.get("side") or "",
                    row.get("total_value") or row.get("estimated_total") or "", row.get("currency") or row.get("order_currency") or "",
                    " ".join(part for part in [row.get("date"), row.get("time")] if part) or "Not shown", row.get("detail_status") or row.get("confirmation_level") or "",
                ),
            )

    def _render_checks(self, bundle: Bundle) -> None:
        duplicate = self._load_json(bundle.directory / "duplicate-checks.json", [])
        paired = self._load_json(bundle.directory / "paired-exit-checks.json", [])
        filled_buy_exits = self._load_json(bundle.directory / "filled-buy-exit-checks.json", [])
        sell_coverage = self._load_json(bundle.directory / "sell-order-coverage.json", [])
        reserve = self._load_json(bundle.directory / "cash-reserve-reconciliation.json", {})
        lines = ["Reconciliation checks", "", "Duplicate exposure:"]
        lines.extend(f"- {json.dumps(row)}" for row in duplicate) if duplicate else lines.append("- None found")
        lines += ["", "Open sell coverage:"]
        lines.extend(f"- {json.dumps(row)}" for row in sell_coverage) if sell_coverage else lines.append("- No oversell/missing-holding finding")
        lines += ["", "Current holdings with no open sell orders:"]
        missing = [row for row in paired if row.get("type") == "holding_without_open_sell_exit"]
        lines.extend(f"- {json.dumps(row)}" for row in missing) if missing else lines.append("- None found")
        special = [row for row in paired if row.get("type") == "special_attention_status"]
        if special:
            lines += ["", "Special-attention tickers:"]
            lines.extend(f"- {json.dumps(row)}" for row in special)
        lines += [
            "",
            "Historical filled buys vs current missing exits:",
            "- Historical buys are not lot-attributed into actionable current-exit claims.",
            "- Use open-sell coverage and the current holdings section above for uncovered quantities.",
        ]
        if filled_buy_exits:
            lines.extend(f"- {json.dumps(row)}" for row in filled_buy_exits)
        else:
            lines.append("- No per-buy actionable missing-exit claims (by design).")
        lines += ["", "Cash pressure (maximum total if every open buy filled):"]
        for account, values in reserve.items():
            lines.append(
                f"- {account}: displayed native available {values.get('broker_displayed_available_trading_capacity_by_currency')}; "
                f"estimated pending-buy commitments {values.get('estimated_pending_buy_commitments_by_settlement_currency')}; "
                f"reconstructed cash before open-buy holds {values.get('reconstructed_cash_before_open_buy_holds_by_currency')}; "
                f"pending sells {values.get('sum_estimated_open_sell_proceeds_by_currency')}"
            )
        self._set_text(self.checks_text, "\n".join(lines))

    def _build_handoff_fallback(self, bundle: Bundle) -> str:
        manifest = bundle.manifest
        return (
            f"Wealthsimple read-only ledger bundle: {bundle.directory}.zip\n"
            f"Status: {manifest.get('status')}\n"
            f"Accounts: {', '.join(manifest.get('accounts_seen') or [])}\n"
            f"Holdings: {sum((manifest.get('holdings_count_by_account') or {}).values())}\n"
            f"Open orders: {sum((manifest.get('open_orders_count_by_account') or {}).values())}\n"
            "Use the bundle JSON/CSV as evidence. Pending orders and recent activity are separate datasets."
        )

    def copy_handoff(self) -> None:
        text = self.handoff_text.get("1.0", "end-1c")
        self.master.clipboard_clear()
        self.master.clipboard_append(text)
        self.status_text.set("ChatGPT handoff copied")

    @staticmethod
    def _clear_table(table: ttk.Treeview) -> None:
        for item in table.get_children():
            table.delete(item)

    def open_report(self) -> None:
        if self.current_bundle is None:
            return
        target = report_path(self.current_bundle)
        if not target.exists():
            messagebox.showwarning("Report missing", f"No Markdown report found in:\n{self.current_bundle.directory}")
            return
        webbrowser.open(target.as_uri())

    def open_folder(self) -> None:
        if self.current_bundle is None:
            return
        try:
            subprocess.Popen(["xdg-open", str(self.current_bundle.directory)])
        except OSError as exc:
            messagebox.showerror("Could not open folder", repr(exc))

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning(
                "Capture in progress",
                "Keep this window open until the read-only capture completes so its log and final bundle stay visible.",
            )
            return
        self.master.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass
    app = WealthsimpleAuditApp(root)
    root.protocol("WM_DELETE_WINDOW", app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
