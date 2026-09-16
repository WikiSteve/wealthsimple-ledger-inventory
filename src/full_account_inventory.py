from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import time
import traceback
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .browser_control_preflight import chromedriver_path, inspect_browser_control
from .export_receipt import validate_activity_receipt
from .activity_filter_evidence import (
    FILTER_GROUPS, FILTER_SNAPSHOT_SCRIPT, FILTER_CLEAR_CLICK_SCRIPT,
    CLEAR_LABEL_CANONICAL, clear_label_matches, confirms_unfiltered,
)
from .inventory_completeness import (
    assess_inventory_completeness, classify_status_token_free_rows,
    derive_sell_coverage_scope, format_residual_warning, make_count_observation,
    parse_broker_pending_count, residual_disclosure_rows,
)
from .order_metadata import METADATA_FUNCTION, validate_metadata, enrich_order
from .deposit_evidence import (parse_deposit_availability, render_deposits,
                               compare_deposit_residuals, render_deposit_comparisons)
from .sell_coverage import (
    OPEN_STATUSES, open_status_from_lines, order_state, quantity_evidence,
    sell_coverage, render_coverage, safe_quantity_record,
)


ACCOUNTS = ["TFSA", "RRSP", "Non-registered"]
ACCOUNT_ALIASES = {
    "TFSA": ["TFSA"],
    "RRSP": ["RRSP"],
    "Non-registered": ["Non-registered", "Unregistered", "Cash"],
}
ACCOUNT_SLUG = {"TFSA": "tfsa", "RRSP": "rrsp", "Non-registered": "nonregistered"}
ORDER_ACTIONS = {
    "Limit buy", "Limit sell", "Market buy", "Market sell",
    "Fractional buy", "Fractional sell", "Stop buy", "Stop sell",
    "Stop limit buy", "Stop limit sell",
}
ACTIVITY_ACTIONS = ORDER_ACTIONS | {
    "Dividend", "Transfer", "Deposit", "Withdrawal", "Electronic funds transfer",
    "Interac e-Transfer", "FX conversion", "Fee", "Tax withholding",
}
ACTIVITY_FINAL_STATUSES = {"Completed", "Filled", "Cancelled", "Expired", "Rejected", "Failed"}
DANGEROUS_CLICK_TEXT = {
    "Buy",
    "Sell",
    "Cancel order",
    "Modify order",
    "Submit order",
    "Place order",
    "Review order",
    "Queue order",
    "Confirm",
    "Transfer",
    "Transfer money",
    "Add money",
    "Move money",
    "Deposit",
    "Withdraw",
    # Live account pages render Convert money / Preview controls next to the
    # cash panel this collector reads, so the click guard must name them.
    "Convert",
    "Convert money",
    "Preview",
    "Preview order",
    "Sell all",
    "Withdraw money",
}
LOGIN_FORM_PATTERNS = {
    "Email address",
    "Password",
    "Two-step verification",
    "verification code",
    "Enter the code",
}
FORBIDDEN_STATE_PATTERNS = {
    "Review order",
    "Submit order",
    "Place order",
    "Queue order",
    "Confirm order",
    "Preview order",
    "Swipe to submit",
}
SAFE_DETAIL_CLOSE_TEXT = {"Close", "Close dialog", "Close order details"}
SPECIAL_TICKERS = {
    "NOC", "QCOM", "CVS", "UPS", "TD", "ADBE", "CRM", "SPCX", "RKLB", "MAXQ", "MDA",
    "AVGO", "GOOG", "META", "MSFT", "NVDA", "INTC", "TSLA", "LUN", "FM", "MP",
    "F", "SPIR", "UFO", "BN", "BAM",
}
# Every settle budget below is the wait this code already took unconditionally.
# Polling can only finish sooner, never later, so worst-case timing is unchanged.
POLL_INTERVAL_SECONDS = 0.15
APP_NAVIGATION_SETTLE_SECONDS = 2.5
EXTERNAL_NAVIGATION_SETTLE_SECONDS = 3.0
ACCOUNT_CARD_SETTLE_SECONDS = 2.2
CONTROL_CLICK_SETTLE_SECONDS = 1.8
ACTIVITY_FILTER_SETTLE_SECONDS = 1.5
LOAD_MORE_SETTLE_SECONDS = 1.2
# Wealthsimple's current Activity view is an accordion: opening one Pending
# disclosure collapses another. Parallel opening therefore cannot produce a
# complete snapshot. Keep the experimental batch implementation, but make the
# bounded serial path the production default.
PENDING_DETAIL_BATCH_SIZE = 1
PENDING_DETAIL_BATCH_TIMEOUT_SECONDS = 4.0
PAGE_INTERACTIVE_SCRIPT = (
    "return document.readyState === 'complete' && !!document.body "
    "&& (document.body.innerText || '').trim().length > 200;"
)
# The heading lookup used to run document.querySelectorAll('h3') inside the
# per-element map, so a page with E controls and H headings cost E full-document
# queries and E x H position comparisons on every inventory pass. Hoisting the
# query, caching heading text once, and scanning headings backwards with an
# early exit produces byte-identical output for far less work.
CONTROLS_SCRIPT = """
    const headings = Array.from(document.querySelectorAll('h3'));
    const headingText = headings.map(h => (h.innerText || '').trim());
    const dateContextFor = (el) => {
      for (let i = headings.length - 1; i >= 0; i--) {
        if (headings[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
          return headingText[i];
        }
      }
      return '';
    };
    return Array.from(document.querySelectorAll('a,button,[role="button"],[role="menuitem"],input,select,textarea'))
      .map((el) => {
        const tag = (el.tagName || '').toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        let value = '';
        if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) {
          value = el.getAttribute('value') || '';
        }
        return {
          tag,
          role: el.getAttribute('role'),
          type: type || el.getAttribute('type'),
          text: ((el.innerText || el.getAttribute('aria-label') || value || '').trim()).slice(0, 3000),
          href: el.href || el.getAttribute('href'),
          id: el.id || null,
          date_context: dateContextFor(el)
        };
      });
"""
# The detail loops need both the full body (evidence plus strict safety scan)
# and the opened card's exact region (parser input). Read both from the same
# already-opened DOM snapshot rather than making separate WebDriver round
# trips for every order/activity detail.
ACTIVITY_DETAIL_SNAPSHOT_SCRIPT = METADATA_FUNCTION + """
    const wanted = arguments[0];
    const controlId = arguments[1];
    const exact = controlId ? document.getElementById(controlId) : null;
    const wantedParts = wanted.split('\\n').map(part => part.trim()).filter(Boolean);
    const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
      .filter(el => {
        const lines = (el.innerText || '').split('\\n').map(part => part.trim());
        return wantedParts.every(part => lines.includes(part))
          && el.getAttribute('aria-expanded') === 'true';
      });
    const exactText = candidates.find(el => {
      const lines = (el.innerText || '').split('\\n').map(part => part.trim()).filter(Boolean);
      const r = el.getBoundingClientRect();
      return lines.join('\\n') === wantedParts.join('\\n') && r.width > 0 && r.height > 0;
    });
    // React may recycle a virtualized card and replace its DOM id between the
    // discovery pass and this read. The Pending-filter view can also expose a
    // duplicate accessibility wrapper for the same visible card. Selecting
    // the first rendered disclosure is safe (read-only) and mirrors the
    // benchmarked interaction; order matching below still fails closed on
    // incomplete or non-matching detail.
    const header = exact && exact.matches('button,[role="button"]')
      && exact.hasAttribute('aria-controls') && exact.getAttribute('aria-expanded') === 'true'
      ? exact : (exactText || candidates.find(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }) || null);
    const region = header && header.hasAttribute('aria-controls')
      ? document.getElementById(header.getAttribute('aria-controls')) : null;
    // Identity metadata needs a unique EXACT text match, not the permissive
    // visible-text parser fallback above. A recycled id alone is insufficient.
    const metadataHeaders = candidates.filter(el => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0
        && (el.innerText || '').split('\\n').map(p => p.trim()).filter(Boolean).join('\\n')
          === wantedParts.join('\\n');
    });
    let metadata;
    try { metadata = orderMetadata(metadataHeaders.length === 1 ? metadataHeaders[0] : null); }
    catch (_) { metadata = {reason: 'metadata_read_failed'}; }
    return {
      order_metadata: metadata,
      body_text: document.body ? (document.body.innerText || '') : '',
      // The current Activity accordion no longer supplies aria-controls.
      // Only one card may be expanded, so the full body is then the exact
      // safe parser scope after the header has been positively identified.
      detail_text: region ? (region.innerText || '') : (header && document.body ? (document.body.innerText || '') : ''),
      url: window.location.href,
      title: document.title || ''
    };
"""
ACTIVITY_DETAIL_BATCH_SCRIPT = """
    const activeStatuses = __OPEN_STATUSES__;
    const rows = arguments[0];
    const timeoutMs = arguments[1];
    const done = arguments[arguments.length - 1];
    const required = [
      'Account', 'Status', 'Submitted', 'Expires',
      'Trading session', 'Type', 'Entered quantity'
    ];
    const selected = rows.map(row => {
      const header = document.getElementById(row.source_control_id);
      const parts = header ? (header.innerText || '').split('\\n').map(value => value.trim()) : [];
      if (!header || !header.matches('button,[role="button"]') ||
          !header.hasAttribute('aria-controls') ||
          header.getAttribute('aria-expanded') !== 'false' ||
          !parts.some(value => activeStatuses.includes(value.toLowerCase()))) {
        return null;
      }
      return {row, header};
    });
    if (selected.some(item => !item)) {
      done({ok: false, reason: 'one or more Pending disclosure headers were not rendered'});
      return;
    }
    selected.forEach(item => item.header.click());
    const started = performance.now();
    const poll = () => {
      const details = selected.map(item => {
        const region = document.getElementById(item.header.getAttribute('aria-controls'));
        const detailText = region ? (region.innerText || '') : '';
        return {
          stable_row_key: item.row.stable_row_key,
          source_control_id: item.row.source_control_id,
          detail_text: detailText,
          ready: required.every(field => detailText.includes(field)) &&
            detailText.includes('Estimated total')
        };
      });
      if (details.every(detail => detail.ready)) {
        done({
          ok: true,
          browser_wait_ms: performance.now() - started,
          details,
          body_text: document.body ? (document.body.innerText || '') : '',
          url: window.location.href,
          title: document.title || ''
        });
        return;
      }
      if (performance.now() - started >= timeoutMs) {
        done({
          ok: false,
          reason: 'batched Pending details did not become complete before timeout',
          browser_wait_ms: performance.now() - started,
          details
        });
        return;
      }
      setTimeout(poll, 10);
    };
    poll();
""".replace("__OPEN_STATUSES__", json.dumps(sorted(OPEN_STATUSES)))
ACTIVITY_DETAIL_BATCH_CLOSE_SCRIPT = """
    const controlIds = new Set(arguments[0]);
    return Array.from(document.querySelectorAll('button[aria-controls][aria-expanded="true"]'))
      .filter(header => controlIds.has(header.id))
      .map(header => {
        // These exact disclosure IDs were opened by this batch. They may have
        // filled since opening; cleanup must not depend on the old status.
        header.click();
        return true;
      });
"""
CURRENT_YEAR_ACTIVITY_JSON = "recent-activity-current-year.json"
CURRENT_YEAR_ACTIVITY_CSV = "recent-activity-current-year.csv"
CURRENT_YEAR_FILLS_JSON = "fills-and-cancels-current-year.json"
IGNORED_NON_INVESTING_ACCOUNT_MARKERS = ("cash-msb", "chequing")
# Wealthsimple prints a TSX listing suffix on some Canadian tickers (MDA.TO,
# BN.TO, BAM.TO, TD.TO) but not others (T, BTO, LUN). Cross-referencing
# holdings against orders therefore needs suffix-insensitive matching. `.A`
# and `.B` are share classes (CTC.A), not exchanges, and must never be dropped.
EXCHANGE_TICKER_SUFFIXES = (".TO", ".TSX", ".V", ".NE", ".CN")
HOLDINGS_ROW_TESTID_PREFIX = "holdings-row-"
# Consecutive rowless-but-rendered observations before an account is accepted
# as genuinely holding no positions.
EMPTY_HOLDINGS_GRID_CONFIRMATIONS = 4
HOLDINGS_GRID_STABLE_CONFIRMATIONS = 4
HOLDINGS_DASHBOARD_TIMEOUT_SECONDS = 15.0
# Wealthsimple's published maximum currency-conversion fee for self-directed
# trade accounts (1.5%; tiered lower above C$10,000, and zero for US-listed
# trades funded from a USD account). Used only as an upper bound when testing
# whether an FX spread could explain a reconciliation residual - never charged,
# inferred, or added to any figure.
# https://www.wealthsimple.com/en-ca/legal/fees/trade
MAX_DOCUMENTED_FX_CONVERSION_FEE = 0.015
TOTAL_CASH_TOOLTIP_PHRASE = "This is a combination of your CAD and USD balances"
TOTAL_CASH_TOOLTIP_TIMEOUT = 3.0
HOLDINGS_TABLE_HEADER_LABELS = {
    "Holdings", "Currency", "Allocation", "Acc. allocation", "Quantity",
    "Price", "Total value", "Account", "All-time return",
}
HOLDINGS_TABLE_STOP_MARKERS = {
    "Recent activity", "Watchlist", "Total cash available", "Scheduled activities",
    "Keep your portfolio on track", "View all",
    "Trade is offered by Wealthsimple Investments Inc. (WSII).",
}
USER_ACCOUNT_CONTEXT_PATH = Path(
    os.environ.get(
        "WEALTHSIMPLE_ACCOUNT_CONTEXT",
        Path.home() / ".config" / "wealthsimple-ledger-inventory" / "user-account-context.json",
    )
)


@dataclass
class RunState:
    out_dir: Path
    mode: str
    started: float = field(default_factory=time.monotonic)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    click_log: list[dict[str, Any]] = field(default_factory=list)
    safety_log: list[dict[str, Any]] = field(default_factory=list)
    performance: dict[str, dict[str, float | int]] = field(default_factory=dict)
    account_readiness_traces: list[dict[str, Any]] = field(default_factory=list)
    rebuilt_from: str | None = None
    source_capture_generated_at: str | None = None
    pending_scan_complete: bool = False
    deposit_availability: list[dict[str, Any]] = field(default_factory=list)
    # Evidence for inventory completeness. Actions are provenance; states are evidence.
    filter_reset_attempted: bool = False
    filter_reset_click_succeeded: bool = False
    filter_observed_default_before: bool | None = None
    filter_observed_default_after: bool | None = None
    filter_state_path: str | None = None
    traversal_exhausted: bool = False
    unparsed_pending_controls: list[str] = field(default_factory=list)
    broker_pending_count_before: dict[str, Any] | None = None
    broker_pending_count_after: dict[str, Any] | None = None
    inventory_completeness: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        if self.blockers:
            return "ERROR"
        if self.warnings:
            return "WARN"
        return "OK"

    def log_click(self, label: str, href: str | None = None, *, blocked: bool = False) -> None:
        event = {"at": iso_now(), "label": label, "href": href, "blocked": blocked}
        self.click_log.append(event)
        if blocked:
            self.safety_log.append({"at": iso_now(), "event": "blocked_click", "label": label, "href": href})

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def block(self, message: str) -> None:
        if message not in self.blockers:
            self.blockers.append(message)

    def accumulate(self, name: str, elapsed_seconds: float, count: int = 1) -> None:
        """Add to a running total so repeated small operations become visible."""
        row = self.performance.setdefault(name, {"elapsed_seconds": 0.0, "count": 0})
        row["elapsed_seconds"] = round(float(row["elapsed_seconds"]) + elapsed_seconds, 3)
        row["count"] = int(row["count"]) + count
        if row["count"]:
            row["milliseconds_per_item"] = round((float(row["elapsed_seconds"]) * 1000) / row["count"], 2)

    def metric(self, name: str, elapsed_seconds: float, count: int = 0) -> None:
        row: dict[str, float | int] = {"elapsed_seconds": round(elapsed_seconds, 3), "count": count}
        if count:
            row["milliseconds_per_item"] = round((elapsed_seconds * 1000) / count, 2)
        self.performance[name] = row


def wait_until(
    probe: Any, timeout: float, interval: float = POLL_INTERVAL_SECONDS,
    clock: Any = time.monotonic, sleeper: Any = time.sleep,
) -> bool:
    """Poll ``probe`` until it is truthy, never waiting longer than ``timeout``.

    Replaces a fixed sleep with the same upper bound: the caller cannot wait
    longer than it used to, and usually returns as soon as the page is usable.
    A probe that raises is treated as "not ready yet" so a transient browser
    error never aborts a capture.
    """
    deadline = clock() + timeout
    while True:
        try:
            if probe():
                return True
        except Exception:
            pass
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(interval, remaining))


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stamp_now() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")[:140] or "capture"


def money_to_float(value: str | None) -> float:
    if not value:
        return 0.0
    return float(re.sub(r"[^0-9.-]", "", value.replace("−", "-")) or 0)


def value_currency(value: str | None) -> str | None:
    """Return the currency an amount settles in.

    Wealthsimple writes the settlement currency as the trailing token
    ("$251.84 CAD"). When a string carries more than one currency token, the
    last one is the amount's own currency and any earlier token is a quote
    reference, so returning the first match would mislabel the settlement
    currency.
    """
    if not value:
        return None
    tokens = re.findall(r"\b(CAD|USD)\b", value)
    return tokens[-1] if tokens else None


def looks_like_money(value: str | None) -> bool:
    """True only for a fully rendered money string, not a label or skeleton.

    The live account page paints "Total cash available" and "Available CAD"
    before their values, so a label-then-next-line read can capture the next
    label as a balance unless the value shape is checked.
    """
    if not value:
        return False
    # This predicate is also a disclosure-evidence gate: a complete collapsed
    # row may skip expansion. Keep the grammar intentionally strict so a
    # loosely money-like label cannot suppress required detail capture.
    return bool(re.match(r"^[-\u2212]?\s?\$[0-9][0-9,]*(\.[0-9]{2})?(\s+(CAD|USD))?$", value.strip()))


def normalize_ticker_for_cross_reference(ticker: str | None) -> str | None:
    """Strip an exchange suffix only, so holdings and orders cross-reference.

    `.TO`/`.V`/`.NE`/`.CN` identify a listing venue and are printed
    inconsistently by Wealthsimple. A share-class suffix such as CTC.A is part
    of the security identity and is preserved.
    """
    if not ticker:
        return ticker
    upper = ticker.strip().upper()
    for suffix in EXCHANGE_TICKER_SUFFIXES:
        if upper.endswith(suffix) and len(upper) > len(suffix):
            return upper[: -len(suffix)]
    return upper


def row_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:24]


def ensure_dirs(out_dir: Path) -> None:
    for root in ["screenshots", "dom", "visible-text"]:
        for section in ["account-overview", "tfsa", "rrsp", "nonregistered", "order-details", "activity", "holdings"]:
            (out_dir / root / section).mkdir(parents=True, exist_ok=True)
    for child in ["raw-exports", "scripts", "logs"]:
        (out_dir / child).mkdir(parents=True, exist_ok=True)


def attach_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    status = inspect_browser_control()
    if not status.ready:
        raise RuntimeError(status.message)
    opts = Options()
    port = os.environ.get("BROWSER_CONTROL_PORT", "9223")
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    return webdriver.Chrome(service=Service(chromedriver_path()), options=opts)


class WealthsimpleReader:
    def __init__(
        self,
        state: RunState,
        expected_security_holdings: dict[str, int] | None = None,
    ):
        self.state = state
        self.expected_security_holdings = expected_security_holdings or {}
        self.driver = attach_driver()

    def settle(self, probe: Any, budget: float, label: str) -> bool:
        """Wait for a page signal, bounded by the fixed sleep this replaced."""
        started = time.monotonic()
        ready = wait_until(probe, budget)
        self.state.accumulate(f"settle:{label}", time.monotonic() - started)
        return ready

    def page_is_interactive(self) -> bool:
        return bool(self.driver.execute_script(PAGE_INTERACTIVE_SCRIPT))

    def disclosure_controls_present(self) -> bool:
        return bool(self.driver.execute_script(
            "return document.querySelectorAll('[aria-controls]').length > 0;"))

    def activity_filter_sidebar_settled(self) -> bool:
        """Edge-triggered: targeted groups expanded and control count stable across polls."""
        result = self.driver.execute_script(
            """
            const names = arguments[0];
            const search = document.querySelector('[data-testid="filter-search"]');
            let root = search;
            while (root && !(root.innerText || '').trim().startsWith('Filters')) root = root.parentElement;
            if (!root) return {ok:false, count:0};
            const buttons = Array.from(root.querySelectorAll('button'));
            const expanded = names.every(name => {
              const matches = buttons.filter(b => b.innerText.trim() === name);
              return matches.length === 1 && matches[0].getAttribute('aria-expanded') === 'true';
            });
            const count = root.querySelectorAll('input[type="checkbox"], input[type="radio"], [aria-pressed]').length;
            return {ok: expanded, count};
            """,
            list(FILTER_GROUPS),
        ) or {"ok": False, "count": 0}
        if not result.get("ok"):
            self._filter_settle_count = None
            return False
        count = int(result.get("count") or 0)
        previous = getattr(self, "_filter_settle_count", None)
        self._filter_settle_count = count
        return previous is not None and previous == count and count > 0

    def click_activity_filter_clear(self, label: str = "Clear") -> bool:
        """Click Clear/Clear-all only inside the Activity filter sidebar."""
        if label in DANGEROUS_CLICK_TEXT:
            self.state.log_click(label, blocked=True)
            return False
        # Try canonical clear labels inside the sidebar only.
        labels = [label] if label else list(CLEAR_LABEL_CANONICAL)
        if label == "Clear":
            labels = list(CLEAR_LABEL_CANONICAL)
        for candidate in labels:
            clicked = self.driver.execute_script(FILTER_CLEAR_CLICK_SCRIPT, candidate, True)
            if clicked and clicked.get("ok"):
                self.state.log_click(f"activity-filter-reset:{clicked.get('text') or candidate}", clicked.get("href"))
                self.settle(self.page_is_interactive, CONTROL_CLICK_SETTLE_SECONDS, "control-click")
                return True
        return False

    def body_text(self) -> str:
        return self.driver.execute_script("return document.body ? document.body.innerText : '';") or ""

    def controls(self) -> list[dict[str, Any]]:
        # Selenium's per-element `.text` calls become extremely slow on the
        # virtualized Activity page after many rows are loaded. Pull the safe
        # control inventory in one browser-side pass instead. Do not persist
        # free-form input values.
        started = time.monotonic()
        found = self.driver.execute_script(CONTROLS_SCRIPT)
        self.state.accumulate("dom_control_inventory", time.monotonic() - started)
        return found

    def capture(self, section: str, name: str, *, strict_state_scan: bool = False, screenshot: bool = True, include_controls: bool = True) -> dict[str, str]:
        self.close_support_chat()
        self.clear_blocking_modal()
        return self._write_capture_evidence(
            section,
            name,
            self.body_text(),
            url=redact_account_ids_in_url(self.driver.current_url),
            title=self.driver.title,
            controls=self.controls() if include_controls else [],
            strict_state_scan=strict_state_scan,
            screenshot=screenshot,
        )

    def _write_capture_evidence(
        self,
        section: str,
        name: str,
        text: str,
        *,
        url: str,
        title: str,
        controls: list[dict[str, Any]],
        strict_state_scan: bool,
        screenshot: bool,
    ) -> dict[str, str]:
        text_dir = self.state.out_dir / "visible-text" / section
        dom_dir = self.state.out_dir / "dom" / section
        png_dir = self.state.out_dir / "screenshots" / section
        text_dir.mkdir(parents=True, exist_ok=True)
        dom_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)
        text_path = text_dir / f"{safe_name(name)}.txt"
        dom_path = dom_dir / f"{safe_name(name)}.json"
        png_path = png_dir / f"{safe_name(name)}.png"
        text = text + "\n"
        url = redact_account_ids_in_url(url)
        controls = [
            {**control, "href": redact_account_ids_in_url(control.get("href"))}
            if isinstance(control, dict) and control.get("href") else control
            for control in (controls or [])
        ]
        text_path.write_text(text, encoding="utf-8")
        dom_path.write_text(json.dumps({
            "url": url,
            "title": title,
            "controls": controls,
        }, indent=2) + "\n", encoding="utf-8")
        screenshot_reference = ""
        if screenshot:
            try:
                self.driver.save_screenshot(str(png_path))
                screenshot_reference = str(png_path)
            except Exception as exc:
                self.state.warn(f"screenshot failed for {section}/{name}: {exc!r}")
        # str(Path("")) is ".", which looks like a real relative path in
        # the ledger even though detail captures intentionally have no PNG.
        self.scan_state(text, strict=strict_state_scan, evidence=str(text_path))
        return {"visible_text": str(text_path), "dom": str(dom_path), "screenshot": screenshot_reference, "url": url}

    def capture_open_activity_detail(
        self,
        section: str,
        name: str,
        row_text: str,
        source_control_id: str | None = None,
    ) -> tuple[dict[str, str], str]:
        """Persist full evidence and parser input from one opened Activity card.

        The fallback preserves the old capture/extract sequence if a browser
        snapshot fails, so an optimization failure cannot silently lose detail
        evidence or downgrade a pending order.
        """
        self.close_support_chat()
        self.clear_blocking_modal()
        try:
            started = time.monotonic()
            snapshot = self.driver.execute_script(
                ACTIVITY_DETAIL_SNAPSHOT_SCRIPT, row_text, source_control_id
            )
            self.state.accumulate("activity_detail_snapshot", time.monotonic() - started)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("body_text"), str):
                raise RuntimeError("Activity detail snapshot returned no readable body text")
            evidence = self._write_capture_evidence(
                section,
                name,
                snapshot["body_text"],
                url=redact_account_ids_in_url(str(snapshot.get("url") or self.driver.current_url)),
                title=str(snapshot.get("title") or self.driver.title),
                controls=[],
                strict_state_scan=True,
                screenshot=False,
            )
            metadata = validate_metadata(snapshot.get("order_metadata"))
            metadata["observed_at"] = iso_now()
            metadata_path = Path(evidence["visible_text"]).with_suffix(".order-metadata.json")
            metadata["evidence_file"] = str(metadata_path)
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            evidence["order_metadata"] = metadata
            return evidence, str(snapshot.get("detail_text") or "")
        except Exception as exc:
            # Evidence is recovered below. Keep the per-occurrence fact in
            # safety data, but do not drown the user-facing warnings when a
            # transient browser problem affects a long detail pass.
            self.state.accumulate("activity_detail_snapshot_fallback", 0.0)
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "activity_detail_snapshot_fallback",
                "section": section,
                "detail": type(exc).__name__,
            })
            self.state.warn(
                "activity detail snapshot fell back to separate evidence reads for at least one detail; "
                "full evidence was still captured"
            )
            evidence = self.capture(section, name, strict_state_scan=True, screenshot=False, include_controls=False)
            return evidence, self.expanded_activity_detail_text(
                row_text, source_control_id
            )

    def capture_open_activity_detail_batch(
        self,
        section: str,
        rows: list[dict[str, Any]],
        names: dict[str, str],
    ) -> dict[str, tuple[dict[str, str], str]]:
        """Open rendered Pending disclosures concurrently and snapshot them.

        Each target is pinned to the current document's exact header ID and
        must still be a collapsed Pending disclosure before it is clicked.
        Missing or incomplete rows are omitted so the caller can recover them
        through the established serial path.
        """
        specs = [
            {
                "stable_row_key": row.get("stable_row_key"),
                "source_control_id": row.get("source_control_id"),
            }
            for row in rows
            if row.get("stable_row_key") and row.get("source_control_id")
        ]
        if len(specs) != len(rows):
            return {}
        self.close_support_chat()
        if not self.clear_blocking_modal():
            return {}
        control_ids = [str(spec["source_control_id"]) for spec in specs]
        started = time.monotonic()
        result: dict[str, Any] | None = None
        cleanup_confirmed = False
        try:
            self.driver.set_script_timeout(PENDING_DETAIL_BATCH_TIMEOUT_SECONDS + 1.0)
            result = self.driver.execute_async_script(
                ACTIVITY_DETAIL_BATCH_SCRIPT,
                specs,
                int(PENDING_DETAIL_BATCH_TIMEOUT_SECONDS * 1000),
            )
            self.state.accumulate("activity_detail_batch", time.monotonic() - started)
            if not isinstance(result, dict) or not result.get("ok"):
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "activity_detail_batch_fallback",
                    "reason": (
                        result.get("reason") if isinstance(result, dict)
                        else "batch script returned no result"
                    ),
                    "requested": len(rows),
                })
                return {}
            details = result.get("details")
            body_text = result.get("body_text")
            if not isinstance(details, list) or not isinstance(body_text, str):
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "activity_detail_batch_fallback",
                    "reason": "batch snapshot was structurally incomplete",
                    "requested": len(rows),
                })
                return {}
            detail_by_key = {
                detail.get("stable_row_key"): detail
                for detail in details
                if isinstance(detail, dict)
                and detail.get("ready")
                and isinstance(detail.get("detail_text"), str)
            }
            if set(detail_by_key) != {row.get("stable_row_key") for row in rows}:
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "activity_detail_batch_fallback",
                    "reason": "batch detail key/count mismatch",
                    "requested": len(rows),
                    "received": len(detail_by_key),
                })
                return {}
            snapshots: dict[str, tuple[dict[str, str], str]] = {}
            for row in rows:
                key = str(row["stable_row_key"])
                name = names[key]
                evidence = self._write_capture_evidence(
                    section,
                    name,
                    body_text,
                    url=redact_account_ids_in_url(str(result.get("url") or self.driver.current_url)),
                    title=str(result.get("title") or self.driver.title),
                    controls=[],
                    strict_state_scan=True,
                    screenshot=False,
                )
                snapshots[key] = (evidence, str(detail_by_key[key]["detail_text"]))
                self.state.log_click("activity:open-batched-rendered-detail")
                self.state.log_click("activity:collapse-batched-rendered-detail")
            close_results = self.driver.execute_script(
                ACTIVITY_DETAIL_BATCH_CLOSE_SCRIPT, control_ids
            )
            cleanup_confirmed = (
                isinstance(close_results, list)
                and len(close_results) == len(control_ids)
                and all(close_results)
            )
            if not cleanup_confirmed:
                self.state.block(
                    "batched Pending detail cleanup did not confirm every opened drawer was collapsed"
                )
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "activity_detail_batch_cleanup_unconfirmed",
                    "requested": len(rows),
                    "confirmed": (
                        sum(bool(value) for value in close_results)
                        if isinstance(close_results, list)
                        else 0
                    ),
                })
                return {}
            return snapshots
        except Exception as exc:
            self.state.accumulate("activity_detail_batch_exception", 0.0)
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "activity_detail_batch_fallback",
                "reason": type(exc).__name__,
                "requested": len(rows),
            })
            return {}
        finally:
            if not cleanup_confirmed:
                try:
                    self.driver.execute_script(
                        ACTIVITY_DETAIL_BATCH_CLOSE_SCRIPT, control_ids
                    )
                except Exception as exc:
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "activity_detail_batch_cleanup_failed",
                        "reason": type(exc).__name__,
                        "requested": len(rows),
                    })

    def close_support_chat(self) -> None:
        try:
            self.driver.execute_script(
                """
                const labels = ['Close support chat'];
                for (const label of labels) {
                  const b = Array.from(document.querySelectorAll('button,[role="button"]'))
                    .find(e => e.getAttribute('aria-label') === label);
                  if (b) b.click();
                }
                """
            )
        except Exception:
            pass

    def blocking_modal(self) -> str | None:
        """Return the heading of a visible page-covering modal, if present."""
        try:
            return self.driver.execute_script(BLOCKING_MODAL_SCRIPT)
        except Exception:
            return None

    def send_escape(self) -> None:
        """Dismiss a modal with Escape without activating any dialog control."""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.keys import Keys

        ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()

    def clear_blocking_modal(self) -> bool:
        """Dismiss a covering modal with Escape, or block the read-only run.

        A left-open export wizard covers the Activity list. Clicking through it
        can target an unrelated control inside the dialog, so this guard never
        clicks dialog controls. If Escape cannot close it, the audit stops.
        """
        heading = self.blocking_modal()
        if heading is None:
            return True
        for _ in range(BLOCKING_MODAL_ESCAPE_ATTEMPTS):
            try:
                self.send_escape()
            except Exception:
                break
            if wait_until(lambda: self.blocking_modal() is None, BLOCKING_MODAL_SETTLE_SECONDS):
                self.state.safety_log.append(
                    {"at": iso_now(), "event": "blocking_modal_dismissed", "heading": heading}
                )
                return True
        self.state.block(
            f"a modal dialog is covering the page and did not close ({heading!r}); "
            "close it in the browser and re-run"
        )
        self.state.safety_log.append(
            {"at": iso_now(), "event": "blocking_modal_persisted", "heading": heading}
        )
        return False

    def scan_state(self, text: str, *, strict: bool, evidence: str) -> None:
        lower = text.lower()
        # Logged-in Activity legitimately includes “log in to the mobile app”.
        # Require login-form evidence, not that generic phrase on its own.
        login_like = (
            ("welcome back" in lower and "password" in lower)
            or ("email address" in lower and "password" in lower)
            or ("log in" in lower and any(pattern.lower() in lower for pattern in LOGIN_FORM_PATTERNS))
        )
        if login_like:
            message = f"login/MFA/session wall visible; inventory cannot continue ({evidence})"
            if strict:
                self.state.block(message)
            else:
                self.state.warn(message)
            self.state.safety_log.append({"at": iso_now(), "event": "login_or_mfa_wall", "evidence": evidence})
        for pattern in FORBIDDEN_STATE_PATTERNS:
            if pattern.lower() in lower:
                message = f"forbidden trade/review state text visible: {pattern} ({evidence})"
                if strict:
                    self.state.block(message)
                else:
                    self.state.warn(message)
                self.state.safety_log.append({"at": iso_now(), "event": "forbidden_state_text", "pattern": pattern, "evidence": evidence})
        for control in ["Cancel order", "Modify order", "Submit order", "Place order", "Queue order", "Convert money"]:
            if control.lower() in lower:
                self.state.safety_log.append({"at": iso_now(), "event": "dangerous_control_visible", "control": control, "evidence": evidence})

    def click_label(self, label: str, *, exact: bool = True, section: str = "nav") -> bool:
        if label in DANGEROUS_CLICK_TEXT:
            self.state.log_click(label, blocked=True)
            return False
        clicked = self.driver.execute_script(
            """
            const label = arguments[0], exact = arguments[1];
            const els = Array.from(document.querySelectorAll('a,button,[role="button"],[role="menuitem"]'));
            const el = els.find(e => {
              const text = ((e.innerText || e.getAttribute('aria-label') || '').trim());
              return exact ? text === label : text.includes(label);
            });
            if (!el) return {ok:false};
            const text = ((el.innerText || el.getAttribute('aria-label') || '').trim());
            const href = el.href || el.getAttribute('href');
            el.scrollIntoView({block:'center'});
            el.click();
            return {ok:true, text, href};
            """,
            label,
            exact,
        )
        if clicked and clicked.get("ok"):
            self.state.log_click(f"{section}:{label}", clicked.get("href"))
            self.settle(self.page_is_interactive, CONTROL_CLICK_SETTLE_SECONDS, "control-click")
            return True
        return False

    def go_app_path(self, path: str, label: str) -> None:
        if not path.startswith("/app/"):
            self.state.block(f"refusing non-app navigation path: {path}")
            return
        url = "https://my.wealthsimple.com" + path
        self.state.log_click(f"readonly-url-nav:{label}", url)
        self.driver.get(url)
        self.settle(self.page_is_interactive, APP_NAVIGATION_SETTLE_SECONDS, "app-navigation")

    def go_url_readonly(self, url: str, label: str) -> None:
        if not url.startswith("https://my.wealthsimple.com/app/"):
            self.state.block(f"refusing non-Wealthsimple app navigation URL: {url}")
            return
        self.state.log_click(f"readonly-url-nav:{label}", url)
        self.driver.get(url)
        self.settle(self.page_is_interactive, EXTERNAL_NAVIGATION_SETTLE_SECONDS, "external-navigation")

    def account_page_readiness(self, account: str) -> dict[str, Any]:
        """Read-only structural snapshot of an account detail page's readiness."""
        readiness = self.driver.execute_script(
            """
            const prefix = arguments[0], account = arguments[1];
            const lines = (document.body ? document.body.innerText : '')
              .split('\\n').map(s => s.trim()).filter(Boolean);
            const after = (label) => {
              const i = lines.indexOf(label);
              return i >= 0 && i + 1 < lines.length ? lines[i + 1] : null;
            };
            return {
              url: location.href,
              account_name_visible: lines.includes(account),
              holdings_rows: document.querySelectorAll('[data-testid^="' + prefix + '"]').length,
              holdings_toolbar: document.querySelectorAll('[data-testid="account-holdings-toolbar"]').length,
              column_headers: Array.from(document.querySelectorAll('[role="columnheader"]'))
                .map(e => (e.innerText || '').trim()),
              total_cash_value: after('Total cash available'),
              available_cad_value: after('Available CAD'),
              available_usd_value: after('Available USD'),
            };
            """,
            HOLDINGS_ROW_TESTID_PREFIX,
            account,
        )
        readiness["verified_empty_by_fresh_holdings_export"] = (
            self.expected_security_holdings.get(account) == 0
        )
        return readiness

    def wait_for_account_content(
        self,
        account: str,
        timeout: float = 22.0,
        *,
        warn_on_timeout: bool = True,
    ) -> dict[str, Any] | None:
        """Wait for real rendered account content, not a skeleton or a wrong page.

        The 2026 account page never prints the word "Positions"; it renders a
        "Stocks" section with a holdings grid. It also paints the cash labels
        about a second before their values, so readiness is gated on the
        structural holdings grid plus money-shaped cash values rather than on
        section wording.
        """
        deadline = time.time() + timeout
        # Match the injectable wall clock used by the deadline. This timing is
        # diagnostic only, so a monotonic source is not necessary here.
        started = time.time()
        trace: dict[str, Any] = {
            "account": account,
            "timeout_seconds": timeout,
            "samples": [],
            "outcome": "timeout",
        }
        readiness: dict[str, Any] | None = None
        stable_empty_grid = 0
        stable_positive_grid = 0
        last_positive_row_count: int | None = None
        try:
            while time.time() < deadline:
                self.close_support_chat()
                if not self.clear_blocking_modal():
                    trace["outcome"] = "blocking_modal"
                    return None
                try:
                    readiness = self.account_page_readiness(account)
                except Exception as exc:
                    trace["outcome"] = "probe_error"
                    self.state.warn(f"{account} account readiness probe failed: {exc!r}")
                    return None
                strict_ready = account_page_is_ready(readiness, account, allow_empty_grid=False)
                empty_ready = account_page_is_ready(readiness, account, allow_empty_grid=True)
                trace["samples"].append({
                    "elapsed_seconds": round(time.time() - started, 3),
                    "holdings_rows": int(readiness.get("holdings_rows") or 0),
                    "holdings_toolbar": bool(readiness.get("holdings_toolbar")),
                    "column_headers": readiness.get("column_headers") or [],
                    "total_cash_value_rendered": looks_like_money(readiness.get("total_cash_value")),
                    "available_cad_value_rendered": looks_like_money(readiness.get("available_cad_value")),
                    "available_usd_value_rendered": looks_like_money(readiness.get("available_usd_value")),
                    "verified_empty_by_fresh_holdings_export": bool(
                        readiness.get("verified_empty_by_fresh_holdings_export")
                    ),
                    "strict_ready": strict_ready,
                    "empty_grid_ready": empty_ready,
                    "strict_failures": account_page_readiness_failures(readiness, account, allow_empty_grid=False),
                    "empty_grid_failures": account_page_readiness_failures(readiness, account, allow_empty_grid=True),
                })
                if strict_ready:
                    row_count = int(readiness.get("holdings_rows") or 0)
                    if row_count == last_positive_row_count:
                        stable_positive_grid += 1
                    else:
                        last_positive_row_count = row_count
                        stable_positive_grid = 1
                    if stable_positive_grid >= HOLDINGS_GRID_STABLE_CONFIRMATIONS:
                        readiness["holdings_rows_stable_confirmations"] = stable_positive_grid
                        trace["outcome"] = "stable_positive_grid"
                        return readiness
                else:
                    stable_positive_grid = 0
                    last_positive_row_count = None
                # An account with no positions is legitimate, but a toolbar that has
                # not yet painted its rows looks identical for a moment. Require the
                # rowless state to persist before accepting it, so holdings are not
                # read an instant before the grid renders.
                if not readiness.get("holdings_rows") and empty_ready:
                    stable_empty_grid += 1
                    if stable_empty_grid >= EMPTY_HOLDINGS_GRID_CONFIRMATIONS:
                        readiness["holdings_rows_stable_confirmations"] = stable_empty_grid
                        if readiness.get("verified_empty_by_fresh_holdings_export"):
                            trace["outcome"] = "stable_export_verified_empty_account"
                            self.state.safety_log.append({
                                "at": iso_now(),
                                "event": "empty_account_verified_by_fresh_holdings_export",
                                "account": account,
                                "stable_confirmations": stable_empty_grid,
                            })
                        else:
                            trace["outcome"] = "stable_empty_grid"
                            self.state.warn(f"{account} holdings grid rendered with no positions; treating the portfolio as empty")
                        return readiness
                else:
                    stable_empty_grid = 0
                time.sleep(0.5)
            if warn_on_timeout:
                self.state.warn(
                    f"timed out waiting for {account} account detail content "
                    f"(holdings_rows={(readiness or {}).get('holdings_rows')}, "
                    "available_cad_rendered="
                    f"{looks_like_money((readiness or {}).get('available_cad_value'))}, "
                    "readiness_failures="
                    f"{account_page_readiness_failures(readiness, account, allow_empty_grid=True)})"
                )
            return readiness
        finally:
            trace["elapsed_seconds"] = round(time.time() - started, 3)
            self.state.account_readiness_traces.append(trace)

    def read_holdings_rows(self, account: str) -> list[dict[str, Any]]:
        """Read the rendered per-account holdings grid rows (read-only query).

        Each row carries a stable data-testid and a security-details href that
        repeats the owning account slug, which binds a parsed holding to the
        account whose page was actually open.
        """
        rows = self.driver.execute_script(
            """
            const prefix = arguments[0];
            return Array.from(document.querySelectorAll('[data-testid^="' + prefix + '"]')).map(r => ({
              testid: r.getAttribute('data-testid'),
              href: r.getAttribute('href') || (r.querySelector('a') ? r.querySelector('a').getAttribute('href') : null),
              cells: (r.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean),
            }));
            """,
            HOLDINGS_ROW_TESTID_PREFIX,
        )
        return rows or []

    def _capture_current_account(
        self,
        account: str,
        home_ev: dict[str, str],
        home_account_values: dict[str, str],
        readiness: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Capture one already-ready account page without navigating."""
        section = ACCOUNT_SLUG[account]
        ev = self.capture(section, f"account-{section}")
        text = Path(ev["visible_text"]).read_text(
            encoding="utf-8", errors="replace"
        )
        dom_rows = self.read_holdings_rows(account)
        tooltip_ev = self.capture_total_cash_tooltip(section)
        tooltip_text = ""
        if tooltip_ev:
            tooltip_text = Path(tooltip_ev["visible_text"]).read_text(
                encoding="utf-8", errors="replace"
            )
        entry = parse_account_identity_and_balance(
            text, account, ev, tooltip_text, tooltip_ev
        )
        entry["overview_evidence"] = home_ev
        entry["page_readiness"] = readiness
        entry["holdings_rows_dom"] = dom_rows
        if readiness and not dom_rows and readiness.get("holdings_rows"):
            self.state.warn(
                f"{account} holdings grid rendered but no rows could be read from the DOM"
            )
        if account in home_account_values:
            entry["total_account_value"] = home_account_values[account]
            entry["total_account_value_status"] = (
                "directly_visible_home_card_currency_unlabeled"
            )
            entry["balance_confirmed_from"] = (
                "Home account card plus account detail"
            )
        return entry

    def _capture_authenticated_cad_position_values(
        self, accounts: dict[str, dict[str, Any]]
    ) -> None:
        """Attach broker-valued CAD controls for foreign-currency positions.

        Wealthsimple's holdings grid presents US securities in USD even though
        the account total is in CAD. The app's own FetchIdentityPositions query
        supports both behaviours: with ``currencyOverride=MARKET`` it returns
        the grid values, while omitting that override returns values in the
        query's CAD financials currency. Reuse one authenticated, allowlisted
        query envelope already issued by the app and retain only the resulting
        account/ticker/quantity/CAD values. Authorization and account IDs are
        never written to the evidence bundle.

        This is a GraphQL query, not a mutation. Failure is deliberately
        non-fatal: the normal missing-FX warning remains in that case.
        """
        started = time.monotonic()
        ws = None
        try:
            import websocket

            handle = self.driver.current_window_handle
            with urllib.request.urlopen(
                f"http://127.0.0.1:{os.environ.get('BROWSER_CONTROL_PORT', '9223')}/json/list", timeout=3
            ) as response:
                targets = json.load(response)
            target = next(
                row for row in targets
                if row.get("type") == "page" and row.get("id") == handle
            )
            ws = websocket.create_connection(
                target["webSocketDebuggerUrl"], timeout=0.4,
                suppress_origin=True,
            )
            command_id = 1
            ws.send(json.dumps({"id": command_id, "method": "Network.enable"}))
            command_id += 1
            ws.send(json.dumps({"id": command_id, "method": "Page.reload"}))
            deadline = time.monotonic() + 15.0
            envelope: tuple[dict[str, Any], dict[str, str]] | None = None
            while time.monotonic() < deadline and envelope is None:
                try:
                    message = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if message.get("method") != "Network.requestWillBeSent":
                    continue
                request = (message.get("params") or {}).get("request") or {}
                if "/graphql" not in str(request.get("url") or ""):
                    continue
                try:
                    payload = json.loads(request.get("postData") or "")
                except (TypeError, ValueError):
                    continue
                variables = payload.get("variables") or {}
                if (
                    payload.get("operationName") == "FetchIdentityPositions"
                    and variables.get("includeAccountData") is True
                    and int(variables.get("first") or 0) >= 200
                    and not variables.get("aggregated")
                    and not variables.get("accountIds")
                ):
                    envelope = (payload, request.get("headers") or {})
            if envelope is None:
                raise RuntimeError("app did not issue the all-account position query")

            payload, request_headers = envelope
            # The query's financials currency is already CAD. Removing the
            # MARKET override asks the same allowlisted query for CAD values.
            payload["variables"].pop("currencyOverride", None)
            payload["variables"].pop("accountIds", None)
            allowed_headers = {
                "authorization", "content-type", "x-platform-os",
                "x-ws-api-version", "x-ws-device-id", "x-ws-identity-id",
                "x-ws-locale", "x-ws-operation-hash",
                "x-ws-operation-name", "x-ws-profile",
                "x-ws-request-timeout",
            }
            replay_headers = {
                key: value for key, value in request_headers.items()
                if key.lower() in allowed_headers
            }
            self.driver.set_script_timeout(20)
            result = self.driver.execute_async_script(
                """
                const payload = arguments[0];
                const headers = arguments[1];
                const done = arguments[arguments.length - 1];
                fetch('/graphql', {
                  method: 'POST', credentials: 'same-origin', headers,
                  body: JSON.stringify(payload)
                }).then(async response => done({
                  status: response.status, body: await response.json()
                })).catch(error => done({error: String(error)}));
                """,
                payload,
                replay_headers,
            )
            if result.get("status") != 200 or result.get("error"):
                raise RuntimeError(
                    f"authenticated position query failed with status {result.get('status')}"
                )
            body = result.get("body") or {}
            if body.get("errors"):
                raise RuntimeError("authenticated position query returned GraphQL errors")
            edges = (
                (((body.get("data") or {}).get("identity") or {}).get("financials") or {})
                .get("current", {}).get("positions", {}).get("edges", [])
            )
            account_by_id = {
                str(entry.get("source_url") or "").rstrip("/").split("/")[-1]: name
                for name, entry in accounts.items()
            }
            controls: dict[str, list[dict[str, Any]]] = {
                name: [] for name in accounts
            }
            for edge in edges:
                node = (edge or {}).get("node") or {}
                security = node.get("security") or {}
                ticker = ((security.get("stock") or {}).get("symbol"))
                value = node.get("totalValue") or {}
                if not ticker or value.get("currency") != "CAD":
                    continue
                for account_ref in node.get("accounts") or []:
                    account = account_by_id.get(str(account_ref.get("id") or ""))
                    if not account:
                        continue
                    controls[account].append({
                        "ticker": normalize_ticker_for_cross_reference(str(ticker)),
                        "quantity": str(node.get("quantity") or ""),
                        "market_value_cad": str(value.get("amount") or ""),
                        "source": (
                            "Wealthsimple authenticated FetchIdentityPositions "
                            "query with CAD financials currency and no MARKET override"
                        ),
                        "captured_at": iso_now(),
                    })
            for account, rows in controls.items():
                if rows:
                    accounts[account]["authenticated_position_valuations_cad"] = rows
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "authenticated_cad_position_valuations_captured",
                "accounts": sorted(name for name, rows in controls.items() if rows),
                "position_count": sum(len(rows) for rows in controls.values()),
                "operation": "FetchIdentityPositions",
                "read_only": True,
            })
            self.state.metric(
                "authenticated_cad_position_valuation",
                time.monotonic() - started,
                sum(len(rows) for rows in controls.values()),
            )
        except Exception as exc:
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "authenticated_cad_position_valuations_unavailable",
                "reason": type(exc).__name__,
            })
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _rendered_investing_account_hrefs(self) -> dict[str, str]:
        from selenium.webdriver.common.by import By

        hrefs: dict[str, str] = {}
        for element in self.driver.find_elements(
            By.CSS_SELECTOR, 'a[href*="/app/account-details/"]'
        ):
            href = element.get_attribute("href")
            if not href:
                continue
            account = account_from_href(href)
            if account in ACCOUNTS and account not in hrefs:
                hrefs[account] = href
            elif account == "Other":
                if is_ignored_non_investing_account_href(href):
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "intentionally_excluded_non_investing_account",
                        "href": href,
                    })
                else:
                    self.state.warn(
                        f"visible unclassified account card found: {href}"
                    )
        return hrefs

    def _open_readonly_worker_tab(self, href: str) -> str:
        """Create one background target for an exact rendered account-card URL."""
        before = set(self.driver.window_handles)
        result = self.driver.execute_cdp_cmd(
            "Target.createTarget", {"url": href, "background": True}
        )
        target_id = str((result or {}).get("targetId") or "")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            handles = set(self.driver.window_handles)
            if target_id and target_id in handles:
                return target_id
            added = handles - before
            if added:
                return next(iter(added))
            time.sleep(0.02)
        raise RuntimeError("browser did not expose the read-only account worker tab")

    def _close_readonly_worker_tabs(
        self,
        anchor: str,
        baseline_handles: set[str],
        worker_handles: set[str],
    ) -> bool:
        for handle in list(worker_handles):
            if handle == anchor or handle not in self.driver.window_handles:
                continue
            try:
                self.driver.switch_to.window(handle)
                self.driver.close()
            except Exception as exc:
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "account_worker_cleanup_failed",
                    "reason": type(exc).__name__,
                })
        if anchor in self.driver.window_handles:
            self.driver.switch_to.window(anchor)
        extra = set(self.driver.window_handles) - baseline_handles
        if extra:
            self.state.block(
                f"read-only account preload left {len(extra)} worker tab(s) open"
            )
            return False
        return True

    def begin_home_account_preload(self) -> dict[str, Any] | None:
        """Capture Home evidence and stage exact URLs without launching workers."""
        preload_started = time.monotonic()
        self.go_app_path("/app/home", "Home")
        self.ensure_home_balance_visibility()
        self.wait_for_home_account_cards()
        home_ev = self.capture("account-overview", "home")
        home_text = Path(home_ev["visible_text"]).read_text(
            encoding="utf-8", errors="replace"
        )
        home_account_values = parse_home_account_values(clean_lines(home_text))
        hrefs = self._rendered_investing_account_hrefs()
        if set(hrefs) != set(ACCOUNTS):
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "account_preload_fallback",
                "reason": "Home did not expose every investing account URL",
                "accounts_found": sorted(hrefs),
            })
            return None

        anchor = self.driver.current_window_handle
        baseline_handles = set(self.driver.window_handles)
        return {
            "started": preload_started,
            "anchor": anchor,
            "baseline_handles": baseline_handles,
            "hrefs": hrefs,
            "workers": {},
            "holdings_dashboard_worker": None,
            "home_evidence": home_ev,
            "home_account_values": home_account_values,
        }

    def launch_home_account_preload_workers(
        self, preload: dict[str, Any]
    ) -> bool:
        """Start all read-only workers after Pending row discovery is complete."""
        workers: dict[str, str] = preload["workers"]
        holdings_dashboard_worker: str | None = None
        hrefs = preload["hrefs"]
        try:
            launch_started = time.monotonic()
            preload["workers_started"] = launch_started
            # Pending discovery and detail capture run between staging and
            # launch. Re-baseline so unrelated tabs opened during that window
            # are not mistaken for leaked workers.
            preload["baseline_handles"] = set(self.driver.window_handles)
            for account in ACCOUNTS:
                href = hrefs[account]
                workers[account] = self._open_readonly_worker_tab(href)
                self.state.log_click(
                    f"readonly-worker-target:{account}", href
                )
            holdings_dashboard_url = (
                "https://my.wealthsimple.com/app/holdings-dashboard"
            )
            holdings_dashboard_worker = self._open_readonly_worker_tab(
                holdings_dashboard_url
            )
            preload["holdings_dashboard_worker"] = (
                holdings_dashboard_worker
            )
            self.state.log_click(
                "readonly-worker-target:Holdings dashboard",
                holdings_dashboard_url,
            )
            self.state.metric(
                "account_preload_worker_launch",
                time.monotonic() - launch_started,
                len(workers) + 1,
            )
            if self.driver.current_window_handle != preload["anchor"]:
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "account_preload_anchor_refocused",
                })
                self.driver.switch_to.window(preload["anchor"])
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "account_preload_workers_launched",
                "accounts": list(ACCOUNTS),
            })
            return True
        except Exception as exc:
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "account_preload_fallback",
                "reason": type(exc).__name__,
            })
            self._close_readonly_worker_tabs(
                preload["anchor"],
                preload["baseline_handles"],
                set(workers.values())
                | (
                    {holdings_dashboard_worker}
                    if holdings_dashboard_worker
                    else set()
                ),
            )
            return False

    def finish_home_account_preload(
        self, preload: dict[str, Any]
    ) -> dict[str, dict[str, Any]] | None:
        """Harvest account workers after other read-only browser work has run."""
        workers = preload["workers"]
        accounts: dict[str, dict[str, Any]] = {}
        accepted = False
        try:
            holdings_worker = preload.get("holdings_dashboard_worker")
            if holdings_worker:
                dashboard_started = time.monotonic()
                self.driver.switch_to.window(holdings_worker)
                self.capture_current_holdings_dashboard(
                    timeout=HOLDINGS_DASHBOARD_TIMEOUT_SECONDS
                )
                self.state.metric(
                    "account_preload_holdings_dashboard_evidence",
                    time.monotonic() - dashboard_started,
                    1,
                )
            for account in ACCOUNTS:
                self.driver.switch_to.window(workers[account])
                readiness_started = time.monotonic()
                readiness = self.wait_for_account_content(
                    account, timeout=22.0, warn_on_timeout=False
                )
                self.state.accumulate(
                    "account_preload_readiness",
                    time.monotonic() - readiness_started,
                    1,
                )
                if not preload_readiness_is_stable(readiness):
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "account_preload_fallback",
                        "reason": "worker holdings grid never held still; readiness timed out mid-render",
                        "account": account,
                        "rows_seen": int(
                            (readiness or {}).get("holdings_rows") or 0
                        ),
                    })
                    return None
                if not account_page_is_ready(
                    readiness, account, allow_empty_grid=True
                ):
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "account_preload_fallback",
                        "reason": "worker account did not satisfy live holdings/cash readiness",
                        "account": account,
                        "rendered_url": redact_account_ids_in_url(
                            str((readiness or {}).get("url") or self.driver.current_url)
                        ),
                    })
                    return None
                capture_started = time.monotonic()
                entry = self._capture_current_account(
                    account,
                    preload["home_evidence"],
                    preload["home_account_values"],
                    readiness,
                )
                self.state.accumulate(
                    "account_preload_evidence",
                    time.monotonic() - capture_started,
                    1,
                )
                rendered_rows = int(
                    (readiness or {}).get("holdings_rows") or 0
                )
                captured_rows = len(entry.get("holdings_rows_dom") or [])
                if captured_rows != rendered_rows:
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "account_preload_fallback",
                        "reason": "holdings row count changed between readiness and capture",
                        "account": account,
                        "rows_rendered": rendered_rows,
                        "rows_captured": captured_rows,
                    })
                    return None
                # Wealthsimple omits Available USD entirely when the native USD
                # balance is zero. CAD is the dependable required account-cash
                # field; USD is optional evidence.
                if not entry.get("available_cash_cad"):
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "account_preload_fallback",
                        "reason": "worker account CAD cash field was incomplete",
                        "account": account,
                    })
                    return None
                accounts[account] = entry
                if self.state.blockers:
                    return None
            # The final worker is already on an authenticated account page.
            # Reuse that page for one read-only all-account CAD valuation
            # control before the workers are closed.
            self._capture_authenticated_cad_position_values(accounts)
            accepted = set(accounts) == set(ACCOUNTS)
            if accepted:
                self.state.metric(
                    "account_preload_worker_lifetime",
                    time.monotonic() - preload.get(
                        "workers_started", preload["started"]
                    ),
                    len(accounts) + int(bool(holdings_worker)),
                )
            return accounts if accepted else None
        except Exception as exc:
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "account_preload_fallback",
                "reason": type(exc).__name__,
            })
            return None
        finally:
            cleanup_ok = self._close_readonly_worker_tabs(
                preload["anchor"],
                preload["baseline_handles"],
                set(workers.values())
                | (
                    {preload["holdings_dashboard_worker"]}
                    if preload.get("holdings_dashboard_worker")
                    else set()
                ),
            )
            if accepted and cleanup_ok:
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "account_preload_completed",
                    "accounts": list(ACCOUNTS),
                    "worker_tabs_closed": len(workers)
                    + int(bool(preload.get("holdings_dashboard_worker"))),
                })

    def abort_home_account_preload(self, preload: dict[str, Any]) -> None:
        """Close launched workers when a later phase blocks before harvest."""
        self._close_readonly_worker_tabs(
            preload["anchor"],
            preload["baseline_handles"],
            set(preload["workers"].values())
            | (
                {preload["holdings_dashboard_worker"]}
                if preload.get("holdings_dashboard_worker")
                else set()
            ),
        )
        self.state.safety_log.append({
            "at": iso_now(),
            "event": "account_preload_aborted_and_cleaned",
        })

    def preload_home_account_cards(self) -> dict[str, dict[str, Any]] | None:
        """Compatibility path that launches and immediately harvests workers."""
        preload = self.begin_home_account_preload()
        if preload is None:
            return None
        if not self.launch_home_account_preload_workers(preload):
            return None
        return self.finish_home_account_preload(preload)

    def click_home_account_cards_serial(self) -> dict[str, dict[str, Any]]:
        from selenium.webdriver.common.by import By

        self.go_app_path("/app/home", "Home")
        self.ensure_home_balance_visibility()
        self.wait_for_home_account_cards()
        home_ev = self.capture("account-overview", "home")
        home_text = Path(home_ev["visible_text"]).read_text(encoding="utf-8", errors="replace")
        home_account_values = parse_home_account_values(clean_lines(home_text))
        hrefs: list[str] = []
        for el in self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/app/account-details/"]'):
            href = el.get_attribute("href")
            if href and href not in hrefs:
                hrefs.append(href)
        accounts: dict[str, dict[str, Any]] = {}
        if not hrefs:
            self.state.block("no Wealthsimple account cards found; login or account access likely blocked")
            return accounts
        for href in hrefs:
            account = account_from_href(href)
            if account == "Other":
                if is_ignored_non_investing_account_href(href):
                    self.state.safety_log.append({
                        "at": iso_now(),
                        "event": "intentionally_excluded_non_investing_account",
                        "href": href,
                    })
                else:
                    self.state.warn(f"visible unclassified account card found: {href}")
                continue
            self.go_app_path("/app/home", "Home")
            self.wait_for_home_account_cards()
            clicked = self.driver.execute_script(
                """
                const href = arguments[0];
                const el = Array.from(document.querySelectorAll('a[href*="/app/account-details/"]'))
                  .find(a => a.href === href);
                if (!el) return false;
                el.scrollIntoView({block:'center'});
                el.click();
                return true;
                """,
                href,
            )
            if not clicked:
                self.state.warn(f"account card disappeared before capture: {account}; skipped direct URL fallback")
                continue
            self.state.log_click(f"account-card:{account}", href)
            # wait_for_account_content already polls for the real readiness
            # signal; this only needs the navigation itself to have landed.
            self.settle(
                lambda: "/app/account-details/" in self.driver.current_url,
                ACCOUNT_CARD_SETTLE_SECONDS, "account-card",
            )
            readiness = self.wait_for_account_content(account)
            accounts[account] = self._capture_current_account(
                account, home_ev, home_account_values, readiness
            )
        if accounts:
            self._capture_authenticated_cad_position_values(accounts)
        return accounts

    def click_home_account_cards(
        self, account_capture_mode: str = "preloaded"
    ) -> dict[str, dict[str, Any]]:
        if account_capture_mode == "preloaded":
            accounts = self.preload_home_account_cards()
            if accounts is not None or self.state.blockers:
                return accounts or {}
            self.state.safety_log.append({
                "at": iso_now(),
                "event": "account_preload_serial_fallback",
            })
        return self.click_home_account_cards_serial()

    def capture_total_cash_tooltip(self, section: str) -> dict[str, str] | None:
        """Capture Wealthsimple's read-only total-cash definition and reference rate.

        This is a hover-only tooltip beside Total cash available. It does not
        navigate or alter account state, and prevents reports from treating
        the CAD aggregate as native CAD cash.
        """
        try:
            from selenium.webdriver.common.action_chains import ActionChains

            triggers = self.driver.find_elements(
                "xpath",
                "//p[normalize-space()='Total cash available']/following-sibling::button[@data-scope='tooltip']",
            )
            if not triggers:
                self.state.safety_log.append({
                    "at": iso_now(),
                    "event": "total_cash_tooltip_not_present",
                    "account": section,
                })
                return None
            trigger = triggers[0]
            ActionChains(self.driver).move_to_element(trigger).pause(0.7).perform()
            # The tooltip content mounts asynchronously. A single fixed pause
            # lost the RRSP definition on the 2026-07-26 10:43 run purely on
            # timing, so poll the hover result within a bounded budget.
            deadline = time.monotonic() + TOTAL_CASH_TOOLTIP_TIMEOUT
            text = ""
            while time.monotonic() < deadline:
                text = self.body_text()
                if total_cash_tooltip_is_exposed(text):
                    break
                time.sleep(0.2)
            if not total_cash_tooltip_is_exposed(text):
                self.state.warn(f"{section} total-cash tooltip did not expose its definition; aggregate cash remains labelled as a CAD conversion")
                return None
            return self.capture(section, f"total-cash-tooltip-{section}", include_controls=False)
        except Exception as exc:
            self.state.warn(f"{section} total-cash tooltip capture failed: {exc!r}")
            return None

    def ensure_home_balance_visibility(self, timeout: float = 12.0) -> bool:
        """Keep account balances visible for the read-only capture.

        This only toggles Wealthsimple's local display/privacy eye, never a
        brokerage or money action. The user explicitly requested visible
        balances so the ledger can evidence totals rather than infer them.
        """
        deadline = time.monotonic() + timeout
        toggled = False
        while time.monotonic() < deadline:
            label = self.driver.execute_script(
                """
                const button = Array.from(document.querySelectorAll('button,[role="button"]'))
                  .find(el => ['Show account balance', 'Hide account balance'].includes(el.getAttribute('aria-label')));
                return button ? button.getAttribute('aria-label') : null;
                """
            )
            if label == "Hide account balance":
                return True
            if label == "Show account balance" and not toggled:
                clicked = self.driver.execute_script(
                    """
                    const button = Array.from(document.querySelectorAll('button,[role="button"]'))
                      .find(el => el.getAttribute('aria-label') === 'Show account balance');
                    if (!button) return false;
                    button.click();
                    return true;
                    """
                )
                if clicked:
                    self.state.log_click("home:show-account-balances")
                    toggled = True
            time.sleep(0.35)
        self.state.warn("Home account-balance privacy toggle did not reach visible state; total account values will be marked unverified")
        return False

    def wait_for_home_account_cards(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hrefs = self.driver.execute_script(
                    "return Array.from(document.querySelectorAll('a[href*=\"/app/account-details/\"]')).map(a => a.href);"
                ) or []
                if home_investing_account_cards_ready(hrefs):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self.state.warn("timed out waiting for Home account cards to render")

    def capture_current_holdings_dashboard(
        self, timeout: float = HOLDINGS_DASHBOARD_TIMEOUT_SECONDS
    ) -> dict[str, str] | None:
        """Capture an already-open cross-account holdings dashboard.

        The dashboard grid is virtualised and renders its rows well after the
        page shell, so a fixed one-second wait stored an empty table. This is
        never the parsing source for the ledger; per-account pages are, because
        each of their rows is already bound to one account.
        """
        deadline = time.time() + timeout
        rows = 0
        while time.time() < deadline:
            try:
                rows = self.driver.execute_script(
                    "return document.querySelectorAll('[data-testid=\"holdings-dashboard-table\"] [role=\"row\"]').length;"
                ) or 0
            except Exception:
                rows = 0
            if rows > 1:
                break
            time.sleep(0.5)
        if rows <= 1:
            self.state.warn("holdings dashboard rows did not render; dashboard evidence is a shell only")
        return self.capture("holdings", "holdings-dashboard")

    def capture_holdings_dashboard(
        self, timeout: float = HOLDINGS_DASHBOARD_TIMEOUT_SECONDS
    ) -> dict[str, str] | None:
        """Navigate to and capture the cross-account holdings dashboard."""
        self.go_app_path("/app/holdings-dashboard", "Holdings")
        return self.capture_current_holdings_dashboard(timeout)

    def verify_activity_filter_defaults(self, *, phase: str = "unspecified") -> bool:
        """Expand only filter disclosures, inspect selections, then restore layout."""
        opened = []
        snapshot = None
        toggle = """
        let root = document.querySelector('[data-testid="filter-search"]');
        while(root && !(root.innerText || '').trim().startsWith('Filters')) root=root.parentElement;
        if(!root) return false;
        const matches=Array.from(root.querySelectorAll('button')).filter(e=>e.innerText.trim()===arguments[0]);
        if(matches.length!==1 || matches[0].getAttribute('aria-expanded')!==arguments[1]) return false;
        matches[0].click(); return true;
        """
        try:
            self._filter_settle_count = None
            for name in FILTER_GROUPS:
                if self.driver.execute_script(toggle, name, "false"):
                    opened.append(name)
                    self.state.log_click("activity-filter-disclosure:" + name)
            self.settle(
                self.activity_filter_sidebar_settled,
                ACTIVITY_FILTER_SETTLE_SECONDS,
                f"activity-filter-expand-{phase}",
            )
            snapshot = self.driver.execute_script(FILTER_SNAPSHOT_SCRIPT, list(FILTER_GROUPS))
            confirmed = confirms_unfiltered(snapshot)
            logs = self.state.out_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            payload = {
                "observed_at": iso_now(),
                "phase": phase,
                "confirmed_unfiltered": confirmed,
                "basis": "explicit_sidebar_defaults",
                "reset_attempted": self.state.filter_reset_attempted,
                "reset_click_succeeded": self.state.filter_reset_click_succeeded,
                "observed_default_before": self.state.filter_observed_default_before,
                "observed_default_after": self.state.filter_observed_default_after,
                "snapshot": snapshot,
            }
            phase_name = {
                "before_traversal": "activity-filter-state-before-traversal.json",
                "after_traversal": "activity-filter-state-after-traversal.json",
            }.get(phase, f"activity-filter-state-{phase}.json")
            (logs / phase_name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            # Canonical file always mirrors the latest known four provenance fields.
            canonical = {
                "observed_at": payload["observed_at"],
                "phase": phase,
                "confirmed_unfiltered": confirmed,
                "basis": "explicit_sidebar_defaults",
                "reset_attempted": self.state.filter_reset_attempted,
                "reset_click_succeeded": self.state.filter_reset_click_succeeded,
                "observed_default_before": (
                    confirmed if phase == "before_traversal"
                    else self.state.filter_observed_default_before
                ),
                "observed_default_after": (
                    confirmed if phase == "after_traversal"
                    else self.state.filter_observed_default_after
                ),
                "snapshot": snapshot if phase == "before_traversal" else (
                    json.loads((logs / "activity-filter-state-before-traversal.json").read_text()).get("snapshot")
                    if (logs / "activity-filter-state-before-traversal.json").exists() else snapshot
                ),
                "after_traversal_snapshot": snapshot if phase == "after_traversal" else None,
            }
            if phase == "after_traversal" and (logs / "activity-filter-state.json").exists():
                prior = json.loads((logs / "activity-filter-state.json").read_text(encoding="utf-8"))
                canonical["snapshot"] = prior.get("snapshot")
                canonical["observed_default_before"] = prior.get("observed_default_before")
                canonical["observed_at"] = prior.get("observed_at", canonical["observed_at"])
            (logs / "activity-filter-state.json").write_text(
                json.dumps(canonical, indent=2) + "\n", encoding="utf-8"
            )
            self.state.filter_state_path = str(logs / "activity-filter-state.json")
            return confirmed
        except Exception:
            # Missing/changed sidebar is not proof of an unfiltered feed.
            return False
        finally:
            for name in reversed(opened):
                try:
                    if self.driver.execute_script(toggle, name, "true"):
                        self.state.log_click("activity-filter-disclosure-close:" + name)
                except Exception:
                    pass

    def _record_broker_pending_count(self, *, phase: str, source: str) -> dict[str, Any]:
        text = self.body_text()
        value = parse_broker_pending_count(text)
        observation = make_count_observation(
            value,
            scope="all_accounts_activity",
            account_filter="all",
            status_filter="pending_transactions_label",
            population_meaning="broker_visible_pending_transactions",
            timestamp=iso_now(),
            source=source,
        )
        observation["phase"] = phase
        path = self.state.out_dir / "logs" / f"broker-pending-count-{phase}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
        if value is None:
            self.state.warn(
                f"broker pending-transaction count unavailable at {phase}; "
                "count corroboration downgraded"
            )
        return observation

    def capture_activity(self) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
        started = time.monotonic()
        self.state.pending_scan_complete = False
        self.state.traversal_exhausted = False
        self.state.unparsed_pending_controls = []
        self.go_app_path("/app/activity", "Activity")
        self.wait_for_activity_cards("pending-order capture")
        # Activity filters persist across navigation and manual browser use.
        # Start from an explicit all-account view so a prior Account/Type
        # filter cannot silently remove open orders from this audit.
        self.state.filter_reset_attempted = True
        self.state.filter_reset_click_succeeded = self.click_activity_filter_clear("Clear")
        if self.state.filter_reset_click_succeeded:
            self._filter_settle_count = None
            self.settle(
                self.activity_filter_sidebar_settled,
                ACTIVITY_FILTER_SETTLE_SECONDS,
                "activity-filter-reset",
            )
        # A reset click is provenance only. Always observe defaults.
        self.state.filter_observed_default_before = self.verify_activity_filter_defaults(
            phase="before_traversal"
        )
        self.state.broker_pending_count_before = self._record_broker_pending_count(
            phase="before_traversal", source="activity_visible_text"
        )
        # Scan the unfiltered feed: the Pending quick filter's inclusion of
        # partial fills/cancel requests is not an established UI contract.
        # Only active disclosures are opened; completed trade drawers stay off.
        if not self.wait_for_activity_cards("all-status open-order discovery"):
            self.state.block(
                "Activity did not render readable cards or an explicit empty state; refusing to report zero open orders"
            )
            ev = self.capture(
                "activity", "activity-pending-render-failure",
                screenshot=True,
            )
            self.state.metric(
                "pending_activity_scan", time.monotonic() - started, 0
            )
            return ev, []
        first_ev = self.capture("activity", "activity-start")
        rows_by_key: dict[str, dict[str, Any]] = {}
        scroll_log: list[dict[str, Any]] = []
        stable_steps = 0
        last_count = -1
        unparsed_seen: set[str] = set()
        exhausted = False
        stable_bottom_steps = 0
        for step in range(30):
            ev = {"url": self.driver.current_url, "visible_text": first_ev["visible_text"], "screenshot": first_ev["screenshot"]}
            controls = self.controls()
            unparsed_seen.update(unparsed_pending_controls(controls))
            for row in parse_pending_rows_from_controls(controls, ev):
                rows_by_key[row["stable_row_key"]] = row
            count = len(rows_by_key)
            if count != last_count:
                print(f"Pending order rows discovered: {count}", flush=True)
            scroll_log.append({"step": step, "pending_rows": count, "url": self.driver.current_url})
            (self.state.out_dir / "logs" / "activity-scroll-log.json").write_text(json.dumps(scroll_log, indent=2) + "\n", encoding="utf-8")
            if count == last_count:
                stable_steps += 1
            else:
                stable_steps = 0
            last_count = count
            button = self._find_safe_button("Load more")
            if button is not None:
                self.state.log_click("activity:Load more")
                self._click(button)
                time.sleep(1.2)
                continue
            # This viewport has actually been parsed. Never declare completion
            # immediately after scrolling into an as-yet-unread final viewport.
            at_bottom = bool(self.driver.execute_script(
                "return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;"
            ))
            stable_bottom_steps = stable_bottom_steps + 1 if at_bottom and stable_steps else 0
            if stable_bottom_steps >= 3:
                exhausted = True
                break
            self.driver.execute_script("window.scrollBy(0, 900);")
            time.sleep(0.5)
        unparsed_seen.update(unparsed_pending_controls(self.controls()))
        unparsed = sorted(unparsed_seen)
        self.state.unparsed_pending_controls = unparsed
        self.state.traversal_exhausted = exhausted
        self.state.broker_pending_count_after = self._record_broker_pending_count(
            phase="after_traversal", source="activity_visible_text"
        )
        self.state.filter_observed_default_after = self.verify_activity_filter_defaults(
            phase="after_traversal"
        )

        filters_proven = (
            self.state.filter_observed_default_before is True
            and self.state.filter_observed_default_after is True
        )
        self.state.pending_scan_complete = exhausted and not unparsed and filters_proven
        if not filters_proven:
            self.state.warn("all-account Activity filter defaults not confirmed; sell coverage scope uncertain")
        if not exhausted:
            self.state.warn("pending order scan reached its traversal bound without a stable bottom; sell coverage uncertain")
        if unparsed:
            self.state.warn(
                f"{len(unparsed)} pending order card(s) could not be parsed into ledger rows and are "
                f"missing from open orders and from cash reconciliation: {'; '.join(unparsed[:5])}"
            )
        ev = self.capture("activity", "activity-expanded-final", screenshot=False)
        try:
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
        except Exception as exc:
            self.state.warn(f"could not reset activity scroll before detail pass: {exc!r}")
        rows = sorted(rows_by_key.values(), key=order_priority_key)
        self.state.metric("pending_activity_scan", time.monotonic() - started, len(rows))
        return ev, rows

    def capture_pending_deposit_availability(self) -> None:
        """Read bounded existing EFT deposit disclosures; never funding controls."""
        texts = [c.get("text", "") for c in self.controls()
                 if c.get("tag") == "button"
                 and c.get("text", "").splitlines()[:2] == ["Deposit", "Electronic funds transfer"]
                 and any(s in c.get("text", "").splitlines() for s in ("In progress", "Pending"))]
        if len(texts) > 10:
            self.state.warn("pending deposit disclosure limit exceeded; availability evidence may be incomplete")
        for index, text in enumerate(texts[:10]):
            if texts.count(text) != 1:
                self.state.warn("ambiguous pending deposit cards; availability not inferred")
                continue
            opened = False
            script = """
            const es=Array.from(document.querySelectorAll('button')).filter(e=>e.innerText.trim()===arguments[0]);
            if(es.length!==1) return null;
            const e=es[0];
            if(arguments[1]==='open' && e.getAttribute('aria-expanded')==='false'){e.click();return {opened:true};}
            if(arguments[1]==='close' && e.getAttribute('aria-expanded')==='true'){e.click();return {};}
            return {text:e.parentElement.innerText, opened:false};
            """
            try:
                result = self.driver.execute_script(script, text, "open")
                if result is None:
                    continue
                opened = result.get("opened", False)
                if opened:
                    self.state.log_click("deposit-availability:open-existing-disclosure")
                latest = {}
                def ready():
                    nonlocal latest
                    latest = self.driver.execute_script(script, text, "read") or {}
                    return "Amount" in latest.get("text", "").splitlines()
                if not self.settle(ready, 4.0, "deposit-availability"):
                    self.state.warn("pending deposit disclosure did not render availability evidence")
                    continue
                record = parse_deposit_availability(latest["text"])
                ev = self._write_capture_evidence(
                    "deposit-details", f"pending-deposit-{index:03d}", latest["text"],
                    url=redact_account_ids_in_url(self.driver.current_url), title=self.driver.title,
                    controls=[], strict_state_scan=False, screenshot=False)
                record.update(observed_at=iso_now(), evidence_file=ev["visible_text"])
                self.state.deposit_availability.append(record)
            except Exception as exc:
                self.state.warn(f"pending deposit availability inspection failed: {type(exc).__name__}; no availability inferred")
            finally:
                if opened:
                    try:
                        self.driver.execute_script(script, text, "close")
                        self.state.log_click("deposit-availability:close-existing-disclosure")
                    except Exception:
                        self.state.warn("could not restore pending deposit disclosure layout")

    def capture_recent_activity(self) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
        """Capture the unfiltered Activity feed separately from Pending orders.

        The Pending pass is authoritative for current open orders. This pass is
        only for recent activity and must never promote final/cancelled rows to
        the open-order ledger.
        """
        started = time.monotonic()
        self.go_app_path("/app/activity", "Activity recent")
        # The app can retain a prior Pending quick-filter or briefly render a
        # blank shell after a long detail pass. Clear is a view-only filter
        # reset; wait for actual Activity cards rather than treating a loading
        # skeleton as a valid zero-row history.
        if self.click_activity_filter_clear("Clear"):
            time.sleep(0.8)
        # Wealthsimple can retain the scroll offset while replacing a Pending
        # filter with the full feed. Always begin at the newest row.
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.2)
        if not self.wait_for_activity_cards("recent-activity capture"):
            self.state.block("Activity did not render readable cards after the pending-detail pass; refusing to report a zero-row history")
            ev = self.capture("activity", "activity-recent-render-failure", screenshot=True)
            self.state.metric("recent_activity_scan", time.monotonic() - started, 0)
            return ev, []
        rows_by_key: dict[str, dict[str, Any]] = {}
        scroll_log: list[dict[str, Any]] = []
        stable_steps = 0
        last_count = -1
        scan_complete = False
        for step in range(40):
            ev = {"url": self.driver.current_url, "visible_text": "", "screenshot": ""}
            visible_rows = parse_activity_rows_from_controls(self.controls(), ev)
            reached_prior_year = False
            for row in visible_rows:
                # Pending is authoritative only in the dedicated open-order
                # pass. Keeping it out of the history dataset prevents the UI
                # and ChatGPT handoff from calling queued orders activity.
                if order_state(row.get("status")) == "open":
                    continue
                if activity_row_is_older_than_cutoff(row):
                    reached_prior_year = True
                    continue
                rows_by_key[row["stable_row_key"]] = row
            count = len(rows_by_key)
            if count != last_count:
                print(f"Recent activity rows discovered: {count}", flush=True)
            scroll_log.append({"step": step, "activity_rows": count, "url": self.driver.current_url})
            (self.state.out_dir / "logs" / "recent-activity-scroll-log.json").write_text(json.dumps(scroll_log, indent=2) + "\n", encoding="utf-8")
            stable_steps = stable_steps + 1 if count == last_count else 0
            last_count = count
            # Activity is newest-first. Once the visible date heading crosses
            # into a prior calendar year, further loading only adds history the
            # audit intentionally excludes.
            if reached_prior_year:
                scroll_log[-1]["stopped_at_prior_year"] = True
                (self.state.out_dir / "logs" / "recent-activity-scroll-log.json").write_text(json.dumps(scroll_log, indent=2) + "\n", encoding="utf-8")
                scan_complete = True
                break
            button = self._find_safe_button("Load more")
            if button is not None:
                self.state.log_click("activity-recent:Load more")
                self._click(button)
                time.sleep(1.2)
                continue
            at_bottom = bool(self.driver.execute_script(
                "window.scrollBy(0, 900); "
                "return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4;"
            ))
            time.sleep(0.5)
            # A stable row count while the page is still moving only means
            # the next virtualized/load-more boundary has not been reached.
            # It is not evidence that the feed is exhausted.
            if not at_bottom:
                stable_steps = 0
            if stable_steps >= 3 and at_bottom:
                scan_complete = True
                break
        if not scan_complete:
            self.state.warn(
                "recent Activity scan exhausted its bounded traversal without reaching the prior-year cutoff or a stable page bottom"
            )
        unparsed_terminal = unparsed_terminal_controls(self.controls())
        if unparsed_terminal:
            self.state.warn(
                f"{len(unparsed_terminal)} terminal Activity card(s) carried a final-status token "
                f"but could not be parsed for selective detail confirmation: "
                f"{'; '.join(unparsed_terminal[:5])}"
            )
        ev = self.capture("activity", "activity-recent-final", screenshot=False)
        for row in rows_by_key.values():
            row["source_list_page"] = ev.get("url")
            row["dom_visible_text_evidence_reference"] = ev.get("visible_text")
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.2)
        rows = sorted(rows_by_key.values(), key=activity_sort_key)
        self.state.metric("recent_activity_scan", time.monotonic() - started, len(rows))
        return ev, rows

    def wait_for_activity_cards(self, purpose: str, timeout: float = 15.0) -> bool:
        """Wait for real Activity data, never accepting a loading shell as empty history."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                # Only reach for the whole body when no rows parsed: on the
                # success path the full innerText read was pure overhead, and
                # this poll runs every 0.4s against a large Activity page.
                rows = parse_activity_rows_from_controls(self.controls(), {"url": self.driver.current_url})
                if rows:
                    return True
                text = self.body_text()
                if "No activity" in text or "No scheduled activities" in text:
                    return True
            except Exception:
                pass
            time.sleep(0.4)
        self.state.warn(f"Activity did not render readable cards within {timeout:.0f}s for {purpose}")
        return False

    def wait_for_pending_activity_cards(
        self, purpose: str, timeout: float = 15.0
    ) -> bool:
        """Wait for filtered Pending rows or a broker-rendered empty state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                rows = parse_pending_rows_from_controls(
                    self.controls(), {"url": self.driver.current_url}
                )
                if rows:
                    return True
                text = self.body_text()
                if any(phrase in text for phrase in (
                    "No scheduled activities",
                    "No pending orders",
                    "No activity",
                    "No results",
                )):
                    return True
            except Exception:
                pass
            time.sleep(0.4)
        self.state.warn(
            f"Pending Activity did not render readable cards within "
            f"{timeout:.0f}s for {purpose}"
        )
        return False

    def open_order_details(
        self,
        rows: list[dict[str, Any]],
        pending_detail_mode: str = "serial",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        started = time.monotonic()
        details: list[dict[str, Any]] = []
        target_rows = {row["stable_row_key"]: row for row in rows}
        processed: set[str] = set()
        unresolved: list[dict[str, Any]] = []
        ordinal = 0
        stable_steps = 0
        # Work through currently rendered cards in batches. The old loop
        # restarted at the top for every detail, which made large order books
        # effectively O(n^2) in scroll time.
        for _step in range(80):
            visible = {row["stable_row_key"]: row for row in parse_pending_rows_from_controls(self.controls(), {"url": self.driver.current_url})}
            candidates = [key for key in visible if key in target_rows and key not in processed]
            for offset in range(0, len(candidates), PENDING_DETAIL_BATCH_SIZE):
                chunk_keys = candidates[offset:offset + PENDING_DETAIL_BATCH_SIZE]
                chunk_rows = [target_rows[key] for key in chunk_keys]
                names: dict[str, str] = {}
                for row in chunk_rows:
                    ordinal += 1
                    key = row["stable_row_key"]
                    names[key] = (
                        f"order-detail-{ordinal:03d}-{row['account']}-{row['ticker']}-"
                        f"{row['side']}-{row.get('estimated_total','')}"
                    )
                if offset == 0 or ordinal == len(rows):
                    print(f"Confirming pending order detail {ordinal}/{len(rows)}", flush=True)
                snapshots = (
                    self.capture_open_activity_detail_batch(
                        "order-details", chunk_rows, names
                    )
                    if pending_detail_mode == "batched"
                    else {}
                )
                for row in chunk_rows:
                    key = row["stable_row_key"]
                    snapshot = snapshots.get(key)
                    opened_serially = False
                    if snapshot is None:
                        if not self._open_activity_row_here(
                            row["row_text"], row.get("source_control_id")
                        ):
                            copy = dict(row)
                            copy["reason"] = (
                                "rendered pending order could not be opened safely "
                                "after batched-detail fallback"
                            )
                            unresolved.append(copy)
                            processed.add(key)
                            continue
                        opened_serially = True
                        snapshot = self.capture_open_activity_detail(
                            "order-details",
                            names[key],
                            row["row_text"],
                            row.get("source_control_id"),
                        )
                    try:
                        ev, detail_text = snapshot
                        parsed = parse_order_detail_blocks(
                            detail_text, ev, ticker_hint=row["ticker"]
                        )
                        match = find_matching_detail(row, parsed, allow_unbound=True)
                        if match and match.get("confirmation_level") == "detail_confirmed":
                            match = enrich_order(match, ev.get("order_metadata"))
                            details.append(match)
                        else:
                            copy = dict(row)
                            copy["reason"] = "detail pane was incomplete or did not parse"
                            if match:
                                copy["partial_detail"] = match
                            unresolved.append(copy)
                        processed.add(key)
                    finally:
                        if opened_serially:
                            self.close_activity_row_here(
                                row["row_text"], row.get("source_control_id")
                            )
            if len(processed) == len(target_rows):
                break
            before = len(processed)
            self.driver.execute_script("window.scrollBy(0, 850);")
            time.sleep(0.22)
            stable_steps = stable_steps + 1 if len(processed) == before and not candidates else 0
            if stable_steps >= 4:
                break
        for key, row in target_rows.items():
            if key not in processed:
                copy = dict(row)
                copy["reason"] = "pending order was not reached in the batched rendered-card detail pass"
                unresolved.append(copy)
        self.state.metric("order_detail_capture", time.monotonic() - started, len(rows))
        return details, unresolved

    def open_completed_activity_details(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Detail-confirm completed buy/sell rows without touching order actions."""
        started = time.monotonic()
        details: list[dict[str, Any]] = []
        candidates = [
            row for row in rows
            # Collapsed Wealthsimple cards often omit the terminal state.
            # A status-unconfirmed buy/sell is not reported as a fill, but it
            # is a safe existing-Activity disclosure target. The detail pane
            # is the authority that resolves Completed versus Cancelled,
            # Expired, or another final state.
            if row.get("status") in {"Completed", "Filled", "Completed/Filled", "Status unconfirmed"}
            and row.get("side") in {"buy", "sell"}
            and row.get("ticker")
        ]
        target_rows = {row["stable_row_key"]: row for row in candidates}
        processed: set[str] = set()
        ordinal = 0
        stable_steps = 0
        # Same batched rendered-card traversal as pending details. Re-starting
        # at the top per completed fill multiplies read-only run time without
        # increasing evidence quality.
        for _step in range(80):
            visible = {
                row["stable_row_key"]: row
                for row in parse_activity_rows_from_controls(self.controls(), {"url": self.driver.current_url})
            }
            candidate_keys = [key for key in visible if key in target_rows and key not in processed]
            for key in candidate_keys:
                row = target_rows[key]
                ordinal += 1
                if ordinal == 1 or ordinal % 10 == 0 or ordinal == len(candidates):
                    print(f"Confirming completed activity detail {ordinal}/{len(candidates)}", flush=True)
                if not self._open_activity_row_here(
                    row["row_text"], row.get("source_control_id")
                ):
                    # A virtualized card can disappear between the control
                    # inventory and click. Record this row-only limitation and
                    # move on; retrying it on every scroll step turns a single
                    # transient miss into an unbounded detail loop.
                    row.setdefault("uncertainty_notes", []).append("completed activity detail row was rendered but could not be reopened safely")
                    processed.add(key)
                    continue
                name = f"activity-detail-{ordinal:03d}-{row['account']}-{row['ticker']}-{row.get('side')}-{row.get('total_value') or ''}"
                ev, detail_text = self.capture_open_activity_detail(
                    "activity", name, row["row_text"], row.get("source_control_id")
                )
                detail = parse_completed_activity_detail(detail_text, row, ev)
                if detail:
                    details.append(detail)
                else:
                    row.setdefault("uncertainty_notes", []).append("completed activity detail opened but exact fill fields were not parsed")
                processed.add(key)
                self.close_activity_row_here(
                    row["row_text"], row.get("source_control_id")
                )
            if len(processed) == len(target_rows):
                break
            before = len(processed)
            self.driver.execute_script("window.scrollBy(0, 850);")
            time.sleep(0.22)
            stable_steps = stable_steps + 1 if len(processed) == before and not candidate_keys else 0
            if stable_steps >= 4:
                break
        for key, row in target_rows.items():
            if key not in processed:
                row.setdefault("uncertainty_notes", []).append("completed activity detail row was not reached in the batched rendered-card detail pass")
        self.state.metric("completed_activity_detail_capture", time.monotonic() - started, len(candidates))
        return details

    def open_terminal_activity_details(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Detail-confirm browser-only terminal activity rows.

        The canonical Activity CSV omits cancelled, expired, rejected, and
        failed events. Their collapsed browser cards can also omit identity
        fields, so open only this small terminal subset and fail visibly if a
        row cannot be confirmed. No order or transfer action is invoked.
        """
        started = time.monotonic()
        terminal_rows = [
            row for row in rows
            if row.get("status") in {"Cancelled", "Expired", "Rejected", "Failed"}
        ]
        identity_counts: dict[tuple[Any, ...], int] = {}
        for row in terminal_rows:
            key = terminal_activity_identity_key(row)
            identity_counts[key] = identity_counts.get(key, 0) + 1
        candidates = []
        for row in terminal_rows:
            collision = identity_counts[terminal_activity_identity_key(row)] > 1
            if collision:
                row["terminal_identity_collision"] = True
            if collision or not terminal_activity_row_is_complete(row):
                candidates.append(row)
        target_rows = {row["stable_row_key"]: row for row in candidates}
        details: list[dict[str, Any]] = []
        processed: set[str] = set()
        ordinal = 0
        stable_steps = 0
        for _step in range(80):
            visible = {
                row["stable_row_key"]: row
                for row in parse_activity_rows_from_controls(
                    self.controls(), {"url": self.driver.current_url}
                )
            }
            candidate_keys = [
                key for key in visible if key in target_rows and key not in processed
            ]
            for key in candidate_keys:
                row = target_rows[key]
                ordinal += 1
                print(
                    f"Confirming terminal activity detail {ordinal}/{len(candidates)}",
                    flush=True,
                )
                if not self._open_activity_row_here(
                    row["row_text"], row.get("source_control_id")
                ):
                    row.setdefault("uncertainty_notes", []).append(
                        "terminal activity detail row was rendered but could not be reopened safely"
                    )
                    processed.add(key)
                    continue
                try:
                    name = f"terminal-activity-{ordinal:03d}-{row['account']}-{row['status']}"
                    evidence, detail_text = self.capture_open_activity_detail(
                        "activity", name, row["row_text"], row.get("source_control_id")
                    )
                    detail = (
                        parse_completed_activity_detail(detail_text, row, evidence)
                        if row.get("side") in {"buy", "sell"}
                        else parse_terminal_activity_detail(detail_text, row, evidence)
                    )
                    if detail:
                        details.append(detail)
                    else:
                        row.setdefault("uncertainty_notes", []).append(
                            "terminal activity detail opened but deterministic identity fields were not parsed"
                        )
                    processed.add(key)
                finally:
                    if not self.close_activity_row_here(
                        row["row_text"], row.get("source_control_id")
                    ):
                        self.state.block(
                            "terminal Activity detail cleanup did not confirm the opened drawer was collapsed"
                        )
            if len(processed) == len(target_rows):
                break
            before = len(processed)
            self.driver.execute_script("window.scrollBy(0, 850);")
            time.sleep(0.22)
            stable_steps = stable_steps + 1 if len(processed) == before and not candidate_keys else 0
            if stable_steps >= 4:
                break
        for key, row in target_rows.items():
            if key not in processed:
                row.setdefault("uncertainty_notes", []).append(
                    "terminal activity detail row was not reached in the rendered-card detail pass"
                )
        self.state.metric("terminal_activity_detail_capture", time.monotonic() - started, len(candidates))
        return details

    def close_detail_pane(self) -> None:
        try:
            clicked = self.driver.execute_script(
                """
                const labels = new Set(['Close', 'Close dialog', 'Close order details']);
                const el = Array.from(document.querySelectorAll('button,[role="button"]'))
                  .find(node => labels.has((node.innerText || node.getAttribute('aria-label') || '').trim()));
                if (!el) return false;
                el.click();
                return true;
                """
            )
            if clicked:
                self.state.log_click("order-detail:Close")
        except Exception as exc:
            self.state.warn(f"could not close order detail pane: {exc!r}")

    def _open_pending_row(self, row_text: str) -> bool:
        for attempt in range(14):
            try:
                clicked = self.driver.execute_script(
                    """
                    const wanted = arguments[0];
                    const el = Array.from(document.querySelectorAll('button,[role="button"]'))
                      .find(b => (b.innerText || '').trim() === wanted);
                    if (!el) return false;
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return true;
                    """,
                    row_text,
                )
                if clicked:
                    self.state.log_click("activity:open-order-detail")
                    self._wait_for_order_detail()
                    return True
            except Exception as exc:
                self.state.warn(f"order row open error: {exc!r}")
                return False
            self.driver.execute_script("window.scrollBy(0, 700);")
            time.sleep(0.35)
        return False

    def _open_activity_row_here(
        self, row_text: str, source_control_id: str | None = None
    ) -> bool:
        """Open a row only if it is already rendered; never hunt from top."""
        try:
            clicked = self.driver.execute_script(
                """
                const wanted = arguments[0];
                const controlId = arguments[1];
                const exact = controlId ? document.getElementById(controlId) : null;
                const wantedParts = wanted.split('\\n').map(part => part.trim()).filter(Boolean);
                const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
                  .filter(b => {
                    const lines = (b.innerText || '').split('\\n').map(part => part.trim());
                    return wantedParts.every(part => lines.includes(part));
                  });
                const exactText = candidates.find(b => {
                  const lines = (b.innerText || '').split('\\n').map(part => part.trim()).filter(Boolean);
                  const r = b.getBoundingClientRect();
                  return lines.join('\\n') === wantedParts.join('\\n') && r.width > 0 && r.height > 0;
                });
                // A virtualized Activity card can receive a new id after
                // discovery. The Pending filter duplicates accessibility
                // wrappers; first visible disclosure is the benchmarked
                // read-only interaction, while parsing/matching stays strict.
                const el = exact && exact.matches('button,[role="button"]')
                  ? exact : (exactText || candidates.find(b => {
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  }) || null);
                if (!el || el.getAttribute('aria-expanded') !== 'false') return false;
                el.scrollIntoView({block:'center'});
                el.click();
                return true;
                """,
                row_text,
                source_control_id,
            )
            if clicked:
                self.state.log_click("activity:open-rendered-detail")
                self._wait_for_order_detail(row_text, source_control_id)
                return True
        except Exception as exc:
            self.state.warn(f"rendered activity row open error: {exc!r}")
        return False

    def _wait_for_order_detail(
        self,
        row_text: str | None = None,
        source_control_id: str | None = None,
        timeout: float = 1.5,
    ) -> None:
        """Wait only until the already-requested read-only detail drawer exists."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = self.driver.execute_script(
                    """
                    const wanted = arguments[0];
                    const controlId = arguments[1];
                    if (wanted) {
                      const exact = controlId ? document.getElementById(controlId) : null;
                      const wantedParts = wanted.split('\\n').map(part => part.trim()).filter(Boolean);
                      const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter(el => {
                          const lines = (el.innerText || '').split('\\n').map(part => part.trim());
                          return wantedParts.every(part => lines.includes(part))
                            && el.getAttribute('aria-expanded') === 'true';
                        });
                      const exactText = candidates.find(el => {
                        const lines = (el.innerText || '').split('\\n').map(part => part.trim()).filter(Boolean);
                        const r = el.getBoundingClientRect();
                        return lines.join('\\n') === wantedParts.join('\\n') && r.width > 0 && r.height > 0;
                      });
                      const header = exact && exact.matches('button,[role="button"]')
                        && exact.hasAttribute('aria-controls') && exact.getAttribute('aria-expanded') === 'true'
                        ? exact : (exactText || candidates.find(el => {
                          const r = el.getBoundingClientRect();
                          return r.width > 0 && r.height > 0;
                        }) || null);
                      if (!header) return false;
                      const region = header.hasAttribute('aria-controls')
                        ? document.getElementById(header.getAttribute('aria-controls')) : null;
                      const detail = region ? (region.innerText || '') : (document.body ? (document.body.innerText || '') : '');
                      const account = wantedParts.find(part =>
                        ['TFSA', 'RRSP', 'Non-registered'].includes(part));
                      const labelledAccount = account
                        ? detail.includes(`Account\\n${account}\\nStatus`) : false;
                      // Labels alone paint before their values. Waiting for
                      // the explicit Account -> account -> Status sequence
                      // prevents a first-frame snapshot that cannot be
                      // deterministically tied back to its pending card.
                      return labelledAccount
                        && ['Submitted', 'Expires', 'Trading session', 'Type', 'Entered quantity']
                          .every(field => detail.includes(field))
                        && detail.includes('Estimated total');
                    }
                    const text = document.body ? document.body.innerText : '';
                    return text.includes('Account') && text.includes('Status');
                    """,
                    row_text,
                    source_control_id,
                )
                if ready:
                    return
            except Exception:
                pass
            time.sleep(0.08)

    def close_activity_row_here(
        self, row_text: str, source_control_id: str | None = None
    ) -> bool:
        """Collapse an already-open Activity card via its own disclosure button."""
        try:
            clicked = self.driver.execute_script(
                """
                const wanted = arguments[0];
                const controlId = arguments[1];
                const exact = controlId ? document.getElementById(controlId) : null;
                const wantedParts = wanted.split('\\n').map(part => part.trim()).filter(Boolean);
                const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
                  .filter(el => {
                    const lines = (el.innerText || '').split('\\n').map(part => part.trim());
                    return wantedParts.every(part => lines.includes(part))
                      && el.getAttribute('aria-expanded') === 'true';
                  });
                const exactText = candidates.find(el => {
                  const lines = (el.innerText || '').split('\\n').map(part => part.trim()).filter(Boolean);
                  const r = el.getBoundingClientRect();
                  return lines.join('\\n') === wantedParts.join('\\n') && r.width > 0 && r.height > 0;
                });
                const element = exact && exact.matches('button,[role="button"]')
                  && exact.hasAttribute('aria-controls') && exact.getAttribute('aria-expanded') === 'true'
                  ? exact : (exactText || candidates.find(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  }) || null);
                if (!element) return false;
                element.click();
                return true;
                """,
                row_text,
                source_control_id,
            )
            if clicked:
                self.state.log_click("activity:collapse-rendered-detail")
            return bool(clicked)
        except Exception as exc:
            self.state.warn(f"could not collapse rendered activity detail: {exc!r}")
            return False

    def expanded_activity_detail_text(
        self, row_text: str, source_control_id: str | None = None
    ) -> str:
        """Return only the exact clicked card's detail region, not page text."""
        return self.driver.execute_script(
            """
            const wanted = arguments[0];
            const controlId = arguments[1];
            const exact = controlId ? document.getElementById(controlId) : null;
            const header = exact && exact.matches('button,[role="button"]')
              && exact.hasAttribute('aria-controls') && exact.getAttribute('aria-expanded') === 'true'
              ? exact
              : Array.from(document.querySelectorAll('button,[role="button"]'))
                  .find(el => !controlId && (el.innerText || '').trim() === wanted
                    && el.hasAttribute('aria-controls')
                    && el.getAttribute('aria-expanded') === 'true');
            if (!header) return '';
            const region = header.hasAttribute('aria-controls')
              ? document.getElementById(header.getAttribute('aria-controls')) : null;
            return region ? (region.innerText || '') : (document.body ? (document.body.innerText || '') : '');
            """,
            row_text,
            source_control_id,
        ) or ""

    def _find_safe_button(self, label: str):
        if label in DANGEROUS_CLICK_TEXT:
            return None
        found = self.driver.execute_script(
            """
            const label = arguments[0];
            const el = Array.from(document.querySelectorAll('button,[role="button"]'))
              .find(e => ((e.innerText || e.getAttribute('aria-label') || '').trim()) === label);
            if (!el) return false;
            el.setAttribute('data-codex-safe-button-target', '1');
            return true;
            """,
            label,
        )
        if not found:
            return None
        from selenium.webdriver.common.by import By
        return self.driver.find_element(By.CSS_SELECTOR, '[data-codex-safe-button-target="1"]')

    def _click(self, element) -> None:
        try:
            label = (self.driver.execute_script(
                "return (arguments[0].innerText || arguments[0].getAttribute('aria-label') || arguments[0].getAttribute('value') || '').trim();",
                element,
            ) or "").strip()
            lines = set(clean_lines(label))
            forbidden = sorted(lines & DANGEROUS_CLICK_TEXT)
            if forbidden:
                self.state.log_click(f"blocked-element:{'/'.join(forbidden)}", element.get_attribute("href"), blocked=True)
                self.state.block(f"blocked dangerous click target containing: {', '.join(forbidden)}")
                return
        except Exception as exc:
            self.state.warn(f"could not inspect click target before click: {exc!r}")
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)


def account_from_href(href: str) -> str:
    lower = href.lower()
    if "tfsa" in lower:
        return "TFSA"
    if "rrsp" in lower:
        return "RRSP"
    if "non-registered" in lower or "unregistered" in lower:
        return "Non-registered"
    return "Other"


def is_ignored_non_investing_account_href(href: str) -> bool:
    """Exclude Chequing from an explicitly investing-only read-only audit."""
    lower = href.lower()
    return any(marker in lower for marker in IGNORED_NON_INVESTING_ACCOUNT_MARKERS)


def home_investing_account_cards_ready(hrefs: list[str]) -> bool:
    """Return true when Home exposes links for the three investing accounts.

    Home paints account-card links before its dollar amounts finish hydrating,
    and the total can be split across separate DOM text nodes. Card identity is
    therefore the correct navigation readiness condition; account pages apply
    their own stricter money-value readiness gate before parsing balances.
    """
    found = {account_from_href(href) for href in hrefs}
    return set(ACCOUNTS) <= found


def parse_account_identity_and_balance(
    text: str,
    account: str,
    evidence: dict[str, str],
    tooltip_text: str = "",
    tooltip_evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    lines = clean_lines(text)
    value = extract_direct_account_value(lines)
    available = None
    # Only accept a money-shaped value. On a partly rendered page the line
    # after "Total cash available" is the next *label* ("Available CAD"), which
    # would otherwise be stored as a balance.
    for label in ["Total cash available", "Available to trade", "Available cash", "Buying power", "Cash"]:
        for i, line in enumerate(lines):
            if line == label and i + 1 < len(lines) and looks_like_money(lines[i + 1]):
                available = lines[i + 1]
                break
        if available:
            break
    available_cad = next(
        (lines[i + 1] for i, line in enumerate(lines)
         if line == "Available CAD" and i + 1 < len(lines) and looks_like_money(lines[i + 1])),
        None,
    )
    available_usd = next(
        (lines[i + 1] for i, line in enumerate(lines)
         if line == "Available USD" and i + 1 < len(lines) and looks_like_money(lines[i + 1])),
        None,
    )
    return {
        "account": account,
        "displayed_account_name": account,
        "account_type": account,
        "registered_classification": "registered" if account in {"TFSA", "RRSP"} else "non-registered/cash",
        "masked_account_identifier": None,
        "currencies_visible": sorted({c for c in ("CAD", "USD") if c in text}),
        "capture_timestamp": iso_now(),
        "source_url": evidence.get("url"),
        "total_account_value": value,
        "total_account_value_status": "directly_visible" if value else "not_directly_visible_not_inferred",
        "available_to_trade": available,
        "available_cash": available,
        "available_cash_cad": available_cad,
        "available_cash_usd": available_usd,
        "available_cash_semantics": "broker_displayed_available_trading_capacity",
        "cash_values_rendered": bool(available),
        "total_cash_available_semantics": parse_total_cash_available_definition(tooltip_text),
        "buying_power": available,
        "settled_cash": None,
        "settled_unsettled_cash_breakdown": "not_displayed_by_wealthsimple",
        "unsettled_cash": None,
        "reserved_or_unavailable_cash": None,
        "margin_buying_power": extract_margin_buying_power(lines),
        "balance_confirmed_from": "account detail reached from Home card",
        "evidence": [entry for entry in [evidence, tooltip_evidence] if entry],
    }


def total_cash_tooltip_is_exposed(text: str | None) -> bool:
    """True once the hovered total-cash tooltip has rendered its definition."""
    return bool(text) and TOTAL_CASH_TOOLTIP_PHRASE in text


def parse_total_cash_available_definition(text: str) -> dict[str, Any]:
    """Parse the account-page hover explanation for the CAD cash aggregate."""
    lines = clean_lines(text)
    explanation = next((line for line in lines if TOTAL_CASH_TOOLTIP_PHRASE in line), None)
    rate = next((line.removeprefix("Current rate: ") for line in lines if line.startswith("Current rate: ")), None)
    as_of = next((line.removeprefix("As of ") for line in lines if line.startswith("As of ")), None)
    return {
        "kind": "cad_presentation_of_combined_native_cad_and_usd_available_balances",
        "source": "Wealthsimple Total cash available tooltip",
        "explanation": explanation or "Tooltip unavailable; do not assume total cash available is native CAD cash.",
        "reference_fx_rate": rate,
        "reference_fx_as_of": as_of,
        "conversion_spread_included": False if explanation else None,
    }


def extract_direct_account_value(lines: list[str]) -> str | None:
    """Extract an account total only when Wealthsimple labels it explicitly.

    Account detail pages list the account name immediately before Positions, so
    taking the next dollar amount accidentally turns the first holding into
    the account total. Fail closed instead of inferring a financial balance.
    """
    labels = {"Total account value", "Account total", "Account value"}
    for index, line in enumerate(lines):
        if line not in labels:
            continue
        for candidate in lines[index + 1:index + 4]:
            if candidate.startswith("$") and ("CAD" in candidate or "USD" in candidate):
                return candidate
    return None


def extract_margin_buying_power(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if "buying power with margin" in line.lower() and i > 0:
            return lines[i - 1]
    return None


def parse_home_account_values(lines: list[str]) -> dict[str, str]:
    """Read explicitly visible account-card totals from Wealthsimple Home."""
    values: dict[str, str] = {}
    for account in ACCOUNTS:
        for index, line in enumerate(lines):
            if line != account:
                continue
            for candidate in lines[index + 1:index + 6]:
                if candidate == account:
                    continue
                # Home cards currently show a dollar amount without repeating
                # a currency label. Preserve the visible amount and record the
                # unlabeled status rather than inventing CAD/USD.
                if candidate.startswith("$"):
                    values[account] = candidate
                    break
            if account in values:
                break
    return values


def parse_holdings_from_account_texts(account_entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer rendered grid rows, fall back to captured page text.

    DOM rows carry the owning account slug and an explicit currency column, so
    they are the strongest evidence. Text parsing keeps browser-free rebuilds
    of an existing bundle working.
    """
    holdings: list[dict[str, Any]] = []
    for account, entry in account_entries.items():
        evidence = entry.get("evidence", [{}])[0]
        dom_rows = entry.get("holdings_rows_dom") or []
        parsed = parse_holdings_rows_from_dom(dom_rows, account, evidence)
        if not parsed:
            text_path = evidence.get("visible_text")
            if not text_path or not Path(text_path).exists():
                continue
            lines = clean_lines(Path(text_path).read_text(encoding="utf-8", errors="replace"))
            parsed = parse_holdings_lines(lines, account, evidence)
        holdings.extend(parsed)
    return holdings


def preload_readiness_is_stable(
    readiness: dict[str, Any] | None,
) -> bool:
    """Reject preload observations returned only because their wait timed out."""
    if not readiness:
        return False
    rows = int(readiness.get("holdings_rows") or 0)
    confirmations = int(
        readiness.get("holdings_rows_stable_confirmations") or 0
    )
    required = (
        HOLDINGS_GRID_STABLE_CONFIRMATIONS
        if rows > 0
        else EMPTY_HOLDINGS_GRID_CONFIRMATIONS
    )
    return confirmations >= required


def account_page_readiness_failures(
    readiness: dict[str, Any] | None,
    account: str,
    *,
    allow_empty_grid: bool = True,
) -> list[str]:
    """Explain every unmet read-only account-page readiness predicate."""
    if not readiness:
        return ["no_readiness_snapshot"]
    failures: list[str] = []
    if not readiness.get("account_name_visible"):
        failures.append("account_name_not_visible")
    if "/app/account-details/" not in (readiness.get("url") or ""):
        failures.append("not_an_account_detail_url")
    grid_rendered = bool(readiness.get("holdings_rows")) or (
        allow_empty_grid
        and (
            (
                bool(readiness.get("holdings_toolbar"))
                and "Holdings" in (readiness.get("column_headers") or [])
            )
            or bool(readiness.get("verified_empty_by_fresh_holdings_export"))
        )
    )
    if not grid_rendered:
        failures.append("holdings_grid_not_rendered")
    # Some accounts omit Available USD entirely when the native USD balance is
    # zero, and some omit Total cash available. A rendered native CAD balance
    # is sufficient after the holdings grid itself has stabilized.
    if not looks_like_money(readiness.get("available_cad_value")):
        failures.append("available_cad_value_not_rendered")
    return failures


def account_page_is_ready(readiness: dict[str, Any] | None, account: str, *, allow_empty_grid: bool = True) -> bool:
    """Accept a page only when it is the intended account, rendered, and priced.

    An empty portfolio is legitimate, so a rendered-but-rowless holdings grid
    counts as ready. Wealthsimple sometimes removes that component entirely
    after loading an empty account; a fresh all-accounts Holdings export may
    independently verify that layout as empty.
    """
    return not account_page_readiness_failures(
        readiness, account, allow_empty_grid=allow_empty_grid
    )


def account_slug_from_row_href(href: str | None) -> str | None:
    """Extract the account slug a holdings row's security link belongs to."""
    if not href:
        return None
    match = re.search(r"[?&]account=([A-Za-z0-9_.\-]+)", href)
    return match.group(1) if match else None


def parse_holdings_row_cells(
    cells: list[str],
    account: str,
    evidence: dict[str, str],
    row_testid: str | None = None,
    href: str | None = None,
) -> dict[str, Any] | None:
    """Parse one row of the 2026 account holdings grid.

    Observed live cell order (July 26 2026): ticker, optional repeated ticker,
    Currency, Allocation, Quantity, Price, Total value, Today's return,
    Today's return %. Price and Total value no longer carry a currency suffix,
    so the row's Currency column is the only currency evidence and the amount
    text must not be used to infer one.
    """
    values = [cell for cell in (cells or []) if cell]
    if values:
        # A focused/hovered row can expose its accessible action label as the
        # first cell (for example ``MP Details``) instead of the bare ticker.
        # The remaining currency/quantity/value cells are unchanged.
        details_match = re.fullmatch(r"([A-Z0-9.]+) Details", values[0])
        if details_match:
            values[0] = details_match.group(1)
    if not values or not looks_like_ticker(values[0]):
        return None
    ticker = values[0]
    rest = values[1:]
    if rest and rest[0] == ticker:
        rest = rest[1:]
    currency = next((cell for cell in rest if cell in {"CAD", "USD"}), None)
    if currency is None:
        return None
    tail = rest[rest.index(currency) + 1:]
    allocation = next((cell for cell in tail if re.match(r"^[0-9][0-9,.]*%$", cell)), None)
    quantity = next((cell for cell in tail if re.match(r"^[0-9][0-9,]*(\.[0-9]+)?$", cell)), None)
    # Low-priced securities can render sub-cent market prices (for example
    # $0.425) while total market value remains cents-based. Preserve either
    # form instead of dropping the complete holding row.
    amounts = [cell for cell in tail if re.match(r"^\$[0-9][0-9,]*(\.[0-9]+)?$", cell)]
    if quantity is None or len(amounts) < 2:
        return None
    price, market_value = amounts[0], amounts[1]
    quantity_label = f"{quantity} share" if quantity == "1" else f"{quantity} shares"
    return {
        "account": account,
        "account_type": account,
        "registered_classification": "registered" if account in {"TFSA", "RRSP"} else "non-registered/cash",
        "ticker": ticker,
        "normalized_ticker": normalize_ticker_for_cross_reference(ticker),
        "security_name": None,
        "classification": classify_security(None, ticker),
        "quantity": quantity_label,
        "current_price": f"{price} {currency}",
        "current_price_currency": currency,
        "market_value": f"{market_value} {currency}",
        "market_value_currency": currency,
        "allocation_percent": allocation,
        "average_cost": None,
        "total_cost": None,
        "unrealized_gain_loss_amount": None,
        "unrealized_gain_loss_percent": None,
        "day_change": None,
        "exchange": None,
        "holdings_row_testid": row_testid,
        "row_account_slug": account_slug_from_row_href(href),
        "detail_evidence_reference": None,
        "row_evidence_reference": evidence,
        "confirmation_level": "row_confirmed",
        "uncertainty_notes": [
            "the 2026 holdings grid does not print a security name; classification is ticker-only"
        ],
    }


def parse_holdings_rows_from_dom(
    rows: list[dict[str, Any]], account: str, evidence: dict[str, str]
) -> list[dict[str, Any]]:
    """Parse holdings from rendered grid rows, dropping rows from another account."""
    out: list[dict[str, Any]] = []
    expected_slug = None
    for row in rows or []:
        slug = account_slug_from_row_href(row.get("href"))
        if slug:
            expected_slug = expected_slug or slug
            if slug != expected_slug:
                continue
        parsed = parse_holdings_row_cells(
            row.get("cells") or [], account, evidence, row.get("testid"), row.get("href")
        )
        if parsed:
            out.append(parsed)
    return dedupe_by_key(out, ["account", "ticker", "quantity", "market_value"])


def find_holdings_table_start(lines: list[str]) -> int | None:
    """Locate the first data line of the 2026 holdings grid in page text."""
    for index, line in enumerate(lines):
        if line != "Holdings":
            continue
        window = lines[index:index + 9]
        if not ({"Currency", "Allocation", "Quantity"} <= set(window)):
            continue
        cursor = index
        while cursor < len(lines) and (
            lines[cursor] in HOLDINGS_TABLE_HEADER_LABELS or lines[cursor].startswith("Today")
        ):
            cursor += 1
        return cursor
    return None


def parse_holdings_table_lines(
    lines: list[str], account: str, evidence: dict[str, str]
) -> list[dict[str, Any]]:
    """Parse the 2026 "Stocks" holdings grid out of captured page text."""
    start = find_holdings_table_start(lines)
    if start is None:
        return []
    out: list[dict[str, Any]] = []
    current: list[str] = []

    def flush(cells: list[str]) -> None:
        parsed = parse_holdings_row_cells(cells, account, evidence)
        if parsed:
            out.append(parsed)

    cursor = start
    while cursor < len(lines):
        line = lines[cursor]
        if line in HOLDINGS_TABLE_STOP_MARKERS:
            break
        starts_new_row = looks_like_ticker(line) and current and not (
            len(current) == 1 and current[0] == line
        )
        if starts_new_row:
            flush(current)
            current = []
        current.append(line)
        cursor += 1
    flush(current)
    return dedupe_by_key(out, ["account", "ticker", "quantity", "market_value"])


def parse_holdings_lines(lines: list[str], account: str, evidence: dict[str, str]) -> list[dict[str, Any]]:
    modern = parse_holdings_table_lines(lines, account, evidence)
    if modern:
        return modern
    holdings: list[dict[str, Any]] = []
    try:
        start = lines.index("Positions")
    except ValueError:
        return holdings
    stop_markers = {"Recent activity", "Trade is offered by Wealthsimple Investments Inc. (WSII).", "Total cash available", "Watchlist"}
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if line in stop_markers:
            break
        if not looks_like_ticker(line):
            i += 1
            continue
        ticker = line
        j = i + 1
        if j < len(lines) and lines[j] == ticker:
            j += 1
        security_parts: list[str] = []
        while j < len(lines) and not (lines[j].startswith("$") and ("CAD" in lines[j] or "USD" in lines[j])):
            if lines[j] in stop_markers or looks_like_ticker(lines[j]):
                break
            security_parts.append(lines[j])
            j += 1
        if j >= len(lines) or not lines[j].startswith("$"):
            i += 1
            continue
        market_value = lines[j]
        qty = next((x for x in lines[j + 1:j + 8] if re.match(r"^[0-9,.]+ shares?$", x)), None)
        current_price = None
        if qty and qty in lines[j + 1:j + 8]:
            qidx = lines.index(qty, j + 1)
            if qidx + 1 < len(lines) and lines[qidx + 1].startswith("$"):
                current_price = lines[qidx + 1]
        security = " ".join(security_parts).strip() or None
        holdings.append({
            "account": account,
            "account_type": account,
            "registered_classification": "registered" if account in {"TFSA", "RRSP"} else "non-registered/cash",
            "ticker": ticker,
            "security_name": security,
            "classification": classify_security(security, ticker),
            "quantity": qty,
            "current_price": current_price,
            "current_price_currency": value_currency(current_price),
            "market_value": market_value,
            "market_value_currency": value_currency(market_value),
            "average_cost": None,
            "total_cost": None,
            "unrealized_gain_loss_amount": None,
            "unrealized_gain_loss_percent": None,
            "day_change": None,
            "exchange": None,
            "detail_evidence_reference": None,
            "row_evidence_reference": evidence,
            "confirmation_level": "row_confirmed",
        })
        i = max(j + 1, i + 1)
    return dedupe_by_key(holdings, ["account", "ticker", "quantity", "market_value"])


# A base symbol of up to six characters plus an optional exchange or share
# class suffix. The previous six-character cap silently rejected SHOP.TO, so
# every pending SHOP.TO order was dropped with no row, no warning and no
# count - a C$135.00 hole in the TFSA reconciliation across several captures.
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,5}(\.[A-Z0-9]{1,3})?$")
NON_TICKER_TOKENS = {"CAD", "USD", "ALL", "YTD"}


def looks_like_ticker(value: str) -> bool:
    return bool(TICKER_PATTERN.match(value)) and value not in NON_TICKER_TOKENS


def classify_security(security: str | None, ticker: str) -> str:
    text = f"{security or ''} {ticker}".lower()
    if "cdr" in text or "canadian depositary receipt" in text or "cad hedged" in text:
        return "CDR"
    if "etf" in text:
        return "ETF"
    return "common_or_unknown"


def parse_pending_rows_from_controls(controls: list[dict[str, Any]], evidence: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for control in controls:
        text = (control.get("text") or "").strip()
        parts = clean_lines(text)
        # Pending dividends/transfers can include an account, ticker-like
        # token, and amount. They are activity, not an executable order. Keep
        # unfamiliar buy/sell variants for detail confirmation, but never turn
        # a pending non-order event into an open-order ledger row.
        has_buy_or_sell_action = any(
            action in ORDER_ACTIONS or re.search(r"\b(?:buy|sell)\b", action, re.IGNORECASE)
            for action in parts
        )
        if not open_status_from_lines(parts) or not has_buy_or_sell_action:
            continue
        row = parse_order_row_text(text)
        if not row:
            continue
        row["source_list_page"] = evidence.get("url")
        row["screenshot_evidence_reference"] = evidence.get("screenshot")
        row["dom_visible_text_evidence_reference"] = evidence.get("visible_text")
        row["confirmation_level"] = "row_confirmed"
        # Wealthsimple Activity cards have stable UUID header IDs. Preserve
        # that identity so two real same-notional ladder legs cannot collapse
        # before their limit/quantity detail is available.
        control_id = control.get("id")
        row["source_control_id"] = control_id
        if control_id:
            row["stable_row_key"] = row_hash("pending-control", control_id)
            row["fingerprint"] = row["stable_row_key"]
        key = row["stable_row_key"]
        if key not in seen:
            seen.add(key)
            rows.append(row)
    duplicate_texts = len([control for control in controls if "Pending" in (control.get("text") or "")]) - len(rows)
    if duplicate_texts > 0:
        # Repeated controls are common in virtualized lists, but this keeps the
        # caller aware that exact duplicate pending rows may require detail views
        # to distinguish legitimate same-price orders from duplicate DOM entries.
        for row in rows:
            row.setdefault("uncertainty_notes", []).append("pending controls contained duplicate-looking row text during capture")
    return rows


def unparsed_pending_controls(controls: list[dict[str, Any]]) -> list[str]:
    """Return pending order cards that look real but produced no ledger row.

    A card carrying Pending, a recognised order action and a money amount is an
    order. If the row parser rejects it the order silently disappears from the
    ledger and from the cash reconciliation, which is how a SHOP.TO buy went
    missing. Report the card text so the next unparsable shape is visible.
    """
    missed: list[str] = []
    for control in controls:
        text = (control.get("text") or "").strip()
        parts = clean_lines(text)
        if not open_status_from_lines(parts):
            continue
        if not any(part in ORDER_ACTIONS or re.search(r"\b(?:buy|sell)\b", part, re.I) for part in parts):
            continue
        if parse_order_row_text(text) is None:
            missed.append(" / ".join(parts[:5]))
    return sorted(set(missed))


def unparsed_terminal_controls(controls: list[dict[str, Any]]) -> list[str]:
    """Expose final-status controls rejected by the Activity row parser."""
    missed: list[str] = []
    for control in controls:
        text = (control.get("text") or "").strip()
        parts = clean_lines(text)
        if not any(part in {"Cancelled", "Expired", "Rejected", "Failed"} for part in parts):
            continue
        if parse_activity_row_text(text) is None:
            missed.append(" / ".join(parts[:6]))
    return sorted(set(missed))


def parse_activity_rows_from_controls(controls: list[dict[str, Any]], evidence: dict[str, str]) -> list[dict[str, Any]]:
    """Parse Activity controls without treating final rows as orders.

    Wealthsimple's collapsed filled rows often omit an explicit ``Filled``
    label. We call those ``Completed/Filled`` only when a buy/sell action is
    present and none of the explicit non-final/failure statuses is visible.
    The exact fill quantity and execution price remain unconfirmed until a
    detail pane or export provides them.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for control in controls:
        text = (control.get("text") or "").strip()
        row = parse_activity_row_text(text)
        if not row:
            continue
        # Activity card text can be identical across separate dates. Include
        # the closest visible date heading to avoid silently conflating two
        # fills/cancels with the same account, ticker, action, and total.
        row["date"] = control.get("date_context") or None
        control_id = control.get("id")
        row["source_control_id"] = control_id
        row["stable_row_key"] = (
            row_hash("activity-control", control_id)
            if control_id else row_hash(
                row.get("account"), row.get("ticker") or row.get("activity_type"),
                row.get("activity_type"), row.get("total_value"), row.get("status"), row.get("date"),
            )
        )
        if row["stable_row_key"] in seen:
            continue
        seen.add(row["stable_row_key"])
        row["source_list_page"] = evidence.get("url")
        row["screenshot_evidence_reference"] = evidence.get("screenshot")
        row["dom_visible_text_evidence_reference"] = evidence.get("visible_text")
        rows.append(row)
    return rows


def activity_row_is_older_than_cutoff(row: dict[str, Any], today: date | None = None) -> bool:
    """Return true only for Activity rows known to predate the current year.

    The audit is scoped to the current calendar year, which also respects the
    requested no-more-than-365-day history bound. Unknown date headings are
    retained rather than guessed away.
    """
    value = (row.get("date") or "").strip()
    if not value or value in {"Today", "Yesterday"}:
        return False
    try:
        observed = datetime.strptime(value, "%B %d, %Y").date()
    except ValueError:
        return False
    now = today or date.today()
    return observed < date(now.year, 1, 1)


def parse_activity_row_text(text: str) -> dict[str, Any] | None:
    parts = clean_lines(text)
    account = next((part for part in parts if part in ACCOUNTS), None)
    action = next((part for part in parts if part in ACTIVITY_ACTIONS), None)
    amount = next((part for part in parts if looks_like_money(part)), None)
    explicit_status = next((part for part in parts if order_state(part) == "open" or part in {*ACTIVITY_FINAL_STATUSES, "In progress", "Upcoming payment"}), None)
    if not account or not action:
        return None
    ticker = None
    for part in parts:
        if part in {account, action, amount, explicit_status, "Transfer"} or looks_like_money(part) or part.startswith("From:") or part.startswith("To:"):
            continue
        if looks_like_ticker(part):
            ticker = part
            break
    side = "buy" if "buy" in action.lower() else "sell" if "sell" in action.lower() else None
    status = explicit_status
    if status is None and side:
        status = "Status unconfirmed"
    if status is None:
        status = "Activity recorded"
    movement_method = next((part for part in parts if part in {
        "Electronic funds transfer", "Interac e-Transfer", "Transfer",
    }), None)
    movement_direction = (
        "out" if any(part == "Withdrawal" for part in parts)
        else "in" if any(part in {"Deposit", "Recurring deposit"} for part in parts)
        else None
    )
    is_currency_conversion = action == "FX conversion"
    activity_domain = (
        "trade" if side
        else "currency_conversion" if is_currency_conversion
        else "cash_movement" if movement_method or movement_direction
        else "income_or_adjustment"
    )
    key = row_hash(account, ticker or action, action, amount, status)
    return {
        "stable_row_key": key,
        "account": account,
        "ticker": ticker,
        "activity_type": action,
        "side": side,
        "activity_domain": activity_domain,
        "movement_method": movement_method,
        "movement_direction": movement_direction,
        "status": status,
        "total_value": amount,
        "currency": value_currency(amount),
        "quantity": None,
        "execution_price": None,
        "date": None,
        "time": None,
        "detail_status": "row_confirmed",
        "exact_fill_fields_confirmed": False,
        "row_text": text,
        "uncertainty_notes": (
            ["collapsed Activity row did not show an explicit status; retained as status-unconfirmed and excluded from completed-fill conclusions"]
            if status == "Status unconfirmed" else []
        ),
    }


def cash_movement_row_is_complete(row: dict[str, Any]) -> bool:
    """Whether a collapsed cash-movement card proves the event sufficiently.

    Counterparty details are useful enrichment, but account, direction,
    method, status, date, and amount are the deterministic audit identity for
    an EFT row. These rows are not brokerage orders and do not require order
    quantity/price fields.
    """
    return bool(
        row.get("activity_domain") == "cash_movement"
        and row.get("account")
        and row.get("movement_direction") in {"in", "out"}
        and row.get("movement_method")
        and row.get("status")
        and row.get("date")
        and looks_like_money(row.get("total_value"))
    )


def terminal_activity_identity_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Collapsed-card identity used to detect rows needing disclosure evidence."""
    return (
        row.get("activity_domain"),
        row.get("account"),
        row.get("ticker"),
        row.get("side"),
        row.get("activity_type"),
        row.get("movement_direction"),
        row.get("movement_method"),
        row.get("status"),
        row.get("date"),
        row.get("total_value"),
    )


def terminal_activity_row_is_complete(row: dict[str, Any]) -> bool:
    """Whether a terminal row has its domain's complete collapsed identity."""
    if row.get("activity_domain") == "cash_movement":
        return cash_movement_row_is_complete(row)
    if row.get("activity_domain") == "trade":
        return bool(
            row.get("account")
            and row.get("ticker")
            and row.get("side") in {"buy", "sell"}
            and row.get("status")
            and row.get("date")
            and looks_like_money(row.get("total_value"))
        )
    return False


def parse_completed_activity_detail(text: str, row: dict[str, Any], evidence: dict[str, str]) -> dict[str, Any] | None:
    """Extract exact fill fields from a read-only completed Activity drawer."""
    lines = clean_lines(text)
    row_lines = clean_lines(row.get("row_text") or "")
    if not row_lines:
        return None
    start = next((index for index in range(len(lines)) if lines[index:index + len(row_lines)] == row_lines), -1)
    window = lines[start:start + 100] if start >= 0 else lines[:100]
    ticker = row.get("ticker") or ""
    stop = next((index + 1 for index, line in enumerate(window) if line == f"View {ticker} details"), len(window))
    window = window[:stop]

    def value(label: str) -> str | None:
        index = next((i for i, line in enumerate(window) if line == label), None)
        return window[index + 1] if index is not None and index + 1 < len(window) else None

    def timestamp(label: str) -> tuple[str | None, str | None]:
        index = next((i for i, line in enumerate(window) if line == label), None)
        if index is None or index + 1 >= len(window):
            return None, None
        date = window[index + 1]
        time_value = window[index + 2] if index + 2 < len(window) and re.search(r"\b[0-9]{1,2}:[0-9]{2}\b", window[index + 2]) else None
        return date, time_value

    account = value("Account")
    status = value("Status")
    filled_date, filled_time = timestamp("Filled")
    if not filled_date:
        filled_date, filled_time = timestamp("Completed")
    submitted_date, submitted_time = timestamp("Submitted")
    filled_quantity = value("Filled quantity")
    quantity = filled_quantity.split(" x ", 1)[0] if filled_quantity else value("Entered quantity") or value("Quantity")
    execution_price = filled_quantity.split(" x ", 1)[1] if filled_quantity and " x " in filled_quantity else value("Average price") or value("Price")
    total = next((value(label) for label in ["Total value", "Total cost", "Estimated proceeds", "Estimated total cost"] if value(label)), None)
    if not (account and status and (filled_date or submitted_date)):
        return None
    merged = dict(row)
    merged.update({
        "account": account,
        "status": status,
        "quantity": quantity,
        "execution_price": execution_price,
        "total_value": total or row.get("total_value"),
        "currency": value_currency(execution_price) or value_currency(total) or row.get("currency"),
        "date": filled_date or submitted_date,
        "time": filled_time or submitted_time,
        "submitted_date": submitted_date,
        "submitted_time": submitted_time,
        "filled_date": filled_date,
        "filled_time": filled_time,
        "detail_status": "detail_confirmed",
        "exact_fill_fields_confirmed": bool(quantity and execution_price and total),
        "execution_price_source": "filled_quantity" if filled_quantity and " x " in filled_quantity else "average_price_or_price_label",
        "detail_evidence_reference": evidence.get("visible_text"),
        "screenshot_evidence_reference": evidence.get("screenshot"),
        "uncertainty_notes": [],
    })
    return merged


def parse_terminal_activity_detail(
    text: str, row: dict[str, Any], evidence: dict[str, str]
) -> dict[str, Any] | None:
    """Extract deterministic identity from a terminal Activity disclosure."""
    lines = clean_lines(text)
    row_lines = clean_lines(row.get("row_text") or "")
    if not row_lines:
        return None
    start = next(
        (index for index in range(len(lines)) if lines[index:index + len(row_lines)] == row_lines),
        -1,
    )
    if start < 0:
        return None
    window = lines[start:start + 80]

    def value(label: str) -> str | None:
        index = next((i for i, line in enumerate(window) if line == label), None)
        return window[index + 1] if index is not None and index + 1 < len(window) else None

    account = value("From") or value("Account") or row.get("account")
    destination = value("To")
    status = value("Status")
    detail_date = value("Date")
    amount = value("Amount")
    if (
        account not in ACCOUNTS
        or status not in {"Cancelled", "Expired", "Rejected", "Failed"}
        or not detail_date
        or not looks_like_money(amount)
    ):
        return None
    merged = dict(row)
    merged.update({
        "account": account,
        "status": status,
        "date": detail_date,
        "total_value": amount,
        "currency": value_currency(amount),
        "from_account": account,
        "to_account": destination,
        "detail_status": "detail_confirmed",
        "confirmation_level": "detail_confirmed",
        "identity_confidence": "detail_confirmed",
        "detail_evidence_reference": evidence.get("visible_text"),
        "screenshot_evidence_reference": evidence.get("screenshot"),
        "uncertainty_notes": [],
    })
    return merged


def merge_activity_details(rows: list[dict[str, Any]], details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {detail.get("stable_row_key"): detail for detail in details}
    return [by_key.get(row.get("stable_row_key"), row) for row in rows]


def activity_sort_key(row: dict[str, Any]) -> tuple[int, str, str, str]:
    status_rank = {
        "Completed": 0,
        "Filled": 0,
        "Completed/Filled": 0,
        "Pending": 1,
        "Cancelled": 2,
        "Expired": 3,
        "Rejected": 4,
        "Failed": 4,
    }.get(row.get("status"), 9)
    return status_rank, row.get("account") or "", row.get("ticker") or "", row.get("activity_type") or ""


def parse_order_row_text(text: str) -> dict[str, Any] | None:
    parts = clean_lines(text)
    action = next((p for p in parts if p in ORDER_ACTIONS), None)
    inferred_action = next((p for p in parts if re.search(r"\b(?:buy|sell)\b", p, re.IGNORECASE)), None)
    action = action or inferred_action
    account = next((p for p in parts if p in ACCOUNTS), None)
    amount = next((p for p in parts if p.startswith("$") and ("CAD" in p or "USD" in p)), None)
    status = open_status_from_lines(parts)
    ticker = None
    for p in parts:
        if p in {action, account, amount, status, "Transfer"} or p.startswith("$") or p.startswith("From:") or p.startswith("To:"):
            continue
        if not looks_like_ticker(p):
            continue
        ticker = p
        break
    # A pending card with an unfamiliar buy/sell action must remain in the
    # audit rather than disappearing. Pending non-order events are filtered by
    # parse_pending_rows_from_controls before reaching this parser.
    if not (action and account and amount and status and ticker):
        return None
    side = "buy" if "buy" in action.lower() else "sell" if "sell" in action.lower() else "unknown"
    key = row_hash(account, ticker, action, amount, status)
    return {
        "stable_row_key": key,
        "fingerprint": key,
        "account": account,
        "account_type": account,
        "registered_vs_nonregistered": "registered" if account in {"TFSA", "RRSP"} else "non-registered/cash",
        "ticker": ticker,
        "security_name": None,
        "side": side,
        "side_label": action,
        "order_type": "limit" if "Limit" in action else "market" if "Market" in action else "unknown",
        "limit_price": None,
        "stop_price": None,
        "quantity": None,
        "filled_quantity": None,
        "remaining_quantity": None,
        "submitted_date": None,
        "submitted_time": None,
        "expiry": None,
        "status": status,
        "current_market_price": None,
        "estimated_cost_or_proceeds": amount,
        "estimated_total": amount,
        "settlement_currency": value_currency(amount),
        "reserved_cash_impact": {value_currency(amount) or "UNKNOWN": money_to_float(amount)} if side == "buy" else None,
        "order_currency": value_currency(amount),
        "account_currency": None,
        "fx_conversion": None,
        "detail_url_or_page_label": None,
        "confirmation_level": "row_confirmed",
        "uncertainty_notes": (["pending Activity card exposed an unfamiliar buy/sell order type; detail confirmation required"] if action not in ORDER_ACTIONS else []),
        "row_text": text,
    }


def parse_order_detail_blocks(
    text: str,
    evidence: dict[str, str],
    *,
    ticker_hint: str | None = None,
) -> list[dict[str, Any]]:
    lines = clean_lines(text)
    out: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        if line != "Account" or idx + 4 >= len(lines) or lines[idx + 2] != "Status":
            continue
        account = lines[idx + 1]
        status = lines[idx + 3]
        if account not in ACCOUNTS:
            continue
        # Never borrow another order's quantity or security from the next block.
        end = next((j for j in range(idx + 4, len(lines)) if lines[j] == "Account"), len(lines))
        window = lines[idx:min(end, idx + 90)]
        submitted = join_label_value(window, "Submitted", 2)
        expiry = join_label_value(window, "Expires", 2)
        type_label = value_after_label(window, "Type")
        limit_price = value_after_label(window, "Limit price")
        quantity = value_after_label(window, "Entered quantity")
        def single_label(label: str) -> str | None:
            values = [window[j + 1] if j + 1 < len(window) else ""
                      for j, text in enumerate(window) if text == label]
            # Duplicate labels may belong to history/replacement state. Reject
            # rather than choosing the first even when the values look equal.
            return values[0] if len(values) == 1 else "ambiguous duplicate label" if values else None
        quantities = quantity_evidence(
            single_label("Entered quantity"), single_label("Filled quantity"),
            single_label("Remaining quantity"),
        )
        estimated_pair = first_label_value_by_prefix(window, "Estimated total")
        if not (type_label and estimated_pair):
            continue
        estimated_label, estimated_total = estimated_pair
        ticker = None
        for candidate in window:
            match = re.match(r"View (.+) details", candidate)
            if match:
                ticker = match.group(1)
                break
        # The exact expanded region sometimes says only "View details" while
        # the surrounding page says "View BAM.TO details". The caller already
        # knows the clicked row's ticker, so use that explicit context rather
        # than downgrading an otherwise complete detail record.
        ticker = ticker or ticker_hint
        if not ticker:
            continue
        side = "buy" if "buy" in type_label.lower() else "sell"
        key = row_hash(account, ticker, type_label, estimated_total, status)
        full_detail = bool(submitted and expiry and limit_price and quantity)
        uncertainty_notes: list[str] = []
        if not full_detail:
            missing = [
                name for name, value in [
                    ("Submitted", submitted),
                    ("Expires", expiry),
                    ("Limit price", limit_price),
                    ("Entered quantity", quantity),
                ] if not value
            ]
            uncertainty_notes.append("detail pane truncated; missing " + ", ".join(missing))
        if quantities["remaining_quantity"] is None:
            uncertainty_notes.append("remaining sell/buy quantity not established by captured labels")
        out.append({
            "stable_row_key": key,
            "fingerprint": key,
            "account": account,
            "account_type": account,
            "registered_vs_nonregistered": "registered" if account in {"TFSA", "RRSP"} else "non-registered/cash",
            "ticker": ticker,
            "security_name": None,
            "side": side,
            "side_label": type_label,
            "order_type": "limit" if "Limit" in type_label else "market",
            "limit_price": limit_price,
            "stop_price": None,
            "quantity": quantity,
            **quantities,
            "submitted_date": split_timestamp_value(submitted)[0],
            "submitted_time": split_timestamp_value(submitted)[1],
            "expiry": expiry,
            "status": status,
            "current_market_price": None,
            "estimated_cost_or_proceeds": estimated_total,
            "estimated_total": estimated_total,
            "estimated_label": estimated_label,
            # The limit quote can be USD while Wealthsimple reserves a CAD
            # estimated cost for the paired CAD account. Reservation and cash
            # pressure must therefore follow the estimated-total currency,
            # not the security quote/limit-price currency.
            "settlement_currency": value_currency(estimated_total),
            "reserved_cash_impact": {value_currency(estimated_total) or "UNKNOWN": money_to_float(estimated_total)} if side == "buy" else None,
            "order_currency": value_currency(limit_price) or value_currency(estimated_total),
            "security_quote_currency": value_currency(limit_price),
            "account_currency": None,
            "fx_conversion": None,
            "source_list_page": evidence.get("url"),
            "detail_url_or_page_label": evidence.get("url"),
            "screenshot_evidence_reference": evidence.get("screenshot"),
            "dom_visible_text_evidence_reference": evidence.get("visible_text"),
            "confirmation_level": "detail_confirmed" if full_detail else "row_confirmed",
            "uncertainty_notes": uncertainty_notes,
        })
    return out


def value_after_label(lines: list[str], label: str) -> str | None:
    for idx, line in enumerate(lines):
        if line == label and idx + 1 < len(lines):
            return lines[idx + 1]
    return None


def split_timestamp_value(value: str | None) -> tuple[str | None, str | None]:
    """Split a joined "<date> <time>" detail value into date and time.

    Wealthsimple separates the clock value from its meridiem with a
    non-breaking space ("12:08\u00a0pm"), so splitting on the last two ASCII
    spaces moved the year out of the date and into the time
    ("July 2," / "2026 12:08\u00a0pm"). Anchor on the clock token instead.
    """
    if not value:
        return None, None
    match = re.search(r"\b\d{1,2}:\d{2}\b", value)
    if not match:
        return value.strip() or None, None
    date_part = value[: match.start()].strip().rstrip(",").strip()
    time_part = value[match.start():].strip()
    return date_part or None, time_part or None


def join_label_value(lines: list[str], label: str, count: int) -> str | None:
    for idx, line in enumerate(lines):
        if line == label and idx + count < len(lines):
            return " ".join(lines[idx + 1:idx + 1 + count])
    return None


def first_label_value_by_prefix(lines: list[str], prefix: str) -> tuple[str, str] | None:
    for idx, line in enumerate(lines):
        if line.startswith(prefix) and idx + 1 < len(lines):
            return line, lines[idx + 1]
    return None


def _matching_order_detail(row: dict[str, Any], details: list[dict[str, Any]], *, allow_unbound: bool = False) -> dict[str, Any] | None:
    candidates = []
    for detail in details:
        if any(detail.get(k) != row.get(k) for k in ("account", "ticker", "side", "estimated_total")):
            continue
        row_id, detail_id = row.get("source_control_id"), detail.get("source_control_id")
        if row_id and detail_id and row_id != detail_id:
            continue
        bound = bool((row_id and row_id == detail_id) or (
            row.get("stable_row_key") and detail.get("detail_capture_row_key") == row["stable_row_key"]))
        if bound or (allow_unbound and not detail_id and not detail.get("detail_capture_row_key")):
            candidates.append(detail)
    return candidates[0] if len(candidates) == 1 else None


def find_matching_detail(row: dict[str, Any], details: list[dict[str, Any]], *, allow_unbound: bool = False) -> dict[str, Any] | None:
    detail = _matching_order_detail(row, details, allow_unbound=allow_unbound)
    if detail is None:
        return None
    merged = {**row, **detail}
    merged["stable_row_key"] = row["stable_row_key"]
    merged["fingerprint"] = row["stable_row_key"]
    merged["source_control_id"] = row.get("source_control_id")
    merged["detail_capture_row_key"] = row["stable_row_key"]
    return merged


def order_priority_key(row: dict[str, Any]) -> tuple[int, int, str]:
    account_rank = {"RRSP": 0, "TFSA": 1, "Non-registered": 2}.get(row.get("account"), 9)
    side_rank = 0 if row.get("side") == "sell" else 1
    return account_rank, side_rank, row.get("ticker") or ""


def merge_rows_and_details(rows: list[dict[str, Any]], details: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    used_details: set[int] = set()
    merged: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        raw = _matching_order_detail(row, [d for d in details if id(d) not in used_details])
        match = find_matching_detail(row, [raw]) if raw is not None else None
        if match:
            used_details.add(id(raw))
            match["priority"] = classify_order_priority(match)
            merged.append(match)
        else:
            copy = dict(row)
            copy["priority"] = classify_order_priority(copy)
            copy["confirmation_level"] = "row_confirmed"
            copy.setdefault("uncertainty_notes", []).append("order detail not confirmed")
            unresolved.append(copy)
            merged.append(copy)
    return merged, unresolved


def reconcile_unresolved_order_records(
    rows: list[dict[str, Any]],
    capture_unresolved: list[dict[str, Any]],
    merge_unresolved: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine two views of the same unresolved pending orders once.

    ``merge_unresolved`` carries final ledger fields while
    ``capture_unresolved`` carries the concrete read-only capture failure.
    Keep both, preserve source-row order, and never silently collapse an
    unkeyed record.
    """
    capture_by_key = {
        str(record["stable_row_key"]): record
        for record in capture_unresolved
        if record.get("stable_row_key")
    }
    merge_by_key = {
        str(record["stable_row_key"]): dict(record)
        for record in merge_unresolved
        if record.get("stable_row_key")
    }
    merged: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for row in rows:
        key = str(row.get("stable_row_key") or "")
        if not key or key in emitted:
            continue
        record = merge_by_key.get(key) or dict(capture_by_key.get(key) or {})
        if not record:
            continue
        capture = capture_by_key.get(key)
        if capture and capture.get("reason"):
            record["reason"] = capture["reason"]
        merged.append(record)
        emitted.add(key)
    # Preserve unexpected/unkeyed diagnostics instead of hiding a collector
    # regression merely to make a warning count tidy.
    for record in merge_unresolved + capture_unresolved:
        key = str(record.get("stable_row_key") or "")
        if not key:
            merged.append(dict(record))
        elif key not in emitted:
            merged.append(dict(record))
            emitted.add(key)
    return merged


def classify_order_priority(order: dict[str, Any]) -> str:
    if order.get("side") == "sell":
        return "P0 EXIT"
    account = order.get("account")
    ticker = order.get("ticker")
    if account == "RRSP" and ticker in {"GOOG", "AVGO", "NVDA", "LUN", "RKLB", "SPCX"}:
        return "P1 CORE"
    if account == "TFSA" and ticker in {"ADBE", "CRM", "MSFT", "META", "SHOP", "LMT", "MDA"}:
        return "P2 TACTICAL"
    if account == "TFSA" and ticker in {"CTC.A", "T", "MP", "PYPL", "NOWS", "SPIR", "UFO", "SPCX", "RKLB", "MAXQ"}:
        return "P3 CANCEL-FIRST"
    return "UNCLASSIFIED"


def parse_recent_activity_from_text(text: str, evidence: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in re.split(r"\n(?=[A-Z][A-Z0-9.]{0,5}\n)", text):
        parsed = parse_order_row_text(block)
        if not parsed:
            continue
        parsed["activity_type"] = parsed["side_label"]
        parsed["date"] = None
        parsed["time"] = None
        parsed["detail_evidence_reference"] = evidence.get("visible_text")
        parsed["export_evidence_reference"] = None
        rows.append(parsed)
    return dedupe_by_key(rows, ["account", "ticker", "side_label", "estimated_total", "status"])


def clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def dedupe_by_key(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(row.get(k) for k in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def compute_cash_reserve(accounts: list[dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, Any]:
    account_map = {a["account"]: a for a in accounts}
    out: dict[str, Any] = {}
    for account in ACCOUNTS:
        account_orders = [o for o in orders if o.get("account") == account]
        buys = [o for o in account_orders if o.get("side") == "buy"]
        sells = [o for o in account_orders if o.get("side") == "sell"]
        buy_totals = money_totals_by_currency(buys)
        sell_totals = money_totals_by_currency(sells)
        available_after_pending = {
            currency: money_to_float(account_map.get(account, {}).get(field))
            for currency, field in {"CAD": "available_cash_cad", "USD": "available_cash_usd"}.items()
            if account_map.get(account, {}).get(field)
        }
        reconstructed_before_open_buy_holds = dict(available_after_pending)
        for currency, amount in buy_totals.items():
            reconstructed_before_open_buy_holds[currency] = round(reconstructed_before_open_buy_holds.get(currency, 0.0) + amount, 2)
        out[account] = {
            "displayed_total_account_value": account_map.get(account, {}).get("total_account_value"),
            "displayed_available_cash_or_buying_power": account_map.get(account, {}).get("available_to_trade"),
            "displayed_unavailable_or_reserved_cash": account_map.get(account, {}).get("reserved_or_unavailable_cash"),
            "sum_estimated_open_buy_costs": None,
            "sum_estimated_open_buy_costs_by_currency": buy_totals,
            "estimated_pending_buy_commitments_by_settlement_currency": buy_totals,
            "sum_estimated_open_sell_proceeds": None,
            "sum_estimated_open_sell_proceeds_by_currency": sell_totals,
            "calculated_reserved_cash_from_pending_buys": None,
            "calculated_reserved_cash_from_pending_buys_by_currency": buy_totals,
            "broker_displayed_available_trading_capacity_by_currency": available_after_pending,
            "reconstructed_cash_before_open_buy_holds_by_currency": reconstructed_before_open_buy_holds,
            "difference_displayed_reserved_vs_calculated": None,
            "orders_with_unknown_settlement_currency": [
                o.get("stable_row_key") for o in account_orders
                if not (o.get("settlement_currency") or value_currency(o.get("estimated_total")))
            ],
            "orders_included_in_reserve": [o.get("stable_row_key") for o in buys],
            "orders_excluded": [{"stable_row_key": o.get("stable_row_key"), "reason": "sell order proceeds not cash reserve"} for o in sells],
            "warning": "Reconstructed native-currency cash equals displayed Available CAD/USD plus open-buy estimated totals settled in that same currency. It intentionally does not use Total cash available, because Wealthsimple presents that field as a CAD conversion of combined CAD and USD balances. Reconstructed values use current order estimates and may differ from settled cash or broker holds if an order changes.",
        }
    return out


def money_totals_by_currency(orders: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for order in orders:
        currency = order.get("settlement_currency") or value_currency(order.get("estimated_total")) or "UNKNOWN"
        totals[currency] = round(totals.get(currency, 0.0) + money_to_float(order.get("estimated_total")), 2)
    return totals


def load_user_account_context(today: date | None = None) -> dict[str, Any]:
    """Load explicit user-provided account capability facts without inferring them from balances."""
    if not USER_ACCOUNT_CONTEXT_PATH.is_file():
        return {}
    try:
        context = json.loads(USER_ACCOUNT_CONTEXT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(context, dict):
        return {}
    trial = context.get("usd_trading_accounts")
    if (
        isinstance(trial, dict)
        and trial.get("status") == "access_expired_conversion_grace_user_reported"
        and trial.get("access_ended")
    ):
        now = today or date.today()
        trial["evidence_basis"] = "user_reported_not_verified_in_app_by_this_capture"
        try:
            access_ended = date.fromisoformat(trial["access_ended"])
            grace_days = int(trial.get("forced_conversion_after_days") or 0)
        except (TypeError, ValueError):
            trial["review_required"] = True
            trial["interpretation"] = (
                "The user reports that USD trading access ended, but the conversion grace period "
                "could not be parsed. The residual USD balance is not directly tradable."
            )
            return context
        conversion_due = access_ended + timedelta(days=grace_days)
        trial["expired_at_capture"] = True
        trial["forced_conversion_on"] = conversion_due.isoformat()
        trial["days_until_forced_conversion"] = (conversion_due - now).days
        trial["review_required"] = now > conversion_due
        trial["directly_tradable_usd"] = False
        trial["interpretation"] = (
            f"The user reports that USD-account access ended on {access_ended.isoformat()}. "
            f"Native USD balances remain held as USD during a {grace_days}-day conversion grace "
            f"period but cannot be used for USD trading. Wealthsimple is expected to force-convert "
            f"the residual USD on or after {conversion_due.isoformat()}. Do not count displayed USD "
            "as directly tradable capacity."
        )
        if trial["review_required"]:
            trial["interpretation"] += " Reconfirm whether the forced conversion has occurred."
        return context
    if isinstance(trial, dict) and trial.get("trial_expires"):
        now = today or date.today()
        trial["evidence_basis"] = "user_reported_not_verified_in_app_by_this_capture"
        try:
            expires = date.fromisoformat(trial["trial_expires"])
        except ValueError:
            trial["review_required"] = True
            trial["expired_at_capture"] = None
            trial["interpretation"] = (
                "trial_expires could not be parsed, so USD trading access is unverified. "
                "A USD balance alone does not prove USD trading-account access."
            )
            return context
        trial["expired_at_capture"] = now > expires
        trial["days_until_expiry"] = (expires - now).days
        trial["review_required"] = now > expires
        if trial["review_required"]:
            # Wealthsimple keeps an unused USD cash balance visible after USD
            # accounts are disabled while blocking its use for USD trades, so an
            # expired trial must not leave a tradable-capacity claim standing.
            trial["interpretation"] = (
                f"The user-reported USD-account trial expired on {trial['trial_expires']}. "
                "A USD balance alone does not prove USD trading-account access, and a residual USD "
                "balance can remain visible while being unusable for USD trades. Treat USD available "
                "balances as unverified for trading until USD-account access is reconfirmed."
            )
    return context


def is_non_trading_day(day: date | None) -> bool:
    """True for a weekend capture, when no North American session is open.

    Statutory holidays are not knowable here, so this is a lower bound: a False
    result does not prove the markets were open.
    """
    return bool(day) and day.weekday() >= 5


def analyse_residual(
    residual: float, total: float, buys_by_currency: dict[str, float], rate: float | None,
    capture_date: date | None = None,
) -> dict[str, Any]:
    """Test a reconciliation residual against a foreign-exchange explanation.

    A pending USD-denominated buy is the only place an FX spread could hide, so
    the residual is measured against the largest spread Wealthsimple publishes.
    Wealthsimple applies the conversion and its fee only when an order fills, and
    charges no conversion fee on US-listed trades funded from a USD account, so
    an unfilled order's reserve should carry no spread at all - this bound is
    deliberately generous.
    """
    foreign_holds = {c: v for c, v in buys_by_currency.items() if c not in {"CAD", "UNKNOWN"}}
    foreign_holds_cad = round(sum(v * rate for v in foreign_holds.values()), 2) if rate else 0.0
    spread_ceiling = round(foreign_holds_cad * MAX_DOCUMENTED_FX_CONVERSION_FEE, 2)
    remaining = round(abs(residual) - spread_ceiling, 2)
    analysis: dict[str, Any] = {
        "residual_cad": residual,
        "residual_percent_of_account_total": round(abs(residual) / total * 100, 4) if total else None,
        "foreign_currency_open_buy_holds": foreign_holds,
        "foreign_currency_open_buy_holds_cad": foreign_holds_cad,
        "max_fx_spread_could_explain_cad": spread_ceiling,
        "residual_after_max_fx_spread_cad": remaining,
        "fee_basis": "documented ceiling only; no fee is asserted, charged, or added to any figure",
        "reference": "https://www.wealthsimple.com/en-ca/legal/fees/trade",
    }
    if abs(residual) <= 1.0:
        analysis["classification"] = "reconciled"
        return analysis
    if not foreign_holds:
        analysis["classification"] = "unexplained_no_foreign_currency_holds"
        analysis["note"] = "No foreign-currency open-buy holds exist, so an FX spread cannot explain this residual."
        return analysis
    if remaining <= 1.0:
        analysis["classification"] = "within_documented_fx_spread_bound"
        analysis["note"] = "The residual is no larger than the documented maximum conversion fee on the foreign-currency holds."
        return analysis
    if capture_date is not None:
        # Recorded as context only. An earlier build dismissed weekend residuals
        # as snapshot noise; a C$135.00 pending SHOP.TO buy dropped by the
        # ticker parser turned out to explain the residual instead, so a
        # non-trading day must not downgrade the finding.
        analysis["capture_weekday"] = capture_date.strftime("%A")
        analysis["capture_on_non_trading_day"] = is_non_trading_day(capture_date)
    analysis["classification"] = "unexplained_not_fx_spread"
    if rate:
        analysis["implied_rate_to_close_on_foreign_holds"] = round(
            (foreign_holds_cad + abs(residual)) / (foreign_holds_cad / rate), 5
        )
        analysis["implied_rate_multiple_of_reference"] = round(
            analysis["implied_rate_to_close_on_foreign_holds"] / rate, 4
        )
    analysis["note"] = (
        "An FX spread on the foreign-currency open-buy holds cannot explain this residual: "
        "closing it would require a conversion rate far above the displayed reference rate, "
        "and Wealthsimple applies conversion fees only at fill."
    )
    return analysis


def reconciliation_status(
    residual: float, total: float, analysis: dict[str, Any], components_captured: bool
) -> str:
    """Grade a residual by evidence, so only material gaps read as findings.

    Nothing is hidden: every status keeps the residual and its analysis. The
    grades separate "explained", "inside a documented bound" and "small enough
    to be snapshot drift with every component captured" from a genuine gap.
    """
    magnitude = abs(residual)
    if magnitude <= 1.0:
        return "reconciled"
    if analysis.get("classification") == "within_documented_fx_spread_bound":
        return "within_documented_fx_bound"
    if components_captured and magnitude <= max(5.0, round(abs(total) * 0.0025, 2)):
        return "minor_snapshot_residual"
    return "residual_unexplained"


def reconcile_total_account_value(
    account: dict[str, Any], holdings: list[dict[str, Any]], orders: list[dict[str, Any]],
    capture_date: date | None = None, components_captured: bool = True,
) -> dict[str, Any]:
    """Compare the broker's account total against its visible components.

    Wealthsimple's displayed available cash excludes cash held for pending
    limit buys, so the expected identity is
    total = total cash available + holdings market value + open-buy commitments,
    all expressed in CAD at the tooltip's reference rate. Any residual is
    reported rather than absorbed, because an unexplained gap means some asset
    or hold is not represented in this bundle.
    """
    name = account.get("account")
    semantics = account.get("total_cash_available_semantics") or {}
    rate = None
    try:
        rate = float(semantics.get("reference_fx_rate")) if semantics.get("reference_fx_rate") else None
    except (TypeError, ValueError):
        rate = None
    total = money_to_float(account.get("total_account_value")) or None
    if total is None:
        return {"status": "account_total_not_visible"}
    account_holdings = [h for h in holdings if h.get("account") == name]
    account_buys = [o for o in orders if o.get("account") == name and o.get("side") == "buy"]

    def by_currency(rows, amount_field, currency_field):
        totals: dict[str, float] = {}
        for row in rows:
            currency = row.get(currency_field) or value_currency(row.get(amount_field)) or "UNKNOWN"
            totals[currency] = round(totals.get(currency, 0.0) + money_to_float(row.get(amount_field)), 2)
        return totals

    holdings_by_currency = by_currency(account_holdings, "market_value", "market_value_currency")
    buys_by_currency = by_currency(account_buys, "estimated_total", "settlement_currency")
    cad_position_controls = {
        normalize_ticker_for_cross_reference(str(row.get("ticker") or "")): row
        for row in (account.get("authenticated_position_valuations_cad") or [])
        if row.get("ticker") and money_to_float(row.get("market_value_cad"))
    }
    controlled_foreign_holdings: list[dict[str, Any]] = []
    uncontrolled_foreign_holdings: list[dict[str, Any]] = []
    direct_foreign_cad_total = 0.0
    for holding in account_holdings:
        currency = (
            holding.get("market_value_currency")
            or value_currency(holding.get("market_value"))
            or "UNKNOWN"
        )
        if currency in {"CAD", "UNKNOWN"}:
            continue
        ticker = normalize_ticker_for_cross_reference(
            str(holding.get("ticker") or "")
        )
        control = cad_position_controls.get(ticker)
        if control:
            cad_value = money_to_float(control.get("market_value_cad"))
            direct_foreign_cad_total += cad_value
            controlled_foreign_holdings.append({
                "ticker": ticker,
                "market_value": holding.get("market_value"),
                "market_value_currency": currency,
                "market_value_cad": round(cad_value, 6),
                "source": control.get("source"),
                "captured_at": control.get("captured_at"),
            })
        else:
            uncontrolled_foreign_holdings.append(holding)
    # Cash that Wealthsimple did not render must never enter the identity as
    # zero: that turns a measurement gap into a fake residual. Prefer the
    # displayed aggregate, fall back to the displayed native balances, and
    # otherwise decline to reconcile.
    cash_source = "displayed_total_cash_available"
    if account.get("available_to_trade"):
        cash_cad_aggregate = money_to_float(account.get("available_to_trade"))
    elif account.get("available_cash_cad"):
        native_usd = money_to_float(account.get("available_cash_usd"))
        cash_cad_aggregate = round(
            money_to_float(account.get("available_cash_cad"))
            + (native_usd * rate if rate is not None else 0.0),
            2,
        )
        cash_source = "derived_from_displayed_native_balances"
    else:
        return {
            "status": "not_reconcilable_cash_not_captured",
            "displayed_total_account_value": total,
            "holdings_market_value_by_currency": holdings_by_currency,
            "open_buy_commitments_by_currency": buys_by_currency,
            "reference_fx_rate": rate,
            "note": (
                "Total cash available and native available balances were not captured for this account, "
                "so no residual is computed. Absent cash is not treated as zero."
            ),
        }
    foreign_components = bool(uncontrolled_foreign_holdings) or any(
        currency not in {"CAD", "UNKNOWN"} for currency in buys_by_currency
    ) or (
        cash_source == "derived_from_displayed_native_balances"
        and money_to_float(account.get("available_cash_usd")) != 0
    )
    if rate is None and foreign_components:
        return {
            "status": "not_reconcilable_without_reference_fx_rate",
            "displayed_total_account_value": total,
            "total_cash_available_cad_aggregate": cash_cad_aggregate,
            "total_cash_available_source": cash_source,
            "holdings_market_value_by_currency": holdings_by_currency,
            "open_buy_commitments_by_currency": buys_by_currency,
            "authenticated_foreign_position_valuations_cad": controlled_foreign_holdings,
            "note": "Native cash was captured, but a current reference FX rate is required to convert foreign-currency components into CAD.",
        }
    holdings_cad = sum(
        value for currency, value in holdings_by_currency.items()
        if currency in {"CAD", "UNKNOWN"}
    ) + direct_foreign_cad_total
    if rate is not None:
        holdings_cad += sum(
            money_to_float(row.get("market_value")) * rate
            for row in uncontrolled_foreign_holdings
        )
    buys_cad = sum(
        value if currency in {"CAD", "UNKNOWN"} else value * rate
        for currency, value in buys_by_currency.items()
    )
    components = round(cash_cad_aggregate + holdings_cad + buys_cad, 2)
    residual = round(total - components, 2)
    analysis = analyse_residual(residual, total, buys_by_currency, rate, capture_date)
    return {
        "status": reconciliation_status(residual, total, analysis, components_captured),
        "displayed_total_account_value": total,
        "total_cash_available_cad_aggregate": cash_cad_aggregate,
        "total_cash_available_source": cash_source,
        "holdings_market_value_by_currency": holdings_by_currency,
        "open_buy_commitments_by_currency": buys_by_currency,
        "reference_fx_rate": rate,
        "authenticated_foreign_position_valuations_cad": controlled_foreign_holdings,
        "foreign_holdings_direct_cad_total": round(direct_foreign_cad_total, 6),
        "components_total_cad": components,
        "residual_cad": residual,
        "components_captured": components_captured,
        "residual_analysis": analysis,
        "note": (
            "Account total is fully explained by visible cash, holdings, and open-buy commitments."
            if residual == 0 else
            "Residual is not explained by visible cash, holdings, and open-buy commitments. "
            "See residual_analysis for what has been excluded; this capture does not "
            "identify the cause and does not attribute it to a fee."
        ),
    }


EXPORT_AS_OF_PREFIX = "As of "
# The observed browser/export pairs differ by up to one calendar day. Keep the
# tolerance no wider: a broader window could let a separate fill of the same
# size absorb a duplicate. The booking cause remains unconfirmed.
BLOCKING_MODAL_ESCAPE_ATTEMPTS = 2
BLOCKING_MODAL_SETTLE_SECONDS = 0.4
# Only the heading is read back: dialog bodies can contain balances, and a
# blocked-run message is not a place to put them.
BLOCKING_MODAL_SCRIPT = """
const dialog = Array.from(document.querySelectorAll('[role="dialog"][aria-modal="true"]'))
  .filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden';
  })
  .pop();
if (!dialog) return null;
const heading = dialog.querySelector('h1,h2,h3');
return ((heading ? heading.innerText : dialog.innerText) || 'unnamed dialog')
  .replace(/\\s+/g, ' ').trim().slice(0, 80);
"""

EXPORT_FILL_MATCH_TOLERANCE_DAYS = 1
ACTIVITY_EXPORT_FIELDS = (
    "transaction_date", "effective_time", "settlement_date", "account_type", "activity_type",
    "activity_sub_type", "symbol", "currency", "quantity", "unit_price",
    "commission", "net_cash_amount",
)
ACTIVITY_EXPORT_SOURCE_FIELDS = ACTIVITY_EXPORT_FIELDS + ("effective_at",)
HOLDINGS_EXPORT_FIELDS = (
    "Account Type", "Symbol", "Exchange", "MIC", "Security Type", "Quantity",
    "Position Direction", "Market Price", "Market Price Currency",
    "Book Value (Market)", "Book Value Currency (Market)",
    "Market Value", "Market Value Currency",
    "Market Unrealized Returns", "Market Unrealized Returns Currency",
)


def read_wealthsimple_export(path: Path) -> tuple[list[dict[str, str]], str | None]:
    """Read a Wealthsimple CSV export with the stdlib reader.

    The file ends with a quoted "As of <timestamp>" footer that is not a data
    row; it is returned separately as the export's as-of stamp rather than
    parsed as a transaction.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    as_of = None
    kept: list[dict[str, str]] = []
    for row in rows:
        first = (next(iter(row.values()), "") or "").strip()
        populated = [value for value in row.values() if (value or "").strip()]
        if first.startswith(EXPORT_AS_OF_PREFIX) and len(populated) == 1:
            as_of = first[len(EXPORT_AS_OF_PREFIX):].strip()
            continue
        if not populated:
            continue
        if not (row.get("transaction_date") or "").strip() and (row.get("effective_date") or "").strip():
            # Current Activity exports use a plain effective_date column.
            row["transaction_date"] = row["effective_date"].strip()
        if not (row.get("transaction_date") or "").strip() and (row.get("effective_at") or "").strip():
            try:
                effective = datetime.fromisoformat(row["effective_at"].strip().replace("Z", "+00:00"))
                if effective.tzinfo is None:
                    effective = effective.replace(tzinfo=timezone.utc)
                row["transaction_date"] = effective.astimezone(timezone.utc).date().isoformat()
            except ValueError:
                # Keep the row and let downstream date validation/reporting
                # expose the malformed source value rather than invent a date.
                row["transaction_date"] = ""
        kept.append(row)
    return kept, as_of


def normalize_export_rows(rows: list[dict[str, str]], fields: tuple[str, ...]) -> list[dict[str, str]]:
    """Keep only the fields the ledger needs.

    Account numbers, account names and free-text descriptions are deliberately
    dropped: the ledger reconciles on account type, symbol and settled amounts,
    and the normalized copy should not carry account identifiers.
    """
    return [{field: (row.get(field) or "").strip() for field in fields} for row in rows]


def export_trade_fills(activity_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Completed buy/sell fills from an activity export, as settled facts."""
    fills: list[dict[str, Any]] = []
    for row in activity_rows:
        if (row.get("activity_type") or "").strip().lower() != "trade":
            continue
        side = (row.get("activity_sub_type") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            continue
        try:
            quantity = abs(float(row.get("quantity") or 0))
            unit_price = float(row.get("unit_price") or 0)
        except ValueError:
            continue
        fills.append({
            "account_type": row.get("account_type"),
            "symbol": normalize_ticker_for_cross_reference(row.get("symbol")),
            "side": side.lower(),
            "quantity": quantity,
            "unit_price": unit_price,
            "currency": row.get("currency"),
            "transaction_date": row.get("transaction_date"),
            "settlement_date": row.get("settlement_date"),
            "net_cash_amount": row.get("net_cash_amount"),
        })
    return fills


def canonical_activity_rows_from_export(
    activity_rows: list[dict[str, str]], *, as_of: str | None = None, year: int | None = None,
) -> list[dict[str, Any]]:
    """Normalize the current-year export into the ledger's activity shape.

    A fresh Wealthsimple Activity CSV is authoritative for settled events.
    Keeping those events in the ordinary activity files means the fast path
    still drives fill/exit reconciliation and gives ChatGPT one usable table,
    without reopening every already-settled Activity disclosure in the browser.
    """
    target_year = year if year is not None else date.today().year
    rows: list[dict[str, Any]] = []
    for index, source in enumerate(activity_rows):
        transaction_date = (source.get("transaction_date") or "").strip()
        if not transaction_date.startswith(f"{target_year}-"):
            continue
        kind = (source.get("activity_type") or "").strip()
        subtype = (source.get("activity_sub_type") or "").strip()
        ticker = normalize_ticker_for_cross_reference(source.get("symbol")) or None
        side = subtype.lower() if kind.lower() == "trade" and subtype.upper() in {"BUY", "SELL"} else None
        quantity = (source.get("quantity") or "").strip().lstrip("-") or None
        unit_price = (source.get("unit_price") or "").strip()
        currency = (source.get("currency") or "").strip()
        net_cash = (source.get("net_cash_amount") or "").strip()
        trade = side is not None
        kind_key = re.sub(r"[^a-z]", "", kind.lower())
        activity_domain = (
            "trade" if trade
            else "currency_conversion" if kind_key in {"fxexchange", "fxconversion", "currencyconversion"}
            else "cash_movement" if kind_key in {"moneymovement", "transfer", "deposit", "withdrawal"}
            else "income_or_adjustment"
        )
        amount = f"${unit_price} {currency}" if unit_price and currency else None
        total = f"${abs(money_to_float(net_cash)):,.2f} {currency}" if net_cash and currency else None
        stable_key = hashlib.sha256(
            "|".join([str(index), transaction_date, source.get("account_type") or "", kind, subtype,
                      ticker or "", quantity or "", unit_price, net_cash]).encode("utf-8")
        ).hexdigest()[:24]
        normalized = {
            "stable_row_key": f"export-{stable_key}",
            "account": (source.get("account_type") or "").strip() or "Unknown",
            "account_type": (source.get("account_type") or "").strip() or "Unknown",
            "ticker": ticker,
            "activity_type": (f"{subtype.title()} trade" if trade else kind) or "Activity recorded",
            "side": side,
            "activity_domain": activity_domain,
            "movement_method": kind if activity_domain == "cash_movement" else None,
            "movement_direction": (
                "in" if activity_domain == "cash_movement" and money_to_float(net_cash) > 0
                else "out" if activity_domain == "cash_movement" and money_to_float(net_cash) < 0
                else None
            ),
            "side_label": (f"{subtype.title()} trade" if trade else kind) or "Activity recorded",
            "status": "Completed" if trade else "Activity recorded",
            "quantity": quantity,
            "execution_price": amount,
            "total_value": total,
            "date": transaction_date,
            "filled_date": transaction_date if trade else None,
            "time": None,
            "detail_status": "export_confirmed",
            "confirmation_level": "export_confirmed",
            "export_evidence_reference": "raw-exports/activity_export",
            "export_as_of": as_of,
            "currency": currency or None,
            "net_cash_amount": net_cash or None,
            "uncertainty_notes": [] if trade else ["settled non-trade activity from canonical export"],
        }
        if activity_domain == "currency_conversion":
            signed_amount = money_to_float(net_cash)
            normalized.update({
                "conversion_pair_key": row_hash(
                    "fx-pair",
                    source.get("account_type") or "",
                    source.get("transaction_date") or "",
                    source.get("effective_time") or "",
                    source.get("cad_per_usd_rate") or "",
                ),
                "conversion_leg_currency": currency or None,
                "conversion_leg_amount": net_cash or None,
                "conversion_leg_direction": (
                    "received" if signed_amount > 0
                    else "sold" if signed_amount < 0
                    else "zero"
                ),
                "cad_per_usd_rate": source.get("cad_per_usd_rate") or None,
            })
        rows.append(normalized)
    return rows


def has_fresh_canonical_activity_export(exports: dict[str, Any]) -> bool:
    """Return true only when the supplied Activity export can replace browser history.

    Pending orders always remain a live browser concern. Freshness must come
    from a broker as-of stamp or an explicitly validated hash-bound downloader
    receipt. No rows, unknown freshness or age above the 24-hour threshold
    cannot safely replace the browser's settled-activity pass.
    """
    provenance = exports.get("activity_export") or {}
    rows = exports.get("activity_export_rows") or []
    return bool(rows) and provenance.get("age_hours_at_capture") is not None and not provenance.get("is_stale_at_capture")


def merge_fresh_export_with_browser_terminal_activity(
    export_rows: list[dict[str, str]], browser_rows: list[dict[str, Any]], *, as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Keep CSV-settled facts once and retain only terminal browser-only statuses.

    Wealthsimple's Activity export currently omits cancelled, expired, and
    rejected order history. The browser scan therefore remains necessary, but
    its filled cards must not be counted beside the canonical CSV fills.
    """
    canonical = canonical_activity_rows_from_export(export_rows, as_of=as_of)
    browser_only = [
        row for row in browser_rows
        if row.get("status") in {"Cancelled", "Expired", "Rejected", "Failed"}
    ]
    return canonical + browser_only


def reconcile_activity_sources(
    browser_rows: list[dict[str, Any]],
    export_rows: list[dict[str, str]],
    *,
    year: int | None = None,
    today: date | None = None,
    export_as_of: str | None = None,
) -> dict[str, Any]:
    """Compare browser-visible fill cards with current-year exported trades.

    Collapsed browser cards do not expose quantity or execution price, so the
    common identity is account, ticker, side, settlement total, and currency.
    Matching is a multiset operation: duplicate cards or duplicate fills remain
    visible as source-only rows instead of being collapsed.
    """
    observed_today = today or date.today()
    target_year = year if year is not None else observed_today.year
    excluded_statuses = {"Pending", "Cancelled", "Expired", "Rejected", "Failed"}
    browser_candidates = []
    for row in browser_rows:
        if row.get("side") not in {"buy", "sell"}:
            continue
        if row.get("status") in excluded_statuses:
            continue
        browser_date = _browser_fill_date(row, observed_today)
        if browser_date is not None and browser_date.year != target_year:
            continue
        browser_candidates.append(row)
    export_candidates = [
        row for row in export_trade_fills(export_rows)
        if (row.get("transaction_date") or "").startswith(f"{target_year}-")
    ]

    def browser_key(row: dict[str, Any]) -> tuple[Any, ...] | None:
        account = row.get("account")
        ticker = normalize_ticker_for_cross_reference(row.get("ticker"))
        side = row.get("side")
        # A detailed browser row can label `currency` with the execution
        # currency while `total_value` is the CAD-settled amount. The export's
        # net cash currency corresponds to the total, so prefer its trailing
        # currency token.
        currency = value_currency(row.get("total_value")) or row.get("currency")
        total = money_to_float(row.get("total_value"))
        if not account or not ticker or not side or not currency or not row.get("total_value"):
            return None
        return account, ticker, side, round(abs(total), 2), currency

    def export_key(row: dict[str, Any]) -> tuple[Any, ...] | None:
        account = row.get("account_type")
        ticker = normalize_ticker_for_cross_reference(row.get("symbol"))
        side = row.get("side")
        currency = row.get("currency")
        cash = row.get("net_cash_amount")
        if not account or not ticker or not side or not currency or cash in {None, ""}:
            return None
        return account, ticker, side, round(abs(money_to_float(str(cash))), 2), currency

    browser_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    export_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    browser_uncomparable: list[dict[str, Any]] = []
    export_uncomparable: list[dict[str, Any]] = []
    for row in browser_candidates:
        key = browser_key(row)
        if key is None:
            browser_uncomparable.append(row)
        else:
            browser_groups.setdefault(key, []).append(row)
    for row in export_candidates:
        key = export_key(row)
        if key is None:
            export_uncomparable.append(row)
        else:
            export_groups.setdefault(key, []).append(row)

    matched: list[dict[str, Any]] = []
    browser_only: list[dict[str, Any]] = []
    export_only: list[dict[str, Any]] = []
    for key in sorted(set(browser_groups) | set(export_groups)):
        browser_group = browser_groups.get(key, [])
        export_group = export_groups.get(key, [])
        overlap = min(len(browser_group), len(export_group))
        account, ticker, side, total, currency = key
        if overlap:
            matched.append({
                "account": account,
                "ticker": ticker,
                "side": side,
                "settlement_total": total,
                "currency": currency,
                "matched_occurrences": overlap,
                "browser_occurrences": len(browser_group),
                "export_occurrences": len(export_group),
                "browser_dates": sorted({
                    str(row.get("date")) for row in browser_group[:overlap]
                    if row.get("date")
                }),
                "export_dates": sorted({
                    str(row.get("transaction_date")) for row in export_group[:overlap]
                    if row.get("transaction_date")
                }),
            })
        for row in browser_group[overlap:]:
            browser_date = _browser_fill_date(row, observed_today)
            export_stamp = export_as_of_datetime(export_as_of)
            if browser_date is not None and export_stamp is not None:
                # Wealthsimple documents T+1 settlement for stocks and ETFs.
                # A same-day or next-business-day browser fill can therefore
                # legitimately be absent from a freshly generated export.
                business_days = 0
                cursor = browser_date
                while cursor < export_stamp.date():
                    cursor += timedelta(days=1)
                    if cursor.weekday() < 5:
                        business_days += 1
                recent = 0 <= business_days <= 1
            else:
                recent = (
                    browser_date is not None
                    and 0 <= (observed_today - browser_date).days <= 3
                )
            browser_only.append({
                "account": row.get("account"),
                "ticker": normalize_ticker_for_cross_reference(row.get("ticker")),
                "side": row.get("side"),
                "settlement_total": row.get("total_value"),
                "currency": (
                    value_currency(row.get("total_value"))
                    or row.get("currency")
                ),
                "browser_date": row.get("date"),
                "browser_status": row.get("status"),
                "source_control_id": row.get("source_control_id"),
                "review_class": (
                    "recent_may_be_unsettled"
                    if recent else "missing_from_settled_export_review"
                ),
            })
        export_only.extend({
            "account": row.get("account_type"),
            "ticker": row.get("symbol"),
            "side": row.get("side"),
            "quantity": row.get("quantity"),
            "unit_price": row.get("unit_price"),
            "settlement_total": row.get("net_cash_amount"),
            "currency": row.get("currency"),
            "transaction_date": row.get("transaction_date"),
        } for row in export_group[overlap:])

    browser_only_recent = [
        row for row in browser_only
        if row["review_class"] == "recent_may_be_unsettled"
    ]
    browser_only_review = [
        row for row in browser_only
        if row["review_class"] == "missing_from_settled_export_review"
    ]
    return {
        "status": (
            "differences_found"
            if browser_only_review or browser_uncomparable or export_uncomparable
            else (
                "differences_expected_recent_settlement"
                if browser_only_recent
                else "browser_visible_fills_corroborated"
            )
        ),
        "target_year": target_year,
        "matching_basis": [
            "account", "normalized ticker", "side",
            "absolute settlement total rounded to cents", "settlement currency",
        ],
        "browser_candidate_count": len(browser_candidates),
        "export_trade_count": len(export_candidates),
        "matched_browser_occurrences": sum(
            row["matched_occurrences"] for row in matched
        ),
        "matched_groups": matched,
        "browser_only": browser_only,
        "browser_only_review_required": browser_only_review,
        "browser_only_recent_unsettled": browser_only_recent,
        "export_only": export_only,
        "browser_uncomparable": browser_uncomparable,
        "export_uncomparable": export_uncomparable,
        "interpretation": (
            "Browser-only rows within the documented T+1 stock/ETF settlement window are separated as "
            "possibly unsettled. Older or undated browser-only rows are not corroborated by the export "
            "and require review. Market holidays can extend settlement and should be considered when "
            "reviewing a boundary case. "
            "Export-only rows are retained as canonical settled history; the browser feed can compress "
            "or omit older and managed-account events. Amount-based multiset agreement is source-level "
            "corroboration, not proof that each repeated browser card maps to a unique exported fill."
        ),
    }


def reconcile_holdings_sources(
    browser_holdings: list[dict[str, Any]],
    export_rows: list[dict[str, str]],
) -> dict[str, Any]:
    """Compare browser and export positions by account, ticker, and quantity."""
    if not export_rows:
        return {
            "status": "not_available",
            "browser_holding_count": len(browser_holdings),
            "export_holding_count": 0,
            "matched": [],
            "differences": [],
            "export_cash_positions": [],
        }

    browser_map = {
        (
            row.get("account"),
            normalize_ticker_for_cross_reference(row.get("ticker")),
        ): row
        for row in browser_holdings
        if row.get("account") and row.get("ticker")
    }
    currency_export_rows = [
        row for row in export_rows
        if (row.get("Security Type") or "").strip().upper() == "CURRENCY"
    ]
    export_cash_positions = [
        {
            "account": row.get("Account Type"),
            "currency": (row.get("Symbol") or "").strip() or None,
            "quantity": quantity_as_float(row.get("Quantity")),
            "market_value": quantity_as_float(row.get("Market Value")),
            "market_value_currency": row.get("Market Value Currency"),
        }
        for row in currency_export_rows
    ]
    security_export_rows = [
        row for row in export_rows
        if (row.get("Security Type") or "").strip().upper() != "CURRENCY"
    ]
    in_scope_export_rows = [
        row for row in security_export_rows if row.get("Account Type") in ACCOUNTS
    ]
    out_of_scope_export_rows = [
        {
            "account": row.get("Account Type"),
            "ticker": normalize_ticker_for_cross_reference(row.get("Symbol")),
            "quantity": quantity_as_float(row.get("Quantity")),
        }
        for row in security_export_rows
        if row.get("Account Type") not in ACCOUNTS
    ]
    export_map = {
        (
            row.get("Account Type"),
            normalize_ticker_for_cross_reference(row.get("Symbol")),
        ): row
        for row in in_scope_export_rows
        if row.get("Account Type") and row.get("Symbol")
    }
    matched: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for key in sorted(set(browser_map) | set(export_map)):
        browser = browser_map.get(key)
        exported = export_map.get(key)
        account, ticker = key
        if browser is None:
            differences.append({
                "type": "export_holding_missing_from_browser",
                "account": account,
                "ticker": ticker,
                "export_quantity": quantity_as_float(
                    exported.get("Quantity") if exported else None
                ),
            })
            continue
        if exported is None:
            differences.append({
                "type": "browser_holding_missing_from_export",
                "account": account,
                "ticker": ticker,
                "browser_quantity": quantity_as_float(browser.get("quantity")),
            })
            continue
        browser_quantity = quantity_as_float(browser.get("quantity"))
        export_quantity = quantity_as_float(exported.get("Quantity"))
        if (
            browser_quantity is None
            or export_quantity is None
            or abs(browser_quantity - export_quantity) > 1e-6
        ):
            differences.append({
                "type": "holding_quantity_mismatch",
                "account": account,
                "ticker": ticker,
                "browser_quantity": browser_quantity,
                "export_quantity": export_quantity,
            })
            continue
        matched.append({
            "account": account,
            "ticker": ticker,
            "quantity": browser_quantity,
            "browser_market_value": browser.get("market_value"),
            "export_market_value": exported.get("Market Value"),
            "market_value_currency": exported.get("Market Value Currency"),
        })
    return {
        "status": "differences_found" if differences else "quantities_match",
        "browser_holding_count": len(browser_map),
        "export_holding_count": len(export_map),
        "matched": matched,
        "differences": differences,
        "export_cash_positions": export_cash_positions,
        "out_of_scope_export_holdings": out_of_scope_export_rows,
        "interpretation": (
            "Quantity is the hard control. Market prices and values are shown for context only "
            "because browser and export timestamps can differ. Export rows whose Security Type is "
            "CURRENCY are reported as cash positions and are not counted as security holdings. "
            "Holdings from accounts outside TFSA, RRSP, and Non-registered are reported separately "
            "and do not fail this audit."
        ),
    }


def fresh_export_security_holding_counts(
    exports: dict[str, Any] | None,
) -> dict[str, int]:
    """Count security positions only when the Holdings export is current.

    A fresh all-accounts export is an independent control for the otherwise
    ambiguous cash-only account layout. Currency rows are excluded because
    the browser holdings grid contains securities, not native cash balances.
    """
    exports = exports or {}
    provenance = exports.get("holdings_export") or {}
    rows = exports.get("holdings_export_rows") or []
    if (
        not rows
        or provenance.get("age_hours_at_capture") is None
        or provenance.get("is_stale_at_capture")
    ):
        return {}
    counts = {account: 0 for account in ACCOUNTS}
    for row in rows:
        account = row.get("Account Type")
        if account not in counts:
            continue
        if (row.get("Security Type") or "").strip().upper() == "CURRENCY":
            continue
        if (row.get("Symbol") or "").strip():
            counts[account] += 1
    return counts


def render_source_reconciliation_md(
    activity: dict[str, Any],
    holdings: dict[str, Any],
) -> str:
    lines = [
        "# Browser and CSV Source Reconciliation",
        "",
        "## Activity",
        "",
        f"- Status: `{activity.get('status')}`",
        f"- Browser fill-like cards: {activity.get('browser_candidate_count', 0)}",
        f"- Exported current-year trades: {activity.get('export_trade_count', 0)}",
        f"- Matched browser occurrences: {activity.get('matched_browser_occurrences', 0)}",
        f"- Browser-only: {len(activity.get('browser_only') or [])}",
        f"- Browser-only recent/possibly unsettled: {len(activity.get('browser_only_recent_unsettled') or [])}",
        f"- Browser-only requiring review: {len(activity.get('browser_only_review_required') or [])}",
        f"- Export-only: {len(activity.get('export_only') or [])}",
        "",
        activity.get("interpretation", ""),
        "",
        "## Holdings",
        "",
        f"- Status: `{holdings.get('status')}`",
        f"- Browser holdings: {holdings.get('browser_holding_count', 0)}",
        f"- Export holdings: {holdings.get('export_holding_count', 0)}",
        f"- Quantity differences: {len(holdings.get('differences') or [])}",
        "",
        holdings.get("interpretation", ""),
        "",
        "See `source-reconciliation.json` for row-level differences.",
    ]
    return "\n".join(lines) + "\n"


def analyze_activity_export(
    activity_rows: list[dict[str, str]], *, year: int | None = None, as_of: str | None = None
) -> dict[str, Any]:
    """Build a privacy-minimized, ChatGPT-ready analysis of settled CSV activity.

    Wealthsimple's export is the canonical record for settled historical
    activity. Browser cards remain necessary for *live* pending orders and
    detail evidence, but this analysis owns completed fills, income, FX, and
    cash movements in the handoff so duplicate browser controls cannot inflate
    their counts.
    """
    target_year = year if year is not None else date.today().year
    current_year = [
        row for row in activity_rows
        if (row.get("transaction_date") or "").startswith(f"{target_year}-")
    ]
    fills = [
        {**row, "quantity": (row.get("quantity") or "").lstrip("-")}
        for row in current_year
        if (row.get("activity_type") or "").lower() == "trade"
        and (row.get("activity_sub_type") or "").upper() in {"BUY", "SELL"}
    ]
    grouped: dict[str, int] = {}
    by_account: dict[str, int] = {}
    for row in current_year:
        kind = (row.get("activity_type") or "Unknown").strip() or "Unknown"
        account = (row.get("account_type") or "Unknown").strip() or "Unknown"
        grouped[kind] = grouped.get(kind, 0) + 1
        by_account[account] = by_account.get(account, 0) + 1
    special_kinds = {
        "Dividend", "Interest", "FxExchange", "MoneyMovement", "Fee", "Tax",
        "CorporateAction", "ReturnOfCapital", "NonCashDistribution", "Correction",
        "AdministrativePayment",
    }
    ancillary = [row for row in current_year if (row.get("activity_type") or "") in special_kinds]
    dates = sorted(d for d in ((row.get("transaction_date") or "").strip() for row in activity_rows) if d)
    all_years: dict[str, int] = {}
    for row in activity_rows:
        kind = (row.get("activity_type") or "Unknown").strip() or "Unknown"
        all_years[kind] = all_years.get(kind, 0) + 1
    outside = len(activity_rows) - len(current_year)
    return {
        "source": "user-supplied Wealthsimple Activity CSV export",
        "canonical_for": "settled activity only; not open orders or live balances",
        "export_as_of": as_of or "not stated in the export",
        "export_coverage": {"earliest_transaction_date": dates[0] if dates else None, "latest_transaction_date": dates[-1] if dates else None},
        "target_year": target_year,
        "rows_in_export": len(activity_rows),
        "current_year_rows": len(current_year),
        "rows_outside_target_year": outside,
        "rows_outside_target_year_are_excluded_from_every_table_below": outside > 0,
        "all_rows_by_type_including_other_years": dict(sorted(all_years.items())),
        "current_year_rows_by_account": dict(sorted(by_account.items())),
        "current_year_rows_by_type": dict(sorted(grouped.items())),
        "current_year_trade_fills": fills,
        "current_year_ancillary_activity": ancillary,
    }


def render_activity_export_analysis_md(analysis: dict[str, Any]) -> str:
    """Standalone readable report kept beside the raw export in every bundle."""
    lines = [
        "# Canonical Activity Export Analysis",
        "",
        f"Source: {analysis['source']}",
        f"Use: {analysis['canonical_for']}",
        f"Export as of: {analysis.get('export_as_of', 'not stated in the export')}",
        f"Export covers: {(analysis.get('export_coverage') or {}).get('earliest_transaction_date') or '?'} to {(analysis.get('export_coverage') or {}).get('latest_transaction_date') or '?'}",
        f"Rows: {analysis['rows_in_export']} total; {analysis['current_year_rows']} in {analysis['target_year']}.",
        "",
        *([f"> **{analysis['rows_outside_target_year']} of {analysis['rows_in_export']} exported rows fall outside {analysis['target_year']} and are excluded from every table below.**", ""] if analysis.get("rows_outside_target_year") else []),
        "## Current-Year Activity Counts",
        "",
        "| Type | Rows |",
        "|---|---:|",
    ]
    lines.extend(f"| {kind} | {count} |" for kind, count in analysis["current_year_rows_by_type"].items())
    lines += ["", "## Settled Trade Fills", "", "| Date | Account | Symbol | Side | Quantity | Unit price | Currency | Net cash |", "|---|---|---|---|---:|---:|---|---:|"]
    for row in analysis["current_year_trade_fills"]:
        lines.append(
            "| {date} | {account} | {symbol} | {side} | {quantity} | {price} | {currency} | {cash} |".format(
                date=row.get("transaction_date") or "?", account=row.get("account_type") or "?",
                symbol=row.get("symbol") or "?", side=row.get("activity_sub_type") or "?",
                quantity=row.get("quantity") or "?", price=row.get("unit_price") or "?",
                currency=row.get("currency") or "?", cash=row.get("net_cash_amount") or "?",
            )
        )
    if not analysis["current_year_trade_fills"]:
        lines.append("| None | - | - | - | - | - | - | - |")
    lines += ["", "## Other Settled Activity", ""]
    for row in analysis["current_year_ancillary_activity"]:
        lines.append(
            f"- {row.get('transaction_date') or '?'} {row.get('account_type') or '?'} "
            f"{row.get('activity_type') or '?'} {row.get('symbol') or ''} "
            f"{row.get('net_cash_amount') or ''} {row.get('currency') or ''}".rstrip()
        )
    if not analysis["current_year_ancillary_activity"]:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _browser_fill_date(
    row: dict[str, Any], today: date | None = None
) -> date | None:
    value = (row.get("filled_date") or row.get("date") or "").strip()
    observed_today = today or date.today()
    if value == "Today":
        return observed_today
    if value == "Yesterday":
        return observed_today - timedelta(days=1)
    try:
        return datetime.strptime(value, "%B %d, %Y").date()
    except ValueError:
        return None


def _export_fill_date(fill: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat((fill.get("transaction_date") or "").strip())
    except ValueError:
        return None


def correlate_duplicate_fills_with_export(
    duplicates: list[dict[str, Any]], activity: list[dict[str, Any]], fills: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Decide which duplicate-looking browser cards the export does not support.

    The export is canonical for completed fills. When a duplicate group holds
    more browser rows than the export has matching fills, the surplus rows are
    labelled ``export_correlated_browser_duplicate`` and excluded from aggregate
    fill counts. Browser rows are annotated, never deleted, and an export fill
    is never dropped. A group the export corroborates in full stays counted.
    """
    by_key = {row.get("stable_row_key"): row for row in activity}
    resolved: list[dict[str, Any]] = []
    for group in duplicates:
        quantity = quantity_as_float(group.get("quantity"))
        price = money_to_float(group.get("execution_price"))
        side = "buy" if "buy" in (group.get("activity_type") or "").lower() else "sell"
        keys = list(group.get("stable_row_keys") or [])
        anchor = next((by_key[key] for key in keys if key in by_key), None)
        anchor_date = _browser_fill_date(anchor) if anchor else None
        matches = [
            fill for fill in fills
            if fill["account_type"] == group.get("account")
            and fill["symbol"] == normalize_ticker_for_cross_reference(group.get("ticker"))
            and fill["side"] == side
            and quantity is not None and abs(fill["quantity"] - quantity) < 1e-6
            and abs(fill["unit_price"] - price) < 0.005
            and (
                anchor_date is None or _export_fill_date(fill) is None
                or abs((_export_fill_date(fill) - anchor_date).days) <= EXPORT_FILL_MATCH_TOLERANCE_DAYS
            )
        ]
        surplus = max(0, len(keys) - len(matches))
        outcome = dict(group)
        outcome["export_matching_fills"] = len(matches)
        outcome["export_transaction_dates"] = sorted(
            {fill["transaction_date"] for fill in matches if fill.get("transaction_date")}
        )
        outcome["browser_rows_not_supported_by_export"] = surplus
        if not matches:
            outcome["resolution"] = "no_matching_export_fill_review_manually"
        elif surplus:
            outcome["resolution"] = "export_correlated_browser_duplicate"
            for key in keys[len(matches):]:
                row = by_key.get(key)
                if row is None:
                    continue
                row["fill_count_status"] = "export_correlated_browser_duplicate"
                row.setdefault("uncertainty_notes", []).append(
                    "browser rendered this settled fill more than once; the export supports fewer fills, "
                    "so this row is retained as evidence but excluded from aggregate fill counts"
                )
        else:
            outcome["resolution"] = "export_confirms_every_browser_row"
        resolved.append(outcome)
    return resolved


ACCOUNT_ID_QUERY_PATTERN = re.compile(r"(account_ids?=)[^&#\s\"]+")
EXPORT_STALE_AFTER_HOURS = 24
EXPORT_AS_OF_PATTERN = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})(?::\d{2})?\s*(?:GMT|UTC)?(?P<offset>[+-]\d{2}:?\d{2})?$")


def redact_account_ids_in_url(url: str | None) -> str | None:
    if not url:
        return url
    return ACCOUNT_ID_QUERY_PATTERN.sub(r"\1<redacted>", url)


def export_as_of_datetime(as_of: str | None) -> datetime | None:
    match = EXPORT_AS_OF_PATTERN.match((as_of or "").strip())
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group("stamp").replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    offset = match.group("offset")
    if not offset:
        return stamp.astimezone()
    offset = offset.replace(":", "")
    delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
    return stamp.replace(tzinfo=timezone(-delta if offset[0] == "-" else delta))


def export_age_hours(as_of: str | None, now: datetime | None = None) -> float | None:
    stamp = export_as_of_datetime(as_of)
    return None if stamp is None else ((now or datetime.now().astimezone()) - stamp).total_seconds() / 3600.0


def redact_export_csv(source: Path, destination: Path, fields: tuple[str, ...]) -> list[str]:
    """Copy only allowlisted ledger fields into a bundle-safe export CSV."""
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        destination.write_text("", encoding="utf-8")
        return []
    header = rows[0]
    keep = [index for index, name in enumerate(header) if name.strip() in fields]
    dropped = [name.strip() for index, name in enumerate(header) if index not in keep]
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow([row[index] if index < len(row) else "" for index in keep])
    return dropped


def import_exports(
    state: RunState, activity_export: Path | None, holdings_export: Path | None,
    activity_receipt: Path | None = None,
) -> dict[str, Any]:
    """Copy explicitly supplied exports into the bundle and record provenance.

    Nothing is discovered implicitly and nothing is fetched; only paths passed
    on the command line are read.
    """
    provenance: dict[str, Any] = {}
    raw_dir = state.out_dir / "raw-exports"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for label, path, fields in (
        ("activity_export", activity_export, ACTIVITY_EXPORT_SOURCE_FIELDS),
        ("holdings_export", holdings_export, HOLDINGS_EXPORT_FIELDS),
    ):
        if path is None:
            continue
        source = Path(path)
        if not source.is_file():
            state.warn(f"{label} not found and was ignored: {source}")
            continue
        try:
            rows, as_of = read_wealthsimple_export(source)
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            # A file chosen through the GUI's "All files" filter may not be
            # UTF-8 text at all. UnicodeDecodeError is a ValueError, not an
            # OSError, so it previously escaped and aborted the whole capture.
            state.warn(f"{label} could not be parsed and was ignored: {type(exc).__name__}")
            continue
        copied = raw_dir / source.name
        dropped = redact_export_csv(source, copied, fields)
        provenance[label] = {
            "source_basename": source.name,
            "bundle_copy": str(copied),
            "bundle_copy_is_redacted": True,
            "bundle_copy_dropped_columns": dropped,
            "bundle_copy_sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "as_of": as_of,
            "row_count": len(rows),
            "imported_at": iso_now(),
            "provenance": "user-supplied Wealthsimple CSV export; canonical for completed activity and holdings",
        }
        age = export_age_hours(as_of)
        provenance[label]["freshness_basis"] = "broker_as_of" if age is not None else "unknown"
        if label == "activity_export" and activity_receipt is not None:
            try:
                proof = validate_activity_receipt(activity_receipt, source, provenance[label]["sha256"],
                                                  max_age_hours=EXPORT_STALE_AFTER_HOURS)
                provenance[label]["download_receipt"] = proof
                # Never override an explicitly old broker timestamp with a new download.
                if age is None and not as_of:
                    age = proof["age_hours"]
                    provenance[label]["freshness_basis"] = "hash_bound_download_receipt"
            except ValueError:
                state.warn("Activity download receipt was rejected; it cannot establish export freshness")
        provenance[label]["age_hours_at_capture"] = None if age is None else round(age, 2)
        provenance[label]["is_stale_at_capture"] = bool(age is not None and age > EXPORT_STALE_AFTER_HOURS)
        if age is None and as_of:
            state.warn(f"{label} as-of stamp could not be parsed and its freshness is unknown: {as_of!r}")
        elif age is None:
            state.warn(f"{label} carries no as-of stamp; its freshness cannot be checked")
        elif age > EXPORT_STALE_AFTER_HOURS:
            state.warn(f"{label} is {age / 24:.1f} days old at capture time (as of {as_of}); settled activity since then is missing from the canonical export")
        normalized_rows = normalize_export_rows(rows, fields)
        if label == "activity_export":
            # Preserve the structured FX rate without retaining the export's
            # free-text description or account identifier in the bundle.
            for normalized, source_row in zip(normalized_rows, rows):
                if (source_row.get("activity_type") or "").strip().lower() != "fxexchange":
                    continue
                rate = re.search(
                    r"\$1USD\s*=\s*\$([0-9]+(?:\.[0-9]+)?)CAD",
                    source_row.get("description") or "",
                    re.IGNORECASE,
                )
                normalized["cad_per_usd_rate"] = rate.group(1) if rate else ""
        provenance[f"{label}_rows"] = normalized_rows
    return provenance


def duplicate_activity_events(activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag one settled event that Activity rendered as two rows.

    Wealthsimple can paint a single completed fill twice under two different
    disclosure IDs. Control-ID identity keeps genuine same-notional ladder legs
    apart, so it cannot collapse these; matching on the settled facts can.
    Two fills of the same security, side, quantity, price and fill timestamp in
    one account are reported for review rather than merged, because a real
    repeated fill is possible.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in activity:
        if row.get("status") not in {"Completed", "Filled", "Completed/Filled"}:
            continue
        if not row.get("ticker"):
            continue
        key = (
            row.get("account"), normalize_ticker_for_cross_reference(row.get("ticker")),
            row.get("activity_type"), row.get("quantity"), row.get("execution_price"),
            row.get("filled_date") or row.get("date"), row.get("time"),
        )
        groups.setdefault(key, []).append(row)
    findings: list[dict[str, Any]] = []
    for key, rows in groups.items():
        distinct = {row.get("stable_row_key") for row in rows}
        if len(rows) < 2 or len(distinct) < 2:
            continue
        findings.append({
            "type": "activity_event_reported_more_than_once",
            "account": key[0], "ticker": key[1], "activity_type": key[2],
            "quantity": key[3], "execution_price": key[4], "filled_date": key[5], "time": key[6],
            "row_count": len(rows),
            "stable_row_keys": sorted(k for k in distinct if k),
            "source_control_ids": sorted({row.get("source_control_id") for row in rows if row.get("source_control_id")}),
            "note": "identical settled fields under different control IDs; confirm against holdings before counting both",
        })
    return findings


def duplicate_checks(holdings: list[dict[str, Any]], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen_h: dict[str, set[str]] = {}
    for h in holdings:
        seen_h.setdefault(normalize_ticker_for_cross_reference(h.get("ticker")), set()).add(h.get("account"))
    for ticker, accounts in seen_h.items():
        if ticker and len(accounts) > 1:
            checks.append({"type": "ticker_held_across_accounts", "ticker": ticker, "accounts": sorted(accounts)})
    seen_orders: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for o in orders:
        key = (o.get("ticker"), o.get("side"), o.get("quantity"), o.get("limit_price"))
        seen_orders.setdefault(key, []).append(o)
    for key, group in seen_orders.items():
        accounts = sorted({g.get("account") for g in group})
        if len(group) > 1 and len(accounts) > 1:
            checks.append({"type": "same_ticker_side_quantity_limit_across_accounts", "key": key, "accounts": accounts})
    return checks


def paired_exit_checks(holdings: list[dict[str, Any]], orders: list[dict[str, Any]], activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sell_keys = {
        (o.get("account"), normalize_ticker_for_cross_reference(o.get("ticker")))
        for o in orders if o.get("side") == "sell" and order_state(o.get("status")) != "terminal"
    }
    checks = []
    for h in holdings:
        normalized = normalize_ticker_for_cross_reference(h.get("ticker"))
        if normalized in SPECIAL_TICKERS and (h.get("account"), normalized) not in sell_keys:
            checks.append({"type": "holding_without_open_sell_exit", "account": h.get("account"), "ticker": h.get("ticker")})
    for ticker in ["QCOM", "CVS", "UPS", "TD"]:
        matching_activity = [
            a for a in activity if normalize_ticker_for_cross_reference(a.get("ticker")) == ticker
        ]
        # "Not seen in activity" reads as "not in play", which is wrong while a
        # live open order exists for the same ticker - that is precisely the
        # case where a fill could arrive next.
        matching_orders = [
            o for o in orders if normalize_ticker_for_cross_reference(o.get("ticker")) == ticker
        ]
        if matching_activity:
            status = "activity_seen"
        elif matching_orders:
            status = "no_activity_but_open_order_pending"
        else:
            status = "not_seen_in_captured_activity"
        checks.append({
            "type": "special_attention_status",
            "ticker": ticker,
            "status": status,
            "open_orders": [
                {"account": o.get("account"), "ticker": o.get("ticker"), "side": o.get("side"),
                 "quantity": o.get("quantity"), "estimated_total": o.get("estimated_total")}
                for o in matching_orders
            ],
            "note": "If filled, paired exits may need placement later. No action taken.",
        })
    return checks


def activity_status_counts(activity: list[dict[str, Any]]) -> dict[str, int]:
    return count_by(
        [{"activity_bucket": activity_bucket(row.get("status"))} for row in activity],
        "activity_bucket",
    )


def activity_by_status(activity: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in activity:
        bucket = activity_bucket(row.get("status"))
        row["activity_bucket"] = bucket
        groups.setdefault(bucket, []).append(row)
    return groups


def activity_bucket(status: str | None) -> str:
    """Normalize Activity into non-overlapping audit buckets.

    Pending is intentionally excluded before this point. Deposits/dividends and
    in-flight movements are kept visible without pretending they are fills.
    """
    normalized = (status or "").strip().lower()
    if normalized in {"completed", "filled", "completed/filled"}:
        return "Completed"
    if normalized in {"cancelled", "canceled"}:
        return "Cancelled"
    if normalized == "expired":
        return "Expired"
    if normalized in {"rejected", "failed"}:
        return "Rejected"
    if normalized in {"in progress", "upcoming payment"}:
        return "In flight"
    if normalized == "status unconfirmed":
        return "Status unconfirmed"
    return "Income / transfer / other"


def annotate_activity_identity_confidence(activity: list[dict[str, Any]]) -> int:
    """Mark rows whose collapsed card cannot prove an order/activity identity."""
    ambiguous = 0
    for row in activity:
        if row.get("detail_status") == "detail_confirmed":
            row["identity_confidence"] = "detail_confirmed"
            continue
        if row.get("status") in {"Cancelled", "Expired", "Rejected", "Failed"}:
            if terminal_activity_row_is_complete(row) and not row.get("terminal_identity_collision"):
                row["identity_confidence"] = (
                    "cash_movement_row_confirmed"
                    if row.get("activity_domain") == "cash_movement"
                    else "terminal_trade_row_confirmed"
                )
            else:
                row["identity_confidence"] = "collapsed_ambiguous"
                row.setdefault("uncertainty_notes", []).append("terminal activity row is incomplete or collides with another collapsed identity and was not detail-confirmed")
                ambiguous += 1
        else:
            row["identity_confidence"] = "row_confirmed"
    return ambiguous


def sell_order_coverage(holdings: list[dict[str, Any]], orders: list[dict[str, Any]], *, inventory_complete: bool = False) -> list[dict[str, Any]]:
    """Compare a supplied inventory. Live bundle completeness is gated separately."""
    return sell_coverage(holdings, orders, normalize_ticker_for_cross_reference, inventory_complete=inventory_complete)


def filled_buy_exit_checks(activity: list[dict[str, Any]], orders: list[dict[str, Any]], holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sell_keys = {
        (order.get("account"), normalize_ticker_for_cross_reference(order.get("ticker")))
        for order in orders if order.get("side") == "sell" and order_state(order.get("status")) != "terminal"
    }
    currently_held = {
        (holding.get("account"), normalize_ticker_for_cross_reference(holding.get("ticker")))
        for holding in holdings
    }
    checks: list[dict[str, Any]] = []
    for row in activity:
        if row.get("side") != "buy" or row.get("status") not in {"Completed", "Filled", "Completed/Filled"}:
            continue
        key = (row.get("account"), normalize_ticker_for_cross_reference(row.get("ticker")))
        # A completed buy with no remaining holding is not missing an exit: it
        # may have been fully sold in the same capture window (FM was the live
        # regression case). Only current positions without an open sell are
        # operationally actionable as a missing exit.
        if not key[1] or key in sell_keys or key not in currently_held:
            continue
        checks.append({
            "type": "filled_buy_without_open_sell_exit",
            "account": key[0],
            "ticker": key[1],
            "quantity": row.get("quantity"),
            "execution_price": row.get("execution_price"),
            "filled_date": row.get("filled_date") or row.get("date"),
            "detail_status": row.get("detail_status"),
        })
    return checks


def quantity_as_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"[0-9][0-9,.]*", value)
    return float(match.group(0).replace(",", "")) if match else None


def summarize_accounts_markdown(account: str, accounts: list[dict[str, Any]], holdings: list[dict[str, Any]], orders: list[dict[str, Any]], activity: list[dict[str, Any]], reserve: dict[str, Any], duplicates: list[dict[str, Any]], paired: list[dict[str, Any]]) -> str:
    acct = next((a for a in accounts if a["account"] == account), {})
    lines = [
        f"# {account} Summary", "",
        f"- Total value: `{display_account_value(acct)}`",
        f"- Total cash available (CAD aggregate of native CAD + USD): `{display_value(acct.get('available_to_trade'))}`",
        f"- Native available CAD: `{display_value(acct.get('available_cash_cad'))}`",
        f"- Native available USD: `{display_value(acct.get('available_cash_usd'))}`",
        "",
    ]
    lines += ["## Holdings", "", "| Ticker | Security | Quantity | Market value |", "|---|---|---:|---:|"]
    for h in [x for x in holdings if x.get("account") == account]:
        lines.append(f"| {h.get('ticker')} | {h.get('security_name')} | {h.get('quantity')} | {h.get('market_value')} |")
    lines += ["", "## Open Orders", "", "| Priority | Ticker | Side | Qty | Limit | Submitted | Expiry | Total | Confirm |", "|---|---|---|---:|---:|---|---|---:|---|"]
    for o in [x for x in orders if x.get("account") == account]:
        submitted = " ".join(part for part in [o.get("submitted_date"), o.get("submitted_time")] if part) or "Not shown"
        lines.append(f"| {o.get('priority')} | {o.get('ticker')} | {o.get('side_label') or o.get('side')} | {o.get('quantity')} | {o.get('limit_price')} | {submitted} | {o.get('expiry')} | {o.get('estimated_total')} | {o.get('confirmation_level')} |")
    lines += ["", "## Recent Activity", "", f"- Captured rows: {len([a for a in activity if a.get('account') == account])}", ""]
    lines += ["## Cash By Currency And Pending-Buy Reconstruction", "", "```json", json.dumps(reserve.get(account, {}), indent=2), "```", ""]
    lines += ["## Warnings", ""]
    account_warnings = [d for d in duplicates if account in d.get("accounts", [])] + [p for p in paired if p.get("account") == account]
    lines += [f"- `{json.dumps(w)}`" for w in account_warnings] or ["- None"]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys = sorted({k for row in rows for k in row if not isinstance(row.get(k), (dict, list))})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def write_indexes(out_dir: Path) -> None:
    for root_name in ["screenshots", "dom", "visible-text", "raw-exports"]:
        root = out_dir / root_name
        files = []
        if root.exists():
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    files.append({"path": str(p), "relative": str(p.relative_to(out_dir)), "size": p.stat().st_size})
        index_path = root / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(index_path, files)


def write_checksums(out_dir: Path) -> None:
    rows = []
    for p in sorted(out_dir.rglob("*")):
        if p.is_file() and p.name != "checksums.sha256":
            rows.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out_dir)}")
    (out_dir / "checksums.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")


def rewrite_text_paths(out_dir: Path, old_root: str, new_root: str) -> None:
    """Rewrite absolute evidence references after copying a bundle.

    Wealthsimple bundles intentionally contain absolute evidence paths so a
    pasted report can point back to the local proof files. When rebuilding into
    a new timestamped bundle, stale absolute paths make the bundle look
    internally inconsistent even if the evidence files were copied correctly.
    """
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old_root in text:
            path.write_text(text.replace(old_root, new_root), encoding="utf-8")


def detect_raw_exports(out_dir: Path) -> list[str]:
    root = out_dir / "raw-exports"
    if not root.exists():
        return []
    return [str(p) for p in sorted(root.rglob("*")) if p.is_file() and p.name != "index.json"]


def write_bundle(state: RunState, accounts_map: dict[str, dict[str, Any]], holdings: list[dict[str, Any]], orders: list[dict[str, Any]], unresolved_orders: list[dict[str, Any]], activity: list[dict[str, Any]], exports: dict[str, Any] | None = None) -> Path:
    bundle_started = time.monotonic()
    out = state.out_dir
    orders = [safe_quantity_record(order) for order in orders]
    accounts = [accounts_map[a] for a in ACCOUNTS if a in accounts_map]
    ambiguous_activity_rows = annotate_activity_identity_confidence(activity)
    reserve = compute_cash_reserve(accounts, orders)
    duplicates = duplicate_checks(holdings, orders)
    # Holdings-dependent checks are only meaningful when holdings were parsed.
    # Running them against an empty holdings set reports every open sell as
    # unbacked, which inverts the truth. Skip them and say so instead.
    holdings_available = bool(holdings)
    exports = exports or {}
    expected_security_holdings = fresh_export_security_holding_counts(exports)
    verified_empty_accounts = {
        account for account, count in expected_security_holdings.items()
        if count == 0 and account in accounts_map
    }
    accounts_with_holdings = {row.get("account") for row in holdings}
    accounts_missing_holdings = [
        a for a in accounts_map
        if a not in accounts_with_holdings and a not in verified_empty_accounts
    ]
    holdings_dependent_checks = ["paired_exit_checks", "filled_buy_exit_checks", "sell_order_coverage"]
    # Finalize completeness prerequisites before any downstream gated analysis.
    fresh_activity_control = has_fresh_canonical_activity_export(exports)
    export_fills = export_trade_fills(exports.get("activity_export_rows") or [])
    if "browser_activity_rows" in exports:
        browser_activity_control_rows = exports["browser_activity_rows"]
    else:
        browser_activity_control_rows = (
            activity if exports.get("activity_export_rows") else []
        )
    activity_source_reconciliation = (
        reconcile_activity_sources(
            browser_activity_control_rows,
            exports.get("activity_export_rows") or [],
            export_as_of=(exports.get("activity_export") or {}).get("as_of"),
        )
        if exports.get("activity_export_rows")
        else {
            "status": "not_available",
            "browser_candidate_count": 0,
            "export_trade_count": 0,
            "matched_browser_occurrences": 0,
            "matched_groups": [],
            "browser_only": [],
            "browser_only_review_required": [],
            "browser_only_recent_unsettled": [],
            "export_only": [],
            "browser_uncomparable": [],
            "export_uncomparable": [],
            "interpretation": "No Activity CSV was supplied.",
        }
    )
    status_unknown_classification = classify_status_token_free_rows(
        browser_activity_control_rows,
        activity_source_reconciliation,
        fresh_export=fresh_activity_control,
    )
    detail_confirmed_orders = [
        o for o in orders if o.get("confirmation_level") == "detail_confirmed"
    ]
    pending_non_orders = len(state.deposit_availability or [])
    completeness = assess_inventory_completeness(
        filter_default_before=state.filter_observed_default_before,
        filter_default_after=state.filter_observed_default_after,
        traversal_exhausted=state.traversal_exhausted or state.pending_scan_complete,
        unparsed_pending_count=len(state.unparsed_pending_controls or []),
        parsed_orders=len(orders),
        detail_confirmed_orders=len(detail_confirmed_orders),
        pending_non_orders=pending_non_orders,
        unresolved_orders=len(unresolved_orders),
        status_unknown_classification=status_unknown_classification,
        fresh_export=fresh_activity_control,
        blockers=list(state.blockers),
        broker_count_before=state.broker_pending_count_before,
        broker_count_after=state.broker_pending_count_after,
    )
    state.inventory_completeness = completeness
    inventory_complete = bool(completeness.get("inventory_complete"))
    if completeness.get("count_corroboration", {}).get("classification") == "contradiction":
        severity = completeness["count_corroboration"].get("severity")
        message = (
            "broker pending-transaction count contradicts parsed pending orders + "
            f"pending non-order activities (B={completeness['count_corroboration'].get('b1') or completeness['count_corroboration'].get('b0')}, "
            f"P+N={completeness['count_corroboration'].get('expected_p_plus_n')})"
        )
        if severity == "blocker":
            state.block(message)
        else:
            state.warn(message)
    if status_unknown_classification.get("fail_closed_missing_export"):
        state.warn(
            f"{status_unknown_classification['status_unknown_row_count']} status-token-free "
            "trade-shaped browser row(s) lack a fresh Activity CSV corroborating source"
        )
    elif status_unknown_classification.get("u3_unresolved_count"):
        state.warn(
            f"{status_unknown_classification['u3_unresolved_count']} status-token-free "
            "trade-shaped browser row(s) remain unresolved after source reconciliation "
            "and are treated as possible missing pending orders"
        )
    for code in completeness.get("historical_missing_observations") or []:
        state.warn(f"historical evidence gap: {code} not_observed_historically")

    paired = paired_exit_checks(holdings, orders, activity) if holdings_available else []
    if not inventory_complete:
        paired = [p for p in paired if p.get("type") != "holding_without_open_sell_exit"]
    filled_buy_exits = filled_buy_exit_checks(activity, orders, holdings) if holdings_available and inventory_complete else []
    coverage_rows = sell_coverage(
        holdings, orders, normalize_ticker_for_cross_reference,
        inventory_complete=inventory_complete,
    ) if holdings_available else []
    export_activity_analysis = analyze_activity_export(
        exports.get("activity_export_rows") or [],
        as_of=(exports.get("activity_export") or {}).get("as_of"),
    ) if export_fills or exports.get("activity_export_rows") else None
    holdings_source_reconciliation = reconcile_holdings_sources(
        holdings,
        exports.get("holdings_export_rows") or [],
    )
    if (
        fresh_activity_control
        and activity_source_reconciliation["browser_candidate_count"] == 0
        and activity_source_reconciliation["export_trade_count"] > 0
    ):
        state.warn(
            "fresh Activity CSV could not be compared against browser fills "
            "because no browser fill-like cards were captured"
        )
    elif (
        fresh_activity_control
        and activity_source_reconciliation.get("browser_only_review_required")
    ):
        state.warn(
            f"{len(activity_source_reconciliation['browser_only_review_required'])} "
            "older or undated browser-visible fill-like activity row(s) were "
            "not corroborated by the fresh settled Activity CSV"
        )
    if holdings_source_reconciliation.get("differences"):
        state.warn(
            f"{len(holdings_source_reconciliation['differences'])} browser/export "
            "holding quantity difference(s) require review"
        )
    duplicate_activity_rows = duplicate_activity_events(activity)
    if duplicate_activity_rows and export_fills:
        duplicate_activity_rows = correlate_duplicate_fills_with_export(
            duplicate_activity_rows, activity, export_fills)
    unresolved_duplicates = [
        row for row in duplicate_activity_rows
        if row.get("resolution") in {None, "no_matching_export_fill_review_manually"}
    ]
    corrected_duplicates = [
        row for row in duplicate_activity_rows
        if row.get("resolution") == "export_correlated_browser_duplicate"
    ]
    if corrected_duplicates:
        state.safety_log.append({
            "at": iso_now(), "event": "browser_duplicate_fills_corrected_from_export",
            "count": len(corrected_duplicates),
        })
    if unresolved_duplicates:
        state.warn(
            f"{len(unresolved_duplicates)} completed activity event(s) appear more than once with identical "
            "settled fields under different control IDs and no export fill corroborates the extra rows; "
            "completed-fill counts may be inflated"
        )
    accounts_with_parsed_holdings = (
        {row.get("account") for row in holdings} | verified_empty_accounts
    )
    capture_date = date.today()
    if state.source_capture_generated_at:
        try:
            capture_date = datetime.fromisoformat(
                state.source_capture_generated_at.replace("Z", "+00:00")
            ).date()
        except ValueError:
            capture_date = date.today()
    total_value_reconciliation = {
        row["account"]: reconcile_total_account_value(
            row, holdings, orders, capture_date=capture_date,
            components_captured=(
                bool(row.get("available_to_trade") or row.get("available_cash_cad"))
                and row["account"] in accounts_with_parsed_holdings
            ),
        )
        for row in accounts
    }
    # Preserve captured arithmetic even when incomplete, but qualify components.
    if not inventory_complete:
        for values in total_value_reconciliation.values():
            if isinstance(values, dict) and "residual_cad" in values:
                values["inventory_completeness_qualified"] = True
    deposit_comparisons = compare_deposit_residuals(
        total_value_reconciliation, state.deposit_availability, inventory_complete=inventory_complete)
    unexplained_totals = [
        name for name, values in total_value_reconciliation.items()
        if values.get("status") == "residual_unexplained"
    ]
    missing_cash_totals = [
        name for name, values in total_value_reconciliation.items()
        if values.get("status") == "not_reconcilable_cash_not_captured"
    ]
    missing_fx_totals = [
        name for name, values in total_value_reconciliation.items()
        if values.get("status") == "not_reconcilable_without_reference_fx_rate"
    ]
    if missing_cash_totals:
        state.warn(
            "account total could not be reconciled because required live components were not captured for: "
            + ", ".join(missing_cash_totals)
        )
    if missing_fx_totals:
        state.warn(
            "account total could not be reconciled because a current reference FX rate was not captured for: "
            + ", ".join(missing_fx_totals)
        )
    # Material residual_unexplained keeps its dedicated warning; every nonzero
    # residual is also disclosed unconditionally regardless of severity grade.
    if unexplained_totals:
        state.warn(
            "account total not fully explained by visible cash, holdings, and open-buy commitments for: "
            + ", ".join(
                f"{name} (residual {total_value_reconciliation[name]['residual_cad']} CAD, "
                f"{(total_value_reconciliation[name].get('residual_analysis') or {}).get('residual_percent_of_account_total')}% of the account, "
                f"{(total_value_reconciliation[name].get('residual_analysis') or {}).get('classification')})"
                for name in unexplained_totals
            )
        )
    residual_rows = residual_disclosure_rows(total_value_reconciliation)
    residual_warning = format_residual_warning(residual_rows)
    if residual_warning:
        state.warn(residual_warning)
    holdings_integrity = {
        "holdings_parsed": len(holdings),
        "accounts_captured": list(accounts_map),
        "accounts_without_parsed_holdings": accounts_missing_holdings,
        "accounts_verified_empty_by_fresh_holdings_export": sorted(
            verified_empty_accounts
        ),
        "holdings_dependent_checks_skipped": [] if holdings_available else holdings_dependent_checks,
        "csv_control": holdings_source_reconciliation,
    }
    if accounts_map and not holdings_available and accounts_missing_holdings:
        state.warn(
            "no holdings were parsed for any captured account; "
            f"{', '.join(holdings_dependent_checks)} were skipped rather than reported as findings"
        )
    elif accounts_missing_holdings:
        state.warn(
            "no holdings were parsed for: " + ", ".join(accounts_missing_holdings)
            + "; exit and oversell coverage is incomplete for those accounts"
        )
    status_counts = activity_status_counts(activity)
    activity_groups = activity_by_status(activity)
    fills_cancels = [a for a in activity if any(term in (a.get("side_label") or a.get("activity_type") or "").lower() for term in ["buy", "sell", "cancel", "expired", "rejected"])]
    accounts_missing = [a for a in ACCOUNTS if a not in accounts_map]
    if accounts_missing:
        state.block(f"missing expected accounts: {', '.join(accounts_missing)}")
        # Late blockers must deny previously computed completeness.
        completeness = assess_inventory_completeness(
            filter_default_before=state.filter_observed_default_before,
            filter_default_after=state.filter_observed_default_after,
            traversal_exhausted=state.traversal_exhausted or bool(state.pending_scan_complete),
            unparsed_pending_count=len(state.unparsed_pending_controls or []),
            parsed_orders=len(orders),
            detail_confirmed_orders=len(detail_confirmed_orders),
            pending_non_orders=pending_non_orders,
            unresolved_orders=len(unresolved_orders),
            status_unknown_classification=status_unknown_classification,
            fresh_export=fresh_activity_control,
            blockers=list(state.blockers),
            broker_count_before=state.broker_pending_count_before,
            broker_count_after=state.broker_pending_count_after,
            historical_missing_observations=list(completeness.get("historical_missing_observations") or []),
        )
        state.inventory_completeness = completeness
        inventory_complete = bool(completeness.get("inventory_complete"))
        if not inventory_complete:
            paired = [p for p in paired if p.get("type") != "holding_without_open_sell_exit"]
            filled_buy_exits = []
            coverage_rows = sell_coverage(
                holdings, orders, normalize_ticker_for_cross_reference,
                inventory_complete=False,
            ) if holdings_available else []
    state.pending_scan_complete = bool(inventory_complete)
    if unresolved_orders:
        state.warn(f"{len(unresolved_orders)} open orders remain row-only/unresolved")
    if ambiguous_activity_rows:
        state.warn(f"{ambiguous_activity_rows} collapsed cancelled/expired/rejected activity rows lack enough fields for deterministic identity; count them as row evidence, not exact order identities")
    for account in ACCOUNTS:
        entry = accounts_map.get(account) or {}
        if account in accounts_map and not (entry.get("available_to_trade") or entry.get("available_cash_cad")):
            state.warn(f"{account} available cash/buying power not directly verified")
        elif account in accounts_map and not entry.get("available_to_trade"):
            # Native Available CAD/USD are displayed evidence; only the CAD
            # aggregate label was interrupted, so the aggregate is derived and
            # must be reported as derived rather than as directly displayed.
            state.safety_log.append({
                "at": iso_now(), "event": "cash_aggregate_derived_from_native_balances", "account": account,
            })
        if account in accounts_map and not accounts_map[account].get("total_account_value"):
            state.warn(f"{account} total account value was not directly visible; it was not inferred from a holding or order amount")

    user_context = load_user_account_context()
    usd_context = user_context.get("usd_trading_accounts") or {}
    if usd_context.get("review_required"):
        state.warn(
            "user-reported USD trading-account access needs reconfirmation "
            f"(trial_expires {usd_context.get('trial_expires')}); USD balances are not proven tradable"
        )
    unknown_currency_orders = sorted(
        {key for values in reserve.values() for key in values.get("orders_with_unknown_settlement_currency") or []}
    )
    if unknown_currency_orders:
        state.warn(
            f"{len(unknown_currency_orders)} open orders have no settlement currency on their estimated total; "
            "their cash commitment is bucketed as UNKNOWN"
        )
    detail_count_by_account = count_by([o for o in orders if o.get("confirmation_level") in {"detail_confirmed", "export_confirmed"}], "account")
    row_only = [o for o in orders if o.get("confirmation_level") == "row_confirmed"]
    blocked_clicks = [event for event in state.click_log if event.get("blocked")]
    read_only_proven = not blocked_clicks and not any(
        event.get("event") == "forbidden_state_text" for event in state.safety_log
    )
    manifest = {
        "status": state.status,
        "mode": state.mode,
        "rebuilt_from": state.rebuilt_from,
        "source_capture_generated_at": state.source_capture_generated_at,
        "generated_at": iso_now(),
        "zip_path": str(out) + ".zip",
        "output_dir": str(out),
        "read_only_confirmation": read_only_proven,
        "trades_created_modified_or_cancelled": False if read_only_proven else None,
        "accounts_expected": ["TFSA", "RRSP", "Non-registered/cash"],
        "accounts_seen": list(accounts_map),
        "accounts_missing": accounts_missing,
        "all_expected_accounts_captured": not accounts_missing,
        "holdings_count_by_account": count_by(holdings, "account"),
        "open_orders_count_by_account": count_by(orders, "account"),
        "pending_scan_complete": state.pending_scan_complete,
        "deposit_availability": state.deposit_availability,
        "sell_coverage_inventory_complete": inventory_complete,
        "sell_coverage_scope": derive_sell_coverage_scope(
            reset_attempted=state.filter_reset_attempted,
            reset_click_succeeded=state.filter_reset_click_succeeded,
            filter_default_before=state.filter_observed_default_before,
            filter_default_after=state.filter_observed_default_after,
            inventory_complete=inventory_complete,
        ),
        "inventory_completeness": completeness,
        "broker_pending_count_before": state.broker_pending_count_before,
        "broker_pending_count_after": state.broker_pending_count_after,
        "status_unknown_row_classification": {
            "status_unknown_row_count": status_unknown_classification.get("status_unknown_row_count"),
            "export_corroborated_terminal_count": len(
                status_unknown_classification.get("export_corroborated_terminal") or []
            ),
            "proven_pending_order_count": len(
                status_unknown_classification.get("proven_pending_order") or []
            ),
            "unresolved_review_required_count": status_unknown_classification.get("u3_unresolved_count"),
            "corroboration_ran": status_unknown_classification.get("corroboration_ran"),
        },
        "unexplained_residuals": residual_rows,
        "detail_confirmed_orders_count_by_account": detail_count_by_account,
        "row_only_orders_count_by_account": count_by(row_only, "account"),
        "recent_activity_count_by_account": count_by(activity, "account"),
        "settled_activity_source": (
            "fresh_activity_csv_plus_browser_terminal_rows"
            if has_fresh_canonical_activity_export(exports)
            else "browser_activity_cards_and_drawers"
        ),
        "activity_capture_scope": (
            "fresh_csv_for_settled_activity; browser_scan_for_pending_and_terminal_non_fill_rows; completed_drawers_skipped"
            if has_fresh_canonical_activity_export(exports)
            else "browser_current_year_activity_cards_and_completed_drawers"
        ),
        "exports_used": detect_raw_exports(out),
        "screenshots_count": count_files(out / "screenshots"),
        "dom_files_count": count_files(out / "dom"),
        "visible_text_files_count": count_files(out / "visible-text"),
        "files_written": [],
        "user_account_context": user_context,
        "holdings_integrity": holdings_integrity,
        "duplicate_activity_events": duplicate_activity_rows,
        "completed_fill_rows_excluded_as_export_correlated_duplicates": sum(
            row.get("browser_rows_not_supported_by_export", 0) for row in duplicate_activity_rows),
        "exports": {key: value for key, value in (exports or {}).items() if not key.endswith("_rows")},
        "source_reconciliation": {
            "activity": activity_source_reconciliation,
            "holdings": holdings_source_reconciliation,
        },
        "account_total_reconciliation": total_value_reconciliation,
        "account_readiness_telemetry": "logs/account-readiness.json",
        "warnings": state.warnings,
        "blockers": state.blockers,
    }

    write_json(out / "account-balances.json", accounts)
    write_csv(out / "account-balances.csv", accounts)
    write_json(out / "holdings-all-accounts.json", holdings)
    write_csv(out / "holdings-all-accounts.csv", holdings)
    write_json(out / "open-orders-all-accounts.json", orders)
    write_csv(out / "open-orders-all-accounts.csv", orders)
    write_json(out / "open-orders-detail-confirmation.json", [o for o in orders if o.get("confirmation_level") in {"detail_confirmed", "export_confirmed"}])
    write_json(out / CURRENT_YEAR_ACTIVITY_JSON, activity)
    write_csv(out / CURRENT_YEAR_ACTIVITY_CSV, activity)
    write_json(out / "recent-activity-by-status.json", activity_groups)
    write_json(out / "recent-completed-filled.json", [row for row in activity if row.get("status") in {"Completed", "Filled", "Completed/Filled"}])
    write_json(out / "recent-cancelled.json", [row for row in activity if row.get("status") == "Cancelled"])
    write_json(out / "recent-expired.json", [row for row in activity if row.get("status") == "Expired"])
    write_json(out / "recent-rejected.json", [row for row in activity if row.get("status") in {"Rejected", "Failed"}])
    write_json(out / CURRENT_YEAR_FILLS_JSON, fills_cancels)
    write_json(out / "duplicate-checks.json", duplicates)
    write_json(out / "paired-exit-checks.json", paired)
    write_json(out / "filled-buy-exit-checks.json", filled_buy_exits)
    write_json(out / "sell-order-coverage.json", coverage_rows)
    (out / "sell-order-coverage.md").write_text(render_coverage(coverage_rows), encoding="utf-8")
    write_json(out / "cash-reserve-reconciliation.json", reserve)
    write_json(out / "holdings-integrity.json", holdings_integrity)
    write_json(out / "source-reconciliation.json", {
        "activity": activity_source_reconciliation,
        "holdings": holdings_source_reconciliation,
    })
    (out / "source-reconciliation.md").write_text(
        render_source_reconciliation_md(
            activity_source_reconciliation,
            holdings_source_reconciliation,
        ),
        encoding="utf-8",
    )
    if browser_activity_control_rows:
        write_json(
            out / "browser-activity-control-rows.json",
            browser_activity_control_rows,
        )
    write_json(out / "duplicate-activity-events.json", duplicate_activity_rows)
    if exports:
        write_json(out / "exports-provenance.json", {
            key: value for key, value in exports.items() if not key.endswith("_rows")})
        for key in ("activity_export_rows", "holdings_export_rows"):
            if exports.get(key):
                write_json(out / f"{key.replace('_', '-')}.json", exports[key])
    if export_activity_analysis is not None:
        write_json(out / "activity-export-analysis.json", export_activity_analysis)
        (out / "activity-export-analysis.md").write_text(
            render_activity_export_analysis_md(export_activity_analysis), encoding="utf-8")
    write_json(out / "account-total-reconciliation.json", total_value_reconciliation)
    write_json(out / "deposit-residual-comparison.json", deposit_comparisons)
    write_json(out / "unresolved-row-only-orders.json", unresolved_orders)
    write_json(out / "logs" / "account-readiness.json", state.account_readiness_traces)

    for account in ACCOUNTS:
        slug = ACCOUNT_SLUG[account]
        account_residuals = [r for r in residual_rows if r["account"] == account]
        summary = {
            "account": account,
            "balance": accounts_map.get(account),
            "holdings": [h for h in holdings if h.get("account") == account],
            "open_orders": [o for o in orders if o.get("account") == account],
            "recent_activity": [a for a in activity if a.get("account") == account],
            "buy_order_reserve_math": reserve.get(account),
            "account_total_reconciliation": total_value_reconciliation.get(account),
            "unexplained_residuals": account_residuals,
            "deposit_residual_comparison": [r for r in deposit_comparisons if r["account"] == account],
            "duplicate_exposures": [d for d in duplicates if account in d.get("accounts", [])],
            "filled_buys_missing_paired_exits": [p for p in paired if p.get("account") == account],
            "completed_filled_buys_missing_paired_exits": [p for p in filled_buy_exits if p.get("account") == account],
            "sell_order_coverage": [p for p in coverage_rows if p.get("account") == account],
            "inventory_completeness": completeness,
            "warnings": [w for w in state.warnings if account in w] + [
                w for w in state.warnings
                if w.startswith("account total residual unexplained")
                or w.startswith("account total not fully explained")
                or w.startswith("historical evidence gap")
                or "filter defaults not confirmed" in w
                or "inventory" in w.lower()
            ],
        }
        # Deduplicate warnings while preserving order.
        seen_w: set[str] = set()
        summary["warnings"] = [w for w in summary["warnings"] if not (w in seen_w or seen_w.add(w))]
        write_json(out / f"{slug}-summary.json", summary)
        residual_md = ""
        if account_residuals:
            residual_md = "\n## Account total residual\n\n" + "\n".join(
                f"- Unexplained residual: `{r['residual_cad']} CAD` "
                f"(status `{r.get('status')}`, classification `{r.get('classification')}`)"
                for r in account_residuals
            ) + "\n"
        elif total_value_reconciliation.get(account, {}).get("residual_cad") == 0:
            residual_md = "\n## Account total residual\n\n- Residual: `0 CAD` (fully explained)\n"
        (out / f"{slug}-summary.md").write_text(
            summarize_accounts_markdown(account, accounts, holdings, orders, activity, reserve, duplicates, paired)
            + residual_md
            + "\n## Capture warnings\n\n"
            + ("\n".join(f"- {w}" for w in summary["warnings"]) or "- None")
            + "\n"
            + "\n" + render_coverage(summary["sell_order_coverage"])
            + "\n".join(render_deposit_comparisons(summary["deposit_residual_comparison"])), encoding="utf-8")

    all_summary = {
        "deposit_residual_comparison": deposit_comparisons,
        "deposit_availability": state.deposit_availability,
        "status": state.status,
        "accounts": accounts,
        "holdings": holdings,
        "open_orders": orders,
        "recent_activity": activity,
        "settled_activity_source": manifest["settled_activity_source"],
        "activity_capture_scope": manifest["activity_capture_scope"],
        "cash_reserve_reconciliation": reserve,
        "duplicate_checks": duplicates,
        "paired_exit_checks": paired,
        "filled_buy_exit_checks": filled_buy_exits,
        "sell_order_coverage": coverage_rows,
        "activity_status_counts": status_counts,
        "canonical_export_activity": export_activity_analysis,
        "source_reconciliation": {
            "activity": activity_source_reconciliation,
            "holdings": holdings_source_reconciliation,
        },
        "account_total_reconciliation": total_value_reconciliation,
        "unexplained_residuals": residual_rows,
        "inventory_completeness": completeness,
        "warnings": state.warnings,
        "blockers": state.blockers,
    }
    write_json(out / "all-accounts-summary.json", all_summary)
    (out / "all-accounts-summary.md").write_text(render_all_accounts_md(all_summary)
        + "\n".join(render_deposit_comparisons(deposit_comparisons)), encoding="utf-8")
    warning_lines = [f"- {x}" for x in state.warnings + state.blockers] or ["- None"]
    # Dedicated summaries must survive CLI/GUI truncation of the general warning list.
    residual_section = ["", "## Unexplained residuals", ""]
    if residual_rows:
        residual_section += [
            f"- {r['account']}: residual `{r['residual_cad']} CAD` "
            f"(status `{r.get('status')}`, classification `{r.get('classification')}`)"
            for r in residual_rows
        ]
    else:
        residual_section.append("- None")
    completeness_section = [
        "", "## Inventory completeness", "",
        f"- State: `{completeness.get('completeness_state')}`",
        f"- Complete: `{completeness.get('inventory_complete')}`",
        f"- Reasons: `{', '.join(completeness.get('reason_codes') or []) or 'none'}`",
        f"- Count corroboration: `{(completeness.get('count_corroboration') or {}).get('classification')}`",
    ]
    (out / "capture-warnings.md").write_text(
        "\n".join(["# Capture warnings", ""] + warning_lines + residual_section + completeness_section) + "\n",
        encoding="utf-8",
    )
    write_json(out / "status-unknown-row-classification.json", {
        "export_corroborated_terminal": [
            {k: row.get(k) for k in ("account", "ticker", "side", "status", "total_value", "source_control_id", "stable_row_key")}
            for row in status_unknown_classification.get("export_corroborated_terminal") or []
        ],
        "proven_pending_order": status_unknown_classification.get("proven_pending_order") or [],
        "unresolved_review_required": [
            {k: row.get(k) for k in ("account", "ticker", "side", "status", "total_value", "source_control_id", "stable_row_key")}
            for row in status_unknown_classification.get("unresolved_review_required") or []
        ],
        "u3_unresolved_count": status_unknown_classification.get("u3_unresolved_count"),
        "corroboration_ran": status_unknown_classification.get("corroboration_ran"),
        "fail_closed_missing_export": status_unknown_classification.get("fail_closed_missing_export"),
    })
    (out / "README.md").write_text(render_readme(state, manifest), encoding="utf-8")
    (out / "next-message-for-chatgpt.md").write_text(render_next_message(manifest, all_summary), encoding="utf-8")
    write_json(out / "logs" / "click-log.json", state.click_log)
    write_json(out / "logs" / "safety-log.json", state.safety_log)
    # A browser-free rebuild preserves the original live timings rather than
    # replacing them with a misleading near-zero report-render duration.
    if "total_capture" not in state.performance:
        state.metric("total_capture", time.monotonic() - state.started)
    write_json(out / "logs" / "performance.json", state.performance)

    state.metric("bundle_write", time.monotonic() - bundle_started)
    write_indexes(out)
    manifest["files_written"] = [str(p.relative_to(out)) for p in sorted(out.rglob("*")) if p.is_file()]
    write_json(out / "manifest.json", manifest)
    write_checksums(out)

    zip_path = Path(str(out) + ".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(out)))
    return zip_path


def rebuild_bundle_from_existing(source_dir: Path, out_dir: Path | None = None) -> Path:
    if not source_dir.exists():
        raise FileNotFoundError(f"source bundle directory not found: {source_dir}")
    if out_dir is None:
        out_dir = Path(f"/tmp/wealthsimple-full-account-inventory-{stamp_now()}")
    if out_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output directory: {out_dir}")
    shutil.copytree(source_dir, out_dir)
    rewrite_text_paths(out_dir, str(source_dir), str(out_dir))

    manifest_path = out_dir / "manifest.json"
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    accounts = json.loads((out_dir / "account-balances.json").read_text(encoding="utf-8"))
    # Prefer the explicitly visible Home account cards. Account-detail pages
    # often omit an account total, so a rebuild must not erase valid Home
    # evidence and manufacture a balance warning.
    home_values: dict[str, str] = {}
    home_text_path = out_dir / "visible-text" / "account-overview" / "home.txt"
    if home_text_path.exists():
        home_values = parse_home_account_values(clean_lines(home_text_path.read_text(encoding="utf-8", errors="replace")))
    # Older builds accepted the first dollar amount after an account name as
    # the total value. Re-evaluate copied evidence so a rebuild cannot retain
    # a position/order amount masquerading as an account balance.
    for account in accounts:
        if account.get("account") in home_values:
            account["total_account_value"] = home_values[account["account"]]
            account["total_account_value_status"] = "directly_visible_home_card_currency_unlabeled"
            continue
        evidence = (account.get("evidence") or [{}])[0]
        text_path = evidence.get("visible_text")
        if not text_path or not Path(text_path).exists():
            continue
        lines = clean_lines(Path(text_path).read_text(encoding="utf-8", errors="replace"))
        direct_value = extract_direct_account_value(lines)
        account["total_account_value"] = direct_value
        account["total_account_value_status"] = "directly_visible" if direct_value else "not_directly_visible_not_inferred"
    accounts_map = {a["account"]: a for a in accounts}
    holdings = json.loads((out_dir / "holdings-all-accounts.json").read_text(encoding="utf-8"))
    orders = json.loads((out_dir / "open-orders-all-accounts.json").read_text(encoding="utf-8"))
    unresolved = json.loads((out_dir / "unresolved-row-only-orders.json").read_text(encoding="utf-8"))
    activity_path = out_dir / CURRENT_YEAR_ACTIVITY_JSON
    if not activity_path.exists():
        activity_path = out_dir / "recent-activity-since-2026-06-17.json"
    activity = json.loads(activity_path.read_text(encoding="utf-8")) if activity_path.exists() else []
    # Older live builds included Pending rows in recent activity because the
    # Activity feed is unfiltered. Pending belongs exclusively in the detailed
    # open-order ledger; preserve historical final/non-final events only.
    activity = [row for row in activity if order_state(row.get("status")) != "open"]
    # Earlier collectors labelled a collapsed buy/sell Activity card as
    # Completed/Filled even when Wealthsimple did not show a final status.
    # Preserve the event, but do not let an inference become a fill fact on a
    # rebuild. Detail-confirmed rows retain their explicit status.
    for row in activity:
        if row.get("status") == "Completed/Filled" and row.get("detail_status") != "detail_confirmed":
            row["status"] = "Status unconfirmed"
            row.setdefault("uncertainty_notes", []).append(
                "rebuild downgraded legacy statusless collapsed Activity row; no explicit final status was captured"
            )
    # Do not carry forward files whose name asserts a June 17 cutoff when the
    # rebuilt ledger is deliberately current-year scoped.
    for legacy_name in [
        "recent-activity-since-2026-06-17.json",
        "recent-activity-since-2026-06-17.csv",
        "fills-and-cancels-since-2026-06-17.json",
    ]:
        legacy_path = out_dir / legacy_name
        if legacy_path.exists():
            legacy_path.unlink()

    state = RunState(
        out_dir=out_dir,
        mode="REBUILD",
        rebuilt_from=str(source_dir),
        source_capture_generated_at=old_manifest.get("generated_at") or old_manifest.get("source_capture_generated_at"),
        # Never inherit an old success Boolean. Reassess from retained evidence.
        pending_scan_complete=False,
        deposit_availability=old_manifest.get("deposit_availability") or [],
    )
    # Preserve residual and completeness warnings; only drop warnings that are
    # re-derived from current evidence with a more specific replacement cause.
    state.warnings = [
        warning for warning in (old_manifest.get("warnings") or [])
        if not (
            (warning.startswith("visible non-target account card found:") and "cash-msb" in warning.lower())
            or warning.startswith("user-reported USD trading-account access needs reconfirmation")
            or "browser/export holding quantity difference(s) require review" in warning
            # Replaced by historical evidence-gap / new filter-default wording.
            or warning.startswith("all-account Activity filter reset not confirmed")
        )
    ]
    state.blockers = list(old_manifest.get("blockers") or [])
    click_log = out_dir / "logs" / "click-log.json"
    safety_log = out_dir / "logs" / "safety-log.json"
    if click_log.exists():
        state.click_log = json.loads(click_log.read_text(encoding="utf-8"))
    if safety_log.exists():
        state.safety_log = json.loads(safety_log.read_text(encoding="utf-8"))
    performance_path = out_dir / "logs" / "performance.json"
    if performance_path.exists():
        try:
            state.performance = json.loads(performance_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state.warn("copied performance log was invalid and could not be preserved")

    # Rehydrate filter / broker-count evidence from retained capture artifacts.
    filter_state_path = out_dir / "logs" / "activity-filter-state.json"
    if filter_state_path.exists():
        try:
            filter_state = json.loads(filter_state_path.read_text(encoding="utf-8"))
            snapshot = filter_state.get("snapshot")
            observed = confirms_unfiltered(snapshot) if snapshot is not None else None
            # Reinterpret retained snapshot; do not invent a post-traversal observation.
            state.filter_observed_default_before = observed
            state.filter_observed_default_after = None  # not_observed_historically
            state.filter_reset_attempted = bool(filter_state.get("reset_attempted", False))
            state.filter_reset_click_succeeded = bool(filter_state.get("reset_click_succeeded", False))
            # Legacy captures recorded confirmed_unfiltered under the old verifier.
            if "reset_attempted" not in filter_state and "explicit filter reset" in str(
                old_manifest.get("sell_coverage_scope") or ""
            ):
                # Unsupported explicit-reset wording: do not invent a reset action.
                state.filter_reset_attempted = False
                state.filter_reset_click_succeeded = False
        except json.JSONDecodeError:
            state.warn("copied activity filter state was invalid and could not be preserved")
    else:
        state.filter_observed_default_before = None
        state.filter_observed_default_after = None

    # Traversal exhaustion from scroll log / old pending_scan signal without inventing filter proof.
    scroll_log_path = out_dir / "logs" / "activity-scroll-log.json"
    if scroll_log_path.exists():
        try:
            scroll_log = json.loads(scroll_log_path.read_text(encoding="utf-8"))
            state.traversal_exhausted = bool(scroll_log) and old_manifest.get("pending_scan_complete") is not False
            # Prefer explicit stable-bottom evidence when the old run claimed incomplete only for filters.
            if old_manifest.get("pending_scan_complete") is False and (
                old_manifest.get("warnings") or []
            ) == ["all-account Activity filter reset not confirmed; sell coverage scope uncertain"]:
                state.traversal_exhausted = True
            elif old_manifest.get("pending_scan_complete") is True:
                state.traversal_exhausted = True
        except json.JSONDecodeError:
            state.traversal_exhausted = False

    # Broker count from retained activity-start text when available.
    activity_start = out_dir / "visible-text" / "activity" / "activity-start.txt"
    if activity_start.exists():
        count_value = parse_broker_pending_count(activity_start.read_text(encoding="utf-8", errors="replace"))
        # Historical captures typically retain one reading; treat as after-or-only partial.
        observation = make_count_observation(
            count_value,
            scope="all_accounts_activity",
            account_filter="all",
            status_filter="pending_transactions_label",
            population_meaning="broker_visible_pending_transactions",
            timestamp=old_manifest.get("generated_at"),
            source="historical_activity_start_visible_text",
        )
        state.broker_pending_count_before = None
        state.broker_pending_count_after = observation

    # Restore export inputs so rebuild does not silently drop CSV corroboration.
    exports: dict[str, Any] = {}
    provenance_path = out_dir / "exports-provenance.json"
    if provenance_path.exists():
        try:
            exports.update(json.loads(provenance_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            state.warn("copied exports provenance was invalid")
    for key, filename in (
        ("activity_export_rows", "activity-export-rows.json"),
        ("holdings_export_rows", "holdings-export-rows.json"),
    ):
        path = out_dir / filename
        if path.exists():
            try:
                exports[key] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state.warn(f"copied {filename} was invalid")
    browser_rows_path = out_dir / "browser-activity-control-rows.json"
    if browser_rows_path.exists():
        try:
            exports["browser_activity_rows"] = json.loads(browser_rows_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state.warn("copied browser activity control rows were invalid")

    for order in orders:
        order["priority"] = classify_order_priority(order)

    zip_path = write_bundle(state, accounts_map, holdings, orders, unresolved, activity, exports=exports)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["rebuilt_from"] = str(source_dir)
    # Preserve original capture timestamp; report generation time is separate.
    if state.source_capture_generated_at:
        manifest["source_capture_generated_at"] = state.source_capture_generated_at
    manifest["generated_at"] = iso_now()
    manifest["zip_path"] = str(zip_path)
    manifest["output_dir"] = str(out_dir)
    manifest["exports_used"] = detect_raw_exports(out_dir)
    manifest["files_written"] = [str(p.relative_to(out_dir)) for p in sorted(out_dir.rglob("*")) if p.is_file()]
    write_json(out_dir / "manifest.json", manifest)
    write_checksums(out_dir)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(out_dir)))
    return zip_path


def render_all_accounts_md(summary: dict[str, Any]) -> str:
    lines = ["# Wealthsimple Full Account Inventory", "", f"- Status: **{summary['status']}**", ""]
    lines += [
        "## Accounts", "",
        "`Total cash available` is Wealthsimple's CAD presentation of combined native CAD and USD balances at a live reference rate; it is not a native-CAD-only cash figure.", "",
        "| Account | Value | Total cash available (CAD aggregate) | Native available CAD | Native available USD |", "|---|---:|---:|---:|---:|",
    ]
    for a in summary["accounts"]:
        lines.append(
            f"| {a.get('account')} | {display_account_value(a)} | {display_value(a.get('available_to_trade'))} | "
            f"{display_value(a.get('available_cash_cad'))} | {display_value(a.get('available_cash_usd'))} |"
        )
    lines += ["", "## Holdings", "", "| Account | Ticker | Quantity | Value |", "|---|---|---:|---:|"]
    for h in summary["holdings"]:
        lines.append(f"| {h.get('account')} | {h.get('ticker')} | {h.get('quantity')} | {h.get('market_value')} |")
    source_reconciliation = summary.get("source_reconciliation") or {}
    activity_control = source_reconciliation.get("activity") or {}
    holdings_control = source_reconciliation.get("holdings") or {}
    lines += [
        "",
        "## Browser And CSV Controls",
        "",
        f"- Holdings quantity control: `{holdings_control.get('status', 'not_available')}`; "
        f"{len(holdings_control.get('differences') or [])} difference(s).",
        f"- Settled activity control: `{activity_control.get('status', 'not_available')}`; "
        f"{activity_control.get('matched_browser_occurrences', 0)} browser occurrence(s) matched, "
        f"{len(activity_control.get('browser_only_recent_unsettled') or [])} recent browser-only, "
        f"{len(activity_control.get('browser_only_review_required') or [])} browser-only requiring review, "
        f"{len(activity_control.get('export_only') or [])} CSV-only.",
        "- Row-level evidence: `source-reconciliation.json`.",
    ]
    lines += ["", "## Open Orders", "", "| Account | Ticker | Side | Qty | Limit | Submitted | Expiry | Total | Priority | Confirm |", "|---|---|---|---:|---:|---|---|---:|---|---|"]
    for o in summary["open_orders"]:
        submitted = " ".join(part for part in [o.get("submitted_date"), o.get("submitted_time")] if part) or "Not shown"
        lines.append(f"| {o.get('account')} | {o.get('ticker')} | {o.get('side_label') or o.get('side')} | {o.get('quantity')} | {o.get('limit_price')} | {submitted} | {o.get('expiry')} | {o.get('estimated_total')} | {o.get('priority')} | {o.get('confirmation_level')} |")
    lines += ["", render_coverage(summary.get("sell_order_coverage", []))]
    lines += ["", "## Recent Activity", "", f"- Source: `{summary.get('settled_activity_source', 'browser_activity_cards_and_drawers')}`", f"- Capture scope: `{summary.get('activity_capture_scope', 'browser_current_year_activity_cards_and_completed_drawers')}`", "", "```json", json.dumps(summary.get("activity_status_counts", {}), indent=2), "```", ""]
    for row in summary["recent_activity"]:
        lines.append(f"- {row.get('status')}: {row.get('account')} {row.get('ticker') or row.get('activity_type')} {row.get('quantity') or ''} {row.get('execution_price') or ''} {row.get('total_value') or ''} {row.get('date') or ''}")
    lines += ["", "## Warnings", ""]
    lines += [f"- {w}" for w in summary["warnings"]] or ["- None"]
    residuals = summary.get("unexplained_residuals") or residual_disclosure_rows(
        summary.get("account_total_reconciliation") or {}
    )
    lines += ["", "## Unexplained residuals", ""]
    if residuals:
        lines += [
            f"- {r['account']}: residual `{r['residual_cad']} CAD` "
            f"(status `{r.get('status')}`, classification `{r.get('classification')}`)"
            for r in residuals
        ]
    else:
        lines.append("- None")
    completeness = summary.get("inventory_completeness") or {}
    lines += [
        "", "## Inventory completeness", "",
        f"- State: `{completeness.get('completeness_state', 'unknown')}`",
        f"- Complete: `{completeness.get('inventory_complete')}`",
        f"- Reasons: `{', '.join(completeness.get('reason_codes') or []) or 'none'}`",
    ]
    lines += ["", "## Blockers", ""]
    lines += [f"- {b}" for b in summary["blockers"]] or ["- None"]
    return "\n".join(lines) + "\n"


def render_readme(state: RunState, manifest: dict[str, Any]) -> str:
    mode_line = (
        f"- Mode: browser-free rebuild of `{manifest.get('rebuilt_from')}`; not a live browser capture\n"
        if state.mode == "REBUILD" else "- Mode: read-only browser capture\n"
    )
    return (
        "# Wealthsimple Full Account Inventory Bundle\n\n"
        f"- Generated: `{manifest['generated_at']}`\n"
        f"- Status: `{manifest['status']}`\n"
        + mode_line
        + "- No trades were created, staged, modified, cancelled, or submitted by this tool.\n\n"
        + "Start with `next-message-for-chatgpt.md`, then use the JSON/CSV files for exact analysis.\n"
    )


def render_next_message(manifest: dict[str, Any], summary: dict[str, Any]) -> str:
    accounts = {a["account"]: a for a in summary["accounts"]}
    lines = [
        f"ZIP path: {manifest['zip_path']}",
        f"Status: {manifest['status']}",
        f"Accounts captured: {', '.join(manifest['accounts_seen'])}",
        "",
    ]
    if manifest.get("rebuilt_from"):
        lines += [
            f"Evidence freshness: this is a report rebuild from `{manifest['rebuilt_from']}` "
            f"(source capture generated `{manifest.get('source_capture_generated_at') or 'unknown'}`), not a new browser capture at report-generation time.",
            "",
        ]
    lines += render_deposits(summary.get("deposit_availability") or [])
    lines += render_deposit_comparisons(summary.get("deposit_residual_comparison") or [])
    residuals = summary.get("unexplained_residuals") or manifest.get("unexplained_residuals") or []
    lines += ["", "Unexplained account-total residuals (materiality does not hide these):"]
    if residuals:
        lines += [
            f"- {r['account']}: residual {r['residual_cad']} CAD "
            f"(status {r.get('status')}, classification {r.get('classification')})"
            for r in residuals
        ]
    else:
        lines.append("- None")
    completeness = summary.get("inventory_completeness") or manifest.get("inventory_completeness") or {}
    lines += [
        "",
        "Inventory completeness:",
        f"- state={completeness.get('completeness_state')}; complete={completeness.get('inventory_complete')}; "
        f"reasons={', '.join(completeness.get('reason_codes') or []) or 'none'}; "
        f"count_corroboration={(completeness.get('count_corroboration') or {}).get('classification')}",
        f"- sell_coverage_scope: {manifest.get('sell_coverage_scope')}",
    ]
    for account in ACCOUNTS:
        a = accounts.get(account, {})
        lines.append(
            f"{account}: value {display_account_value(a, 'not directly shown')}; "
            f"total cash available {display_value(a.get('available_to_trade'))} (CAD aggregate of native CAD + USD); "
            f"native available CAD {display_value(a.get('available_cash_cad'))}; "
            f"native available USD {display_value(a.get('available_cash_usd'))}."
        )
    usd_context = (manifest.get("user_account_context") or {}).get("usd_trading_accounts") or {}
    if usd_context:
        # The evidence basis and remaining trial window live in the manifest;
        # the handoff prose is what actually reaches a reader, so it must not
        # read as if this capture verified USD trading access.
        qualifiers = [f"evidence basis {usd_context.get('evidence_basis', 'unspecified')}"]
        if usd_context.get("days_until_expiry") is not None:
            qualifiers.append(f"{usd_context['days_until_expiry']} days until stated expiry")
        if usd_context.get("review_required"):
            qualifiers.append("reconfirmation required")
        if usd_context.get("days_until_forced_conversion") is not None:
            qualifiers.append(
                f"{usd_context['days_until_forced_conversion']} days until expected forced conversion"
            )
        event_label = (
            f"access ended {usd_context.get('access_ended')}"
            if usd_context.get("access_ended")
            else f"trial expiry {usd_context.get('trial_expires', 'not provided')}"
        )
        lines += ["", f"USD trading-account context ({(manifest.get('user_account_context') or {}).get('source', 'user-reported')}): "
                  f"{usd_context.get('status', 'unknown')}; {event_label}; "
                  f"{'; '.join(qualifiers)}. "
                  f"{usd_context.get('interpretation', '')}"]
    lines += [
        "",
        "Cash by native currency and pending-buy reconstruction:",
        "- `Total cash available` is a CAD conversion of the combined native CAD and USD balances, not a native-CAD-only balance. Wealthsimple's tooltip says the reference rate excludes the conversion spread.",
        "- Reconstructed native-currency cash before open-buy holds = displayed Available [currency] + open-buy estimated totals settled in that same currency.",
        "- Use an order's `estimated total` currency for the hold. A USD-quoted limit with a CAD estimated total is a CAD commitment, not a USD commitment.",
    ]
    reserve = summary.get("cash_reserve_reconciliation", {})
    for account in ACCOUNTS:
        values = reserve.get(account, {})
        lines.append(
            f"- {account}: displayed native available {json.dumps(values.get('broker_displayed_available_trading_capacity_by_currency', {}), sort_keys=True)}; "
            f"pending-buy commitments by settlement currency {json.dumps(values.get('estimated_pending_buy_commitments_by_settlement_currency', {}), sort_keys=True)}; "
            f"reconstructed before open-buy holds {json.dumps(values.get('reconstructed_cash_before_open_buy_holds_by_currency', {}), sort_keys=True)}."
        )
    lines += ["", "Holdings by account:"]
    for account in ACCOUNTS:
        hs = [h for h in summary["holdings"] if h.get("account") == account]
        lines.append(f"- {account}: " + ", ".join(f"{h.get('ticker')} {h.get('quantity')}" for h in hs))
    source_reconciliation = summary.get("source_reconciliation") or {}
    activity_control = source_reconciliation.get("activity") or {}
    holdings_control = source_reconciliation.get("holdings") or {}
    lines += [
        "",
        "Independent browser/CSV controls:",
        f"- Holdings: {holdings_control.get('status', 'not_available')}; "
        f"browser {holdings_control.get('browser_holding_count', 0)}, "
        f"export {holdings_control.get('export_holding_count', 0)}, "
        f"quantity differences {len(holdings_control.get('differences') or [])}.",
        f"- Settled activity: {activity_control.get('status', 'not_available')}; "
        f"browser fill-like cards {activity_control.get('browser_candidate_count', 0)}, "
        f"exported trades {activity_control.get('export_trade_count', 0)}, "
        f"matched browser occurrences {activity_control.get('matched_browser_occurrences', 0)}, "
        f"recent browser-only/possibly unsettled {len(activity_control.get('browser_only_recent_unsettled') or [])}, "
        f"browser-only requiring review {len(activity_control.get('browser_only_review_required') or [])}, "
        f"CSV-only {len(activity_control.get('export_only') or [])}.",
        "- The CSV does not replace live cash or Pending-order evidence. "
        "See source-reconciliation.json for every difference.",
    ]
    lines += ["", "Open orders by account:"]
    for account in ACCOUNTS:
        os = [o for o in summary["open_orders"] if o.get("account") == account]
        lines.append(f"- {account}: " + "; ".join(f"{o.get('ticker')} {o.get('side_label') or o.get('side')} {o.get('quantity') or '?'} @ {o.get('limit_price') or '?'} total {o.get('estimated_total')}" for o in os))
    q_status = [
        p for p in summary["paired_exit_checks"]
        if p.get("type") == "special_attention_status"
        and p.get("ticker") in {"QCOM", "CVS", "UPS", "TD"}
    ]
    lines += ["", "QCOM/CVS/UPS/TD status:", *[f"- {x['ticker']}: {x['status']}" for x in q_status]]
    noc_accounts = sorted({h.get("account") for h in summary["holdings"] if h.get("ticker") == "NOC"})
    lines += ["", f"NOC duplicate status: {'held in ' + ', '.join(noc_accounts) if len(noc_accounts) > 1 else 'not duplicated across captured holdings'}"]
    lines += ["", "Duplicate warnings:", *[f"- `{json.dumps(x)}`" for x in summary["duplicate_checks"]]]
    lines += ["", "Filled buys missing paired exits:", *[f"- `{json.dumps(x)}`" for x in summary["paired_exit_checks"] if x.get("type") != "special_attention_status"]]
    lines += ["", "Completed filled buys missing a current open exit:", *[f"- `{json.dumps(x)}`" for x in summary.get("filled_buy_exit_checks", [])]]
    lines += ["", render_coverage(summary.get("sell_order_coverage", []))]
    lines += ["", "Recent activity status counts:", f"- `{json.dumps(summary.get('activity_status_counts', {}))}`"]
    completed = [row for row in summary["recent_activity"] if row.get("status") in {"Completed", "Filled", "Completed/Filled"}]
    export_is_canonical = bool(summary.get("canonical_export_activity"))
    activity_source = summary.get("settled_activity_source", "browser_activity_cards_and_drawers")
    if activity_source == "fresh_activity_csv_plus_browser_terminal_rows":
        lines += [
            "",
            "Completed/fill-like trades:",
            "- Exact completed fills are listed once in the canonical Activity CSV section below. "
            "Cancelled/expired/rejected browser rows remain in the activity status totals.",
        ]
    else:
        lines += ["", "Completed/fill-like trades (browser Activity cards):"]
        if export_is_canonical:
            lines += [
                "- NOT canonical, and NOT to be added to the exported fills below. This is browser rendering retained for live cross-reference; where the two lists disagree about settled activity, the export wins."
            ]
        lines += [f"- {row.get('account')} {row.get('ticker')} {row.get('activity_type')} {row.get('quantity') or '?'} @ {row.get('execution_price') or '?'} total {row.get('total_value') or '?'} status {row.get('status')} date {row.get('date') or 'not shown'}" for row in completed] or ["- None captured"]
    export_activity = summary.get("canonical_export_activity")
    if export_activity:
        lines += [
            "", f"Canonical exported settled activity ({export_activity['target_year']}):",
            "- Source: user-supplied Wealthsimple Activity CSV. It is authoritative for settled activity, not pending orders or live balances.",
            f"- Export as of: {export_activity.get('export_as_of', 'not stated in the export')}; covers {(export_activity.get('export_coverage') or {}).get('earliest_transaction_date') or '?'} to {(export_activity.get('export_coverage') or {}).get('latest_transaction_date') or '?'}. Use the exact rows below for settled counts and totals.",
            f"- Current-year rows by type: `{json.dumps(export_activity['current_year_rows_by_type'], sort_keys=True)}`",
            *([f"- {export_activity['rows_outside_target_year']} further exported rows fall outside {export_activity['target_year']} and are not listed here."] if export_activity.get("rows_outside_target_year") else []),
            "- Exact current-year trade fills:",
        ]
        lines += [
            f"- {row.get('transaction_date')} {row.get('account_type')} {row.get('symbol')} "
            f"{row.get('activity_sub_type')} {row.get('quantity')} @ {row.get('unit_price')} "
            f"{row.get('currency')} net {row.get('net_cash_amount')}"
            for row in export_activity["current_year_trade_fills"]
        ] or ["- None"]
    row_only = [o for o in summary["open_orders"] if o.get("confirmation_level") == "row_confirmed"]
    lines += ["", f"Row-only/unresolved orders: {len(row_only)}"]
    lines += ["", "Do not add native CAD and USD without an explicit current FX rate. The reconstructed numbers use order estimates and may differ from settled cash if an order changes or Wealthsimple changes a hold."]
    if summary["warnings"] or summary["blockers"]:
        lines += ["", "Top blockers/warnings:", *[f"- {x}" for x in (summary["blockers"] + summary["warnings"])[:10]]]
    return "\n".join(lines) + "\n"


def display_value(value: Any, fallback: str = "Not shown") -> str:
    return str(value) if value not in {None, ""} else fallback


def display_account_value(account: dict[str, Any], fallback: str = "Not directly shown") -> str:
    value = display_value(account.get("total_account_value"), fallback)
    if account.get("total_account_value_status") == "directly_visible_home_card_currency_unlabeled" and value != fallback:
        return f"{value} (Home currency unlabeled)"
    return value


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row.get(key) or "Unknown"] = out.get(row.get(key) or "Unknown", 0) + 1
    return out


def count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def run_live(
    mode: str, out_dir: Path,
    activity_export: Path | None = None, holdings_export: Path | None = None,
    pending_detail_mode: str = "serial",
    account_capture_mode: str = "preloaded",
    activity_receipt: Path | None = None,
) -> tuple[RunState, Path]:
    ensure_dirs(out_dir)
    state = RunState(out_dir=out_dir, mode=mode)
    exports = import_exports(state, activity_export, holdings_export, activity_receipt)
    use_csv_fast_path = has_fresh_canonical_activity_export(exports)
    accounts_map: dict[str, dict[str, Any]] = {}
    holdings: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    activity: list[dict[str, Any]] = []
    account_preload: dict[str, Any] | None = None
    reader: WealthsimpleReader | None = None
    try:
        reader = WealthsimpleReader(
            state,
            expected_security_holdings=fresh_export_security_holding_counts(exports),
        )
        print("Phase 1/6: checking the existing controlled browser", flush=True)
        initial = reader.capture("account-overview", "initial-page", strict_state_scan=True)
        text = Path(initial["visible_text"]).read_text(encoding="utf-8", errors="replace")
        if state.blockers:
            pass
        elif "Log in" in text and "Password" in text:
            state.block("Wealthsimple login page is visible; user must log in before inventory can run")
        else:
            print("Phase 2/6: staging account-page evidence and exact read-only URLs", flush=True)
            accounts_started = time.monotonic()
            if account_capture_mode == "preloaded":
                account_preload = reader.begin_home_account_preload()
                if account_preload is None and not state.blockers:
                    state.safety_log.append({
                        "at": iso_now(),
                        "event": "account_preload_serial_fallback",
                    })
                    accounts_map = reader.click_home_account_cards_serial()
                    state.metric(
                        "account_pages_capture",
                        time.monotonic() - accounts_started,
                        len(accounts_map),
                    )
            else:
                accounts_map = reader.click_home_account_cards_serial()
                state.metric(
                    "account_pages_capture",
                    time.monotonic() - accounts_started,
                    len(accounts_map),
                )
            if not state.blockers:
                if account_preload is None:
                    reader.capture_holdings_dashboard()
                if not state.blockers:
                    print("Phase 3/6: finding all pending orders", flush=True)
                    activity_ev, rows = reader.capture_activity()
                    reader.capture_pending_deposit_availability()
                    if not state.blockers:
                        print("Phase 4/6: confirming pending order details", flush=True)
                        details, unresolved_from_open = reader.open_order_details(
                            rows, pending_detail_mode
                        )
                        # Account pages compete heavily with the interactive
                        # drawer pass. Launch only after every pending detail is
                        # captured, then overlap the list-only activity scan.
                        if account_preload is not None:
                            if reader.launch_home_account_preload_workers(
                                account_preload
                            ):
                                state.safety_log.append({
                                    "at": iso_now(),
                                    "event": "holdings_dashboard_preloaded_in_worker",
                                })
                            else:
                                account_preload = None
                        orders, unresolved_merge = merge_rows_and_details(rows, details)
                        if use_csv_fast_path:
                            # The export is canonical for settled activity and
                            # was freshly generated before this capture. Keep
                            # the live browser focused on what the CSV cannot
                            # know: every currently Pending order and its
                            # detail drawer.
                            print("Phase 5/6: using fresh canonical Activity CSV; scanning browser only for cancelled/expired/rejected rows", flush=True)
                            _recent_ev, browser_activity_rows = reader.capture_recent_activity()
                            # Preserve the independent browser dataset before
                            # the canonical CSV replaces settled rows in the
                            # final ledger. write_bundle reconciles both
                            # sources without reopening completed drawers.
                            exports["browser_activity_rows"] = (
                                browser_activity_rows
                            )
                            terminal_details = reader.open_terminal_activity_details(
                                browser_activity_rows
                            )
                            browser_activity_rows = merge_activity_details(
                                browser_activity_rows, terminal_details
                            )
                            exports["browser_activity_rows"] = browser_activity_rows
                            activity = merge_fresh_export_with_browser_terminal_activity(
                                exports["activity_export_rows"], browser_activity_rows,
                                as_of=(exports.get("activity_export") or {}).get("as_of"),
                            )
                            state.metric("browser_completed_activity_drawers_skipped", 0.0, len(activity))
                            state.safety_log.append({
                                "at": iso_now(),
                                "event": "fresh_activity_csv_replaced_browser_completed_activity_drawers",
                                "current_year_rows": len(activity),
                            })
                        else:
                            print("Phase 5/6: reading recent completed, cancelled, expired, and rejected activity", flush=True)
                            _recent_ev, activity_rows = reader.capture_recent_activity()
                            completed_details = reader.open_completed_activity_details(activity_rows)
                            activity = merge_activity_details(activity_rows, completed_details)
                        unresolved = reconcile_unresolved_order_records(
                            rows, unresolved_from_open, unresolved_merge
                        )
                        if account_preload is not None:
                            state.safety_log.append({
                                "at": iso_now(),
                                "event": "account_preload_overlapped_browser_pipeline",
                                "overlapped": [
                                    "holdings-dashboard",
                                    "recent-activity-scan",
                                ],
                            })
                            harvest_started = time.monotonic()
                            accounts_map = (
                                reader.finish_home_account_preload(account_preload)
                                or {}
                            )
                            state.metric(
                                "account_pages_capture",
                                time.monotonic() - harvest_started,
                                len(accounts_map),
                            )
                            account_preload = None
                            if not accounts_map and not state.blockers:
                                fallback_started = time.monotonic()
                                state.safety_log.append({
                                    "at": iso_now(),
                                    "event": "account_preload_serial_fallback",
                                })
                                accounts_map = reader.click_home_account_cards_serial()
                                reader.capture_holdings_dashboard()
                                state.metric(
                                    "account_pages_serial_fallback",
                                    time.monotonic() - fallback_started,
                                    len(accounts_map),
                                )
                        if (
                            account_capture_mode == "preloaded"
                            and account_preload is None
                            and not accounts_map
                            and not state.blockers
                        ):
                            fallback_started = time.monotonic()
                            state.safety_log.append({
                                "at": iso_now(),
                                "event": "account_preload_serial_fallback",
                            })
                            accounts_map = (
                                reader.click_home_account_cards_serial()
                            )
                            reader.capture_holdings_dashboard()
                            state.metric(
                                "account_pages_serial_fallback",
                                time.monotonic() - fallback_started,
                                len(accounts_map),
                            )
                        if not state.blockers:
                            holdings_started = time.monotonic()
                            holdings = parse_holdings_from_account_texts(accounts_map)
                            state.metric(
                                "holdings_parse",
                                time.monotonic() - holdings_started,
                                len(holdings),
                            )
    except Exception as exc:
        state.block(f"live inventory crashed before completion: {exc!r}")
        crash_path = out_dir / "logs" / "crash-traceback.txt"
        crash_path.parent.mkdir(parents=True, exist_ok=True)
        crash_path.write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if reader is not None and account_preload is not None:
            reader.abort_home_account_preload(account_preload)
    print("Phase 6/6: building the local ledger and evidence bundle", flush=True)
    zip_path = write_bundle(state, accounts_map, holdings, orders, unresolved, activity, exports)
    return state, zip_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Wealthsimple full account inventory capture")
    parser.add_argument("--mode", choices=["FAST", "FULL"], default="FULL")
    parser.add_argument("--out-dir", help="Output directory. Defaults to /tmp/wealthsimple-full-account-inventory-$TS")
    parser.add_argument("--rebuild-from", help="Existing bundle directory to copy and regenerate reports from final JSON truth")
    parser.add_argument("--activity-export", help="Optional Wealthsimple activity CSV export; canonical for completed activity")
    parser.add_argument("--activity-download-receipt", help="Explicit hash-bound receipt from this tool's Activity downloader")
    parser.add_argument("--holdings-export", help="Optional Wealthsimple holdings CSV export; canonical for positions")
    parser.add_argument(
        "--pending-detail-mode",
        choices=["batched", "serial"],
        default="serial",
        help="Serial opens one read-only Pending drawer at a time; batched is experimental",
    )
    parser.add_argument(
        "--account-capture-mode",
        choices=["preloaded", "serial"],
        default="preloaded",
        help="Preloaded overlaps read-only account-page loading; serial retains Home-card traversal",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ts = stamp_now()
    out_dir = Path(args.out_dir or f"/tmp/wealthsimple-full-account-inventory-{ts}")
    if args.rebuild_from:
        zip_path = rebuild_bundle_from_existing(Path(args.rebuild_from), out_dir)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        counts = {
            "holdings": sum(manifest["holdings_count_by_account"].values()),
            "open_orders": sum(manifest["open_orders_count_by_account"].values()),
            "detail_confirmed_orders": sum(manifest["detail_confirmed_orders_count_by_account"].values()),
            "row_only_orders": sum(manifest["row_only_orders_count_by_account"].values()),
            "recent_activity_rows": sum(manifest["recent_activity_count_by_account"].values()),
        }
        print(str(zip_path))
        print(manifest["status"])
        print(", ".join(manifest["accounts_seen"]))
        print(json.dumps(counts, sort_keys=True))
        print(f"Rebuilt Wealthsimple inventory bundle from final JSON truth; no browser actions or trade/order state changes were made.")
        print("; ".join((manifest["blockers"] + manifest["warnings"])[:8]) or "None")
        return 0 if manifest["status"] in {"OK", "WARN"} else 2
    state, zip_path = run_live(
        args.mode, out_dir,
        Path(args.activity_export) if args.activity_export else None,
        Path(args.holdings_export) if args.holdings_export else None,
        args.pending_detail_mode,
        args.account_capture_mode,
        Path(args.activity_download_receipt) if args.activity_download_receipt else None,
    )
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    counts = {
        "holdings": sum(manifest["holdings_count_by_account"].values()),
        "open_orders": sum(manifest["open_orders_count_by_account"].values()),
        "detail_confirmed_orders": sum(manifest["detail_confirmed_orders_count_by_account"].values()),
        "row_only_orders": sum(manifest["row_only_orders_count_by_account"].values()),
        "recent_activity_rows": sum(manifest["recent_activity_count_by_account"].values()),
    }
    print(str(zip_path))
    print(manifest["status"])
    print(", ".join(manifest["accounts_seen"]))
    print(json.dumps(counts, sort_keys=True))
    print(f"Read-only Wealthsimple inventory captured {counts['holdings']} holdings and {counts['open_orders']} open orders across {len(manifest['accounts_seen'])} accounts; no trade/order state changes were made.")
    print("; ".join((manifest["blockers"] + manifest["warnings"])[:8]) or "None")
    return 0 if manifest["status"] in {"OK", "WARN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
