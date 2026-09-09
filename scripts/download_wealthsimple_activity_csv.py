#!/usr/bin/env python3
"""Download a read-only 12-month Wealthsimple Activity CSV from the attached browser.

This intentionally automates only the export wizard. It never opens a trade
ticket and never touches orders, transfers, settings, subscriptions, or account
features. The known wizard controls are checked by exact test id and label.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from selenium import webdriver

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.full_account_inventory import (
    ACTIVITY_EXPORT_FIELDS,
    analyze_activity_export,
    normalize_export_rows,
    read_wealthsimple_export,
    render_activity_export_analysis_md,
)
from src.browser_control_preflight import chromedriver_path, inspect_browser_control
from src.export_receipt import activity_download_proof


ACTIVITY_URL = "https://my.wealthsimple.com/app/activity"
ACTIVITY_READY_TIMEOUT_SECONDS = 45
ACTIVITY_LOAD_ATTEMPTS = 2
EXPORT_BUTTON_TESTID = "button-download-activities"
EXPORT_DIALOG_TESTID = "button-download-activities-modal"
PERIOD_SELECTOR_TESTID = "activities-export-period-selector"
NEXT_BUTTON_TESTID = "button-activities-export-next"
DOWNLOAD_BUTTON_TESTID = "generate-documents-cta-button"
CSV_REQUIRED_COLUMNS = {"account_type", "activity_type", "description"}
CSV_DATE_COLUMNS = {"transaction_date", "effective_at", "effective_date"}
AUDIT_RUNNER = ROOT / "scripts" / "run_full_inventory.py"


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def attach_driver() -> "webdriver.Chrome":
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    status = inspect_browser_control()
    if not status.ready:
        raise RuntimeError(status.message)
    options = Options()
    port = os.environ.get("BROWSER_CONTROL_PORT", "9223")
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    driver = webdriver.Chrome(service=Service(chromedriver_path()), options=options)
    # Foreground the automation tab on ITS display. With the virtual launcher
    # this cannot steal the operator's desktop focus, and React popovers paint.
    driver.execute_cdp_cmd("Page.bringToFront", {})
    return driver


def wait_until(probe, timeout: float, interval: float = 0.15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if probe():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(probe())
    except Exception:
        return False


def visible_export_dialog_heading(driver: "webdriver.Chrome") -> str | None:
    return driver.execute_script(
        r"""
        const dialog = document.querySelector('[data-testid="button-download-activities-modal"]');
        if (!dialog) return null;
        const r = dialog.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return null;
        const heading = dialog.querySelector('h1,h2,h3');
        return ((heading ? heading.innerText : dialog.innerText) || 'unnamed dialog')
          .replace(/\s+/g, ' ').trim().slice(0, 80);
        """
    )


def visible_modal_heading(driver: "webdriver.Chrome") -> str | None:
    """Return the top visible generic modal heading for the Documents wizard."""
    return driver.execute_script(
        r"""
        const dialog = Array.from(document.querySelectorAll('[role="dialog"][aria-modal="true"]'))
          .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
          .pop();
        if (!dialog) return null;
        const heading = dialog.querySelector('h1,h2,h3');
        return ((heading ? heading.innerText : dialog.innerText) || 'unnamed dialog')
          .replace(/\s+/g, ' ').trim().slice(0, 80);
        """
    )


ACTIVITY_READY_SCRIPT = r"""
const button = document.querySelector('[data-testid=button-download-activities]');
const rect = button ? button.getBoundingClientRect() : null;
const text = (document.body && document.body.innerText) || '';
const lower = text.toLowerCase();
return {
  usable: !!(button && rect && rect.width > 0 && rect.height > 0 && !button.disabled),
  text_length: text.length,
  login_wall: (lower.includes('password') && (lower.includes('log in') || lower.includes('welcome back'))),
};
"""


def activity_page_state(driver: "webdriver.Chrome") -> dict[str, Any]:
    try:
        return driver.execute_script(ACTIVITY_READY_SCRIPT) or {}
    except Exception:
        return {}


def activity_page_is_ready(driver: "webdriver.Chrome", seen: dict[str, int]) -> bool:
    state = activity_page_state(driver)
    if state.get("login_wall"):
        raise RuntimeError("a Wealthsimple login/MFA wall is visible; log in and re-run")
    if not state.get("usable"):
        seen["length"] = -1
        return False
    length = int(state.get("text_length") or 0)
    stable = length > 0 and length == seen.get("length")
    seen["length"] = length
    return stable


def open_activity_page(driver: "webdriver.Chrome") -> None:
    last: dict[str, Any] = {}
    for attempt in range(1, ACTIVITY_LOAD_ATTEMPTS + 1):
        if attempt == 1:
            driver.get(ACTIVITY_URL)
        else:
            driver.refresh()
        seen: dict[str, int] = {}
        if wait_until(lambda: activity_page_is_ready(driver, seen), ACTIVITY_READY_TIMEOUT_SECONDS, 0.5):
            return
        last = activity_page_state(driver)
        if attempt < ACTIVITY_LOAD_ATTEMPTS:
            print(f"Activity page did not finish hydrating within {ACTIVITY_READY_TIMEOUT_SECONDS:.0f}s (attempt {attempt}/{ACTIVITY_LOAD_ATTEMPTS}); refreshing and waiting again.", flush=True)
    raise RuntimeError(
        f"Activity page did not finish hydrating after {ACTIVITY_LOAD_ATTEMPTS} attempts "
        f"(export control usable: {last.get('usable')}, page text {last.get('text_length')} chars)"
    )


def dismiss_export_dialog_with_escape(driver: "webdriver.Chrome") -> bool:
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys

    if visible_export_dialog_heading(driver) is None:
        return True
    for _ in range(2):
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        if wait_until(lambda: visible_export_dialog_heading(driver) is None, 0.5):
            return True
    return False


def dismiss_modal_with_escape(driver: "webdriver.Chrome") -> bool:
    """Close a generic Documents-wizard modal without clicking an action."""
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.keys import Keys

    if visible_modal_heading(driver) is None:
        return True
    for _ in range(2):
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        if wait_until(lambda: visible_modal_heading(driver) is None, 0.5):
            return True
    return False


def click_testid(driver: "webdriver.Chrome", testid: str, expected_text: str | None = None) -> None:
    """Click a named export-wizard control after validating it is safe and visible."""
    clicked = driver.execute_script(
        """
        const id = arguments[0], expected = arguments[1];
        const el = document.querySelector(`[data-testid="${id}"]`);
        if (!el) return {ok:false, reason:'missing'};
        const r = el.getBoundingClientRect();
        const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
        if (r.width <= 0 || r.height <= 0) return {ok:false, reason:'not visible', text};
        if (el.disabled) return {ok:false, reason:'disabled', text};
        if (expected && text !== expected) return {ok:false, reason:'unexpected text', text};
        el.scrollIntoView({block:'center'});
        const c = el.getBoundingClientRect();
        const hit = document.elementFromPoint(c.left + c.width / 2, c.top + c.height / 2);
        if (!hit || !(el.contains(hit) || hit.contains(el))) return {ok:false, reason:'covered by another element', text};
        el.click();
        return {ok:true, text};
        """,
        testid,
        expected_text,
    )
    if not clicked or not clicked.get("ok"):
        raise RuntimeError(f"refused export-wizard control {testid}: {clicked}")


def testid_is_visible(driver: "webdriver.Chrome", testid: str) -> bool:
    return bool(driver.execute_script(
        """
        const el = document.querySelector(`[data-testid="${arguments[0]}"]`);
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
        """,
        testid,
    ))


def click_export_option(driver: "webdriver.Chrome", text: str) -> None:
    """Choose an exact option from the export period selector's linked listbox."""
    clicked = driver.execute_script(
        r"""
        const dialog = document.querySelector('[data-testid="button-download-activities-modal"]');
        if (!dialog) return {ok:false, reason:'export dialog missing'};
        const selector = dialog.querySelector('[data-testid="activities-export-period-selector"]');
        const listbox = selector && document.getElementById(selector.getAttribute('aria-controls'));
        if (!listbox) return {ok:false, reason:'export period listbox missing'};
        const expected = arguments[0];
        const el = Array.from(listbox.querySelectorAll('[role="option"]'))
          .find(e => (e.innerText || '').replace(/\s+/g, ' ').trim() === expected);
        if (!el) return {ok:false, reason:'option missing', expected};
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return {ok:false, reason:'option not visible', expected};
        el.click();
        return {ok:true};
        """,
        text,
    )
    if not clicked or not clicked.get("ok"):
        raise RuntimeError(f"refused export period option {text!r}: {clicked}")


def click_all_accounts(driver: "webdriver.Chrome", dialog_testid: str | None = None) -> None:
    """Select the visible All accounts checkbox; the audit parser ignores Chequing."""
    clicked = driver.execute_script(
        r"""
        const dialogTestid = arguments[0];
        const dialog = dialogTestid
          ? document.querySelector(`[data-testid="${dialogTestid}"]`)
          : Array.from(document.querySelectorAll('[role="dialog"][aria-modal="true"]'))
              .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
              .pop();
        if (!dialog) return {ok:false, reason:'dialog missing'};
        const el = Array.from(dialog.querySelectorAll('[role="checkbox"]'))
          .find(e => {
            const label = document.getElementById(e.getAttribute('aria-labelledby'))?.innerText || e.innerText || '';
            return /(^|\s)All accounts$/.test(label.replace(/\s+/g, ' ').trim());
          });
        if (!el) return {ok:false, reason:'All accounts checkbox missing'};
        if (el.getAttribute('aria-checked') === 'true') return {ok:true, already:true};
        el.click();
        return {ok:true, already:false};
        """
    , dialog_testid)
    if not clicked or not clicked.get("ok"):
        raise RuntimeError(f"refused account selector: {clicked}")
    if clicked.get("already"):
        return
    if not wait_until(lambda: bool(driver.execute_script(
        "const id=arguments[0]; const d=id?document.querySelector(`[data-testid=\"${id}\"]`):Array.from(document.querySelectorAll('[role=dialog][aria-modal=true]')).filter(x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0}).pop(); const e=d&&Array.from(d.querySelectorAll('[role=checkbox]')).find(x=>/(^|\\s)All accounts$/.test((document.getElementById(x.getAttribute('aria-labelledby'))?.innerText||x.innerText||'').replace(/\\s+/g,' ').trim())); return e&&e.getAttribute('aria-checked')==='true';",
        dialog_testid,
    )), 3):
        raise RuntimeError("account selector did not become checked; the export scope is not All accounts")


def new_csv(download_dir: Path, before: set[Path]) -> Path | None:
    candidates = [path for path in download_dir.glob("*.csv") if path not in before]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def validate_activity_csv(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"downloaded CSV is not UTF-8 text and cannot be an Activities export: {exc}") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("downloaded CSV is empty; no Activities export was produced")
    columns = [column.strip().strip('"') for column in lines[0].split(",")]
    missing = sorted(CSV_REQUIRED_COLUMNS - set(columns))
    if missing:
        raise RuntimeError(f"downloaded CSV is not an Activities export; missing columns: {missing}")
    if not CSV_DATE_COLUMNS.intersection(columns):
        raise RuntimeError(
            "downloaded CSV is not an Activities export; missing a supported date column "
            f"({sorted(CSV_DATE_COLUMNS)})"
        )
    if len(lines) < 2:
        raise RuntimeError("downloaded CSV has the Activities header but no activity rows; it must not be handed on as canonical settled activity")
    return columns


def download_activity_csv(download_dir: Path) -> dict[str, Any]:
    download_dir.mkdir(parents=True, exist_ok=True)
    driver = attach_driver()
    driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
    before = set(download_dir.glob("*.csv"))
    receipt: dict[str, Any] = {"kind": "activities", "period": "last_12_months", "account_scope": "all_accounts"}
    try:
        open_activity_page(driver)
        if not dismiss_export_dialog_with_escape(driver):
            raise RuntimeError("a pre-existing modal would not close with Escape")
        click_testid(driver, EXPORT_BUTTON_TESTID, "Download activities")
        if not wait_until(lambda: visible_export_dialog_heading(driver) == "Download activities", 5):
            raise RuntimeError("Activity export wizard did not open")
        click_testid(driver, PERIOD_SELECTOR_TESTID)
        if not wait_until(lambda: bool(driver.execute_script(
            "const d=document.querySelector('[data-testid=button-download-activities-modal]'); const s=d?.querySelector('[data-testid=activities-export-period-selector]'); const l=s&&document.getElementById(s.getAttribute('aria-controls')); return Array.from(l?.querySelectorAll('[role=option]')||[]).some(e=>(e.innerText||'').trim()==='Last 12 months');"
        )), 2):
            raise RuntimeError("Activity export period choices did not open")
        click_export_option(driver, "Last 12 months")
        click_testid(driver, NEXT_BUTTON_TESTID, "Next")
        if not wait_until(lambda: bool(driver.execute_script(
            "return document.querySelector('[data-testid=generate-documents-cta-button]')")), 5):
            raise RuntimeError("Activity export account step did not render")
        click_all_accounts(driver, EXPORT_DIALOG_TESTID)
        if not wait_until(lambda: bool(driver.execute_script(
            "const button=document.querySelector('[data-testid=generate-documents-cta-button]'); return button && !button.disabled"
        )), 10, 0.25):
            raise RuntimeError("Activity export Download CSV button did not enable")
        driver.save_screenshot(str(download_dir / "activity-export-before-download.png"))
        click_testid(driver, DOWNLOAD_BUTTON_TESTID, "Download CSV")
        if not wait_until(lambda: new_csv(download_dir, before) is not None and not list(download_dir.glob("*.crdownload")), 30, 0.25):
            raise RuntimeError("Activities CSV did not download within 30 seconds")
        csv_path = new_csv(download_dir, before)
        if csv_path is None:
            raise RuntimeError("Activities CSV download could not be identified")
        receipt["csv_path"] = str(csv_path)
        receipt["columns"] = validate_activity_csv(csv_path)
        receipt.update(activity_download_proof(csv_path))
        receipt["downloaded_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return receipt
    finally:
        # Never click a dialog button. Escape is safe cleanup for an error or
        # a wizard left open after download.
        dismiss_export_dialog_with_escape(driver)
        try:
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "default"})
        except Exception:
            pass


def audit_command_for_activity_export(csv_path: Path, mode: str = "FULL", holdings_csv: Path | None = None,
                                      activity_receipt: Path | None = None) -> list[str]:
    """Command that hands the preserved CSV to the standard audit collector."""
    if mode not in {"FAST", "FULL"}:
        raise ValueError(f"unsupported audit mode: {mode}")
    command = [sys.executable, str(AUDIT_RUNNER), "--mode", mode, "--activity-export", str(csv_path)]
    if holdings_csv is not None:
        command += ["--holdings-export", str(holdings_csv)]
    if activity_receipt is not None:
        command += ["--activity-download-receipt", str(activity_receipt)]
    return command


def write_activity_analysis(csv_path: Path, output_dir: Path) -> dict[str, str]:
    """Parse a downloaded export into a privacy-minimized report for ChatGPT."""
    rows, as_of = read_wealthsimple_export(csv_path)
    analysis = analyze_activity_export(normalize_export_rows(rows, ACTIVITY_EXPORT_FIELDS), as_of=as_of)
    json_path = output_dir / "activity-export-analysis.json"
    markdown_path = output_dir / "activity-export-analysis.md"
    json_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_activity_export_analysis_md(analysis), encoding="utf-8")
    return {"analysis_json": str(json_path), "analysis_markdown": str(markdown_path), "export_as_of": as_of or "not stated in the export", "export_coverage": analysis["export_coverage"], "rows_outside_target_year": analysis["rows_outside_target_year"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Wealthsimple 12-month Activity CSV downloader")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--activities-12-months", action="store_true", help="download the Activity CSV for all accounts")
    source.add_argument("--analyze-existing", type=Path, help="parse an already-downloaded Activity CSV without opening the browser")
    parser.add_argument("--download-dir", type=Path, default=Path(f"/tmp/wealthsimple-csv-downloads-{stamp()}"))
    parser.add_argument("--run-audit", action="store_true", help="after validation, start the standard read-only audit with this CSV attached")
    parser.add_argument("--audit-mode", choices=["FAST", "FULL"], default="FULL")
    parser.add_argument("--holdings-export", type=Path, help="optional holdings CSV to attach to the --run-audit capture")
    parser.add_argument("--download-holdings", action="store_true", help="also download a fresh all-accounts Holdings CSV and attach it to the audit")
    args = parser.parse_args()
    if args.download_holdings and args.analyze_existing:
        parser.error("--download-holdings requires --activities-12-months")
    if args.download_holdings and args.holdings_export:
        parser.error("choose either --download-holdings or --holdings-export, not both")
    if args.analyze_existing:
        csv_path = args.analyze_existing.expanduser()
        if not csv_path.is_file():
            parser.error(f"Activity CSV does not exist: {csv_path}")
        args.download_dir.mkdir(parents=True, exist_ok=True)
        receipt = {"kind": "activities", "csv_path": str(csv_path), "analysis_only": True}
    else:
        try:
            receipt = download_activity_csv(args.download_dir)
        except RuntimeError as exc:
            print(f"Activity CSV download failed: {exc}", file=sys.stderr, flush=True)
            return 1
    receipt.update(write_activity_analysis(Path(receipt["csv_path"]), args.download_dir))
    holdings_csv = args.holdings_export
    if args.download_holdings:
        from scripts.download_wealthsimple_holdings_csv import download_holdings_csv

        print("Downloading a fresh all-accounts Holdings CSV for independent position control.", flush=True)
        try:
            holdings_receipt = download_holdings_csv(args.download_dir)
        except RuntimeError as exc:
            print(f"Holdings CSV download failed: {exc}", file=sys.stderr, flush=True)
            return 1
        receipt["holdings_export"] = holdings_receipt
        holdings_csv = Path(holdings_receipt["csv_path"])
    receipt_path = args.download_dir / "activity-download-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    print(f"ACTIVITY_CSV={receipt['csv_path']}", flush=True)
    if holdings_csv is not None:
        print(f"HOLDINGS_CSV={holdings_csv}", flush=True)
    if args.run_audit:
        command = audit_command_for_activity_export(Path(receipt["csv_path"]), args.audit_mode, holdings_csv,
                                                   receipt_path if not receipt.get("analysis_only") else None)
        print("Starting standard read-only audit with fresh Activity and Holdings CSV controls attached.", flush=True)
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
