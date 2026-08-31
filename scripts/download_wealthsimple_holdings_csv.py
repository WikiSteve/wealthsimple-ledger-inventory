#!/usr/bin/env python3
"""Download a read-only Wealthsimple Holdings report for all accounts."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.download_wealthsimple_activity_csv import (
    attach_driver,
    click_all_accounts,
    click_testid,
    dismiss_modal_with_escape,
    new_csv,
    stamp,
    testid_is_visible,
    visible_modal_heading,
    wait_until,
)


DOCUMENTS_URL = "https://my.wealthsimple.com/app/docs"
GENERATE_BUTTON_TESTID = "row-custom-download"
TYPE_SELECTOR_TESTID = "button-generate-documents-type-selector"
HOLDINGS_OPTION_TESTID = "select-input-option-holdings-report-csv"
NEXT_BUTTON_TESTID = "button-holdings-report-next"
DOWNLOAD_BUTTON_TESTID = "generate-documents-cta-button"
CSV_REQUIRED_COLUMNS = {"Account Type", "Symbol", "Quantity", "Market Value"}


def validate_holdings_csv(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            columns = next(reader, [])
            rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"downloaded CSV is not UTF-8 text and cannot be a Holdings report: {exc}") from exc
    if not columns:
        raise RuntimeError("downloaded CSV is empty; no Holdings report was produced")
    missing = sorted(CSV_REQUIRED_COLUMNS - set(columns))
    if missing:
        raise RuntimeError(f"downloaded CSV is not a Holdings report; missing columns: {missing}")
    if not any(len(row) >= len(columns) and any(cell.strip() for cell in row[: len(columns)]) for row in rows):
        raise RuntimeError("downloaded CSV has the Holdings header but no position rows")
    return columns


def open_documents_page(driver) -> None:
    driver.get(DOCUMENTS_URL)
    if not wait_until(lambda: testid_is_visible(driver, GENERATE_BUTTON_TESTID), 45, 0.5):
        raise RuntimeError("Documents page did not expose the Generate document control within 45 seconds")


def download_holdings_csv(download_dir: Path) -> dict[str, Any]:
    download_dir.mkdir(parents=True, exist_ok=True)
    driver = attach_driver()
    driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
    before = set(download_dir.glob("*.csv"))
    receipt: dict[str, Any] = {"kind": "holdings", "account_scope": "all_accounts"}
    try:
        open_documents_page(driver)
        if not dismiss_modal_with_escape(driver):
            raise RuntimeError("a pre-existing modal would not close with Escape")
        click_testid(driver, GENERATE_BUTTON_TESTID, "Generate document")
        if not wait_until(lambda: visible_modal_heading(driver) == "Generate document", 5):
            raise RuntimeError("document export wizard did not open")
        click_testid(driver, TYPE_SELECTOR_TESTID)
        # The document-type list is a React popover.  It is normally quick,
        # but two seconds is not a reliable readiness contract on a waking
        # browser/profile; the option can appear just after that deadline.
        if not wait_until(lambda: testid_is_visible(driver, HOLDINGS_OPTION_TESTID), 12, 0.25):
            raise RuntimeError("document type choices did not open")
        click_testid(driver, HOLDINGS_OPTION_TESTID, "Holdings report (CSV)")
        click_testid(driver, NEXT_BUTTON_TESTID, "Next")
        if not wait_until(lambda: testid_is_visible(driver, DOWNLOAD_BUTTON_TESTID), 5):
            raise RuntimeError("Holdings report account step did not render")
        click_all_accounts(driver)
        if not wait_until(lambda: bool(driver.execute_script(
            "const button=document.querySelector('[data-testid=generate-documents-cta-button]'); return button && !button.disabled"
        )), 10, 0.25):
            raise RuntimeError("Holdings report Download CSV button did not enable")
        driver.save_screenshot(str(download_dir / "holdings-export-before-download.png"))
        click_testid(driver, DOWNLOAD_BUTTON_TESTID, "Download CSV")
        if not wait_until(lambda: new_csv(download_dir, before) is not None and not list(download_dir.glob("*.crdownload")), 30, 0.25):
            raise RuntimeError("Holdings CSV did not download within 30 seconds")
        csv_path = new_csv(download_dir, before)
        if csv_path is None:
            raise RuntimeError("Holdings CSV download could not be identified")
        receipt["csv_path"] = str(csv_path)
        receipt["columns"] = validate_holdings_csv(csv_path)
        receipt["downloaded_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return receipt
    finally:
        dismiss_modal_with_escape(driver)
        try:
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "default"})
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Wealthsimple Holdings CSV downloader")
    parser.add_argument("--download-dir", type=Path, default=Path(f"/tmp/wealthsimple-csv-downloads-{stamp()}"))
    args = parser.parse_args()
    try:
        receipt = download_holdings_csv(args.download_dir)
    except RuntimeError as exc:
        print(f"Holdings CSV download failed: {exc}", flush=True)
        return 1
    receipt_path = args.download_dir / "holdings-download-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    print(f"HOLDINGS_CSV={receipt['csv_path']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
