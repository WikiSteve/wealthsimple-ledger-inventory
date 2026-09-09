"""Synthetic disclosure shapes; no account data, browser, or trading actions."""
from copy import deepcopy
import json
from decimal import Decimal

import pytest

from src.sell_coverage import quantity, quantity_evidence, verified_remaining, render_coverage
from src.full_account_inventory import (
    RunState, WealthsimpleReader, ensure_dirs, parse_order_detail_blocks,
    parse_pending_rows_from_controls, parse_activity_rows_from_controls,
    unparsed_pending_controls, parse_holdings_row_cells, sell_order_coverage,
    write_bundle, rebuild_bundle_from_existing, merge_rows_and_details,
)


def detail(*, account="RRSP", ticker="XYZ", status="Pending", original="12 shares",
           filled=None, remaining=None, oid="order-a", currency="CAD"):
    text = f"Account\n{account}\nStatus\n{status}\nSubmitted\nSeptember 9, 2026\n9:00 am\nExpires\nSeptember 10, 2026\n4:00 pm\nType\nLimit sell\nLimit price\n$10.00 {currency}\n"
    for label, value in (("Entered quantity", original), ("Filled quantity", filled), ("Remaining quantity", remaining)):
        if value is not None:
            text += f"{label}\n{value}\n"
    text += f"Estimated total proceeds\n$120.00 CAD\nView {ticker} details\n"
    order = parse_order_detail_blocks(text, {"url": "fixture", "visible_text": "synthetic.txt"})[0]
    order["source_control_id"] = oid
    return order, text


def holding(account="RRSP", ticker="XYZ", qty="12", currency="CAD"):
    return parse_holdings_row_cells([ticker, currency, "5%", qty, "$10.00", "$120.00"], account, {"url": "fixture"})


def summaries(holdings, orders, complete=True):
    return [r for r in sell_order_coverage(holdings, orders, inventory_complete=complete) if "coverage_status" in r]


def test_entered_only_and_legacy_are_uncertain_not_zero():
    order, _ = detail(original="5 shares")
    assert order["original_quantity"] == "5"
    assert order["remaining_quantity"] is None
    assert order["quantity_provenance"]["raw_labels"]["remaining"] is None
    current = summaries([holding()], [order])[0]
    assert current['open_sell_remaining'] == '5'
    assert current['uncovered_quantity'] == '7'
    assert current['coverage_quantity_bases'] == ['pending_entered_quantity']
    for candidate in ({**order, "quantity_provenance": None, "remaining_quantity": "5"},):
        row = summaries([holding()], [candidate])[0]
        assert row["coverage_status"] == "uncertain"
        assert row["open_sell_remaining"] is None
        assert row["uncovered_quantity"] is None
        assert "remaining sell quantity unknown" in render_coverage([row])


def test_two_sell_parser_to_coverage():
    a, _ = detail(original="4", filled="1 shares x $10.00 CAD", oid="a", status="Partially filled")
    b, _ = detail(original="2", remaining="2", oid="b")
    row = summaries([holding()], [a, b])[0]
    assert row["type"] == "partial_sell_coverage"
    assert row["severity"] == "info"
    assert (row["holding_quantity"], row["open_sell_remaining"], row["uncovered_quantity"]) == ("12", "5", "7")
    assert row["open_sell_orders_found"] == 2


def test_explicit_remaining_equal_original_with_no_fill_is_valid():
    order, _ = detail(original="12", remaining="12")
    assert verified_remaining(order) == (Decimal(12), None)
    assert summaries([holding()], [order])[0]["coverage_status"] == "fully_covered"


def test_forged_wrong_label_and_tampered_value_rejected():
    order, _ = detail(remaining="5")
    order["quantity_provenance"]["raw_labels"]["remaining"] = "Entered quantity"
    assert verified_remaining(order)[0] is None
    order, _ = detail(remaining="5")
    order["remaining_quantity"] = "6"
    assert verified_remaining(order)[0] is None


@pytest.mark.parametrize("original,filled,remaining", [
    ("5", "6", None), ("5", None, "6"), ("5", "2", "4"),
    ("5", "nonsense", "2"), ("-5", "0", "2"), ("5", "NaN", None),
])
def test_contradictory_or_invalid_labels_stay_uncertain(original, filled, remaining):
    order, _ = detail(original=original, filled=filled, remaining=remaining)
    assert verified_remaining(order)[0] is None
    assert summaries([holding()], [order])[0]["uncovered_quantity"] is None


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1e3", "1,2", "1.2.3", "", None, True, "9" * 101])
def test_strict_quantity(value):
    assert quantity(value) is None


def test_exact_fractions_and_zero():
    a, _ = detail(original="0.3", filled="0.1", oid="a")
    b, _ = detail(original="0.1", remaining="0.1", oid="b")
    row = summaries([holding(qty="0.3")], [a, b])[0]
    assert row["open_sell_remaining"] == "0.3"
    assert row["uncovered_quantity"] == "0.0"
    zero, _ = detail(original="1", filled="1")
    assert verified_remaining(zero)[0] == 0
    assert summaries([holding()], [zero])[0]["coverage_status"] == "uncovered"
    assert quantity("1,234.50 shares") == Decimal("1234.50")


@pytest.mark.parametrize("status", ["Pending", "Partially filled", "Pending cancellation", "Pending cancel", "Cancel requested"])
def test_active_status_discovery_detail_merge_report(status):
    control = {"id": "header-a", "text": f"XYZ\nLimit sell\nRRSP\n$120.00 CAD\n{status}"}
    rows = parse_pending_rows_from_controls([control], {"url": "fixture"})
    assert len(rows) == 1 and rows[0]["status"] == status
    assert not unparsed_pending_controls([control])
    assert parse_activity_rows_from_controls([control], {"url": "fixture"})[0]["status"] == status
    order, _ = detail(status=status, original="8", filled="3", oid="header-a")
    merged, unresolved = merge_rows_and_details(rows, [order])
    assert not unresolved
    assert summaries([holding()], merged)[0]["open_sell_remaining"] == "5"


@pytest.mark.parametrize("status", ["Cancelled", "Canceled", "Expired", "Rejected", "Filled", "Completed", "Failed"])
def test_terminal_state_excluded_even_with_nonzero_remainder(status):
    order, _ = detail(status=status, remaining="8")
    row = summaries([holding()], [order])[0]
    assert row["open_sell_orders_found"] == 0
    assert row["open_sell_remaining"] == "0"


def test_unknown_status_and_unknown_quantities_do_not_hide_known_excess():
    a, _ = detail(original="20", remaining="20", oid="a")
    b, _ = detail(status="Unrecognized state", oid="b")
    rows = sell_order_coverage([holding()], [a, b], inventory_complete=True)
    row = [r for r in rows if "coverage_status" in r][0]
    assert row["coverage_status"] == "excess_open_sells"
    assert row["excess_quantity_lower_bound"] == "8"
    assert row["open_sell_remaining"] is None
    assert row["uncovered_quantity"] is None
    assert "order_status_unknown" in row["uncertainty_reasons"]
    assert "order a has 20 coverage units (explicit_remaining)" in render_coverage(rows)


def test_incomplete_default_and_explicit_complete_empty():
    assert sell_order_coverage([holding()], [])[0]["coverage_status"] == "uncertain"
    assert summaries([holding()], [], complete=False)[0]["uncovered_quantity"] is None
    assert summaries([holding()], [])[0]["uncovered_quantity"] == "12"


def test_account_isolation_and_missing_account():
    order, _ = detail(account="TFSA", remaining="5")
    rows = summaries([holding("RRSP"), holding("TFSA")], [order])
    assert {r["account"]: r["open_sell_remaining"] for r in rows} == {"RRSP": "0", "TFSA": "5"}
    order["account"] = None
    assert all(r["uncovered_quantity"] is None for r in summaries([holding()], [order]))
    buy = {"account": None, "ticker": None, "side": "buy", "status": "Pending"}
    assert summaries([holding()], [buy])[0]["coverage_status"] == "uncovered"


def test_alias_currency_and_instrument_conflicts():
    order, _ = detail(ticker="XYZ", remaining="5")
    assert summaries([holding(ticker="XYZ.TO")], [order])[0]["open_sell_remaining"] == "5"
    us, _ = detail(ticker="XYZ", remaining="5", currency="USD")
    # CAD settlement does not change a USD quote into a CAD instrument.
    assert us["settlement_currency"] == "CAD" and us["security_quote_currency"] == "USD"
    assert summaries([holding(ticker="XYZ.TO")], [us])[0]["coverage_status"] == "uncertain"
    order["security_id"] = "security-2"
    h = {**holding(), "security_id": "security-1"}
    order["status"] = "mystery"
    reasons = summaries([h], [order])[0]["uncertainty_reasons"]
    assert "instrument_identity_conflict" in reasons and "order_status_unknown" in reasons
    assert summaries([{**holding(), "classification": "CDR"}], [detail(remaining="5")[0]])[0]["coverage_status"] == "uncertain"
    market, _ = detail(remaining="5")
    market["security_quote_currency"] = None
    market["order_currency"] = "CAD"  # Legacy estimate/settlement fallback is not a quote.
    assert summaries([holding()], [market])[0]["coverage_status"] == "uncertain"


def test_share_classes_do_not_merge():
    order, _ = detail(ticker="XYZ.B", remaining="5")
    rows = summaries([holding(ticker="XYZ.A")], [order])
    hrow = next(r for r in rows if r["ticker"] == "XYZ.A")
    assert hrow["open_sell_orders_found"] == 0


def test_deduplication_only_on_identity_and_no_conflicting_lower_bound():
    order, _ = detail(original="20", remaining="20", oid="real-id")
    copy = {**order, "screenshot_evidence_reference": "different.png"}
    assert summaries([holding()], [order, copy])[0]["known_open_sell_remaining"] == "20"
    copy = deepcopy(order)
    copy.update(quantity_evidence("1", None, "1"))
    row = summaries([holding()], [order, copy])[0]
    assert row["coverage_status"] == "uncertain"
    assert row["known_open_sell_remaining"] == "0"
    del order["source_control_id"]
    # Same text hash is NOT proof of same order.
    assert summaries([holding()], [order, dict(order)])[0]["coverage_status"] == "uncertain"


def test_duplicate_holdings_and_linked_orders_are_uncertain():
    order, _ = detail(remaining="5")
    assert summaries([holding(), holding()], [order])[0]["coverage_status"] == "uncertain"
    order["oco_group"] = "alternative-exits"
    assert summaries([holding()], [order])[0]["open_sell_remaining"] is None


def test_disclosures_do_not_borrow_quantities_from_next_order():
    first, text = detail(original="5")
    _, second = detail(ticker="QQQ", remaining="3")
    parsed = parse_order_detail_blocks(text + second, {"url": "fixture"})
    assert parsed[0]["remaining_quantity"] is None
    assert parsed[1]["remaining_quantity"] == "3"
    duplicate = text.replace("Entered quantity\n5", "Entered quantity\n5\nEntered quantity\n5")
    assert parse_order_detail_blocks(duplicate, {"url": "fixture"})[0]["remaining_quantity"] is None


def test_parser_through_bundle_reports_and_legacy_rebuild(tmp_path):
    out = tmp_path / "bundle"
    ensure_dirs(out)
    order, _ = detail(original="5", remaining="5")
    accounts = {a: {"account": a, "available_to_trade": "$10.00 CAD"} for a in ("TFSA", "RRSP", "Non-registered")}
    state = RunState(out_dir=out, mode="FULL", pending_scan_complete=True)
    write_bundle(state, accounts, [holding()], [order], [], [])
    report = json.loads((out / "sell-order-coverage.json").read_text())[0]
    assert report["uncovered_quantity"] == "7"
    for name in ("sell-order-coverage.md", "rrsp-summary.md", "all-accounts-summary.md", "next-message-for-chatgpt.md"):
        text = (out / name).read_text()
        assert "partially_uncovered" in text and "remaining sell quantity 5" in text
    # Legacy copied quantity, no provenance and no completeness proof.
    legacy = {**order, "remaining_quantity": "5"}
    del legacy["quantity_provenance"]
    (out / "open-orders-all-accounts.json").write_text(json.dumps([legacy]))
    manifest = json.loads((out / "manifest.json").read_text())
    manifest.pop("pending_scan_complete")
    (out / "manifest.json").write_text(json.dumps(manifest))
    rebuilt = tmp_path / "rebuilt"
    rebuild_bundle_from_existing(out, rebuilt)
    result = json.loads((rebuilt / "sell-order-coverage.json").read_text())[0]
    assert result["coverage_status"] == "uncertain" and result["uncovered_quantity"] is None
    assert json.loads((rebuilt / "open-orders-all-accounts.json").read_text())[0]["remaining_quantity"] is None


def test_same_notional_ladder_legs_keep_their_own_detail_quantities():
    controls = [{"id": oid, "text": "XYZ\nLimit sell\nRRSP\n$120.00 CAD\nPending"} for oid in ("a", "b")]
    rows = parse_pending_rows_from_controls(controls, {"url": "fixture"})
    a, _ = detail(original="3", remaining="3", oid="a")
    b, _ = detail(original="2", remaining="2", oid="b")
    merged, unresolved = merge_rows_and_details(rows, [b, a])
    assert not unresolved
    assert [o["remaining_quantity"] for o in merged] == ["3", "2"]
    assert summaries([holding()], merged)[0]["uncovered_quantity"] == "7"
    # One leg's detail cannot be recycled for its sibling.
    _, unresolved = merge_rows_and_details(rows, [a])
    assert len(unresolved) == 1 and unresolved[0]["source_control_id"] == "b"


@pytest.mark.parametrize("status", ["Partially filled", "Pending cancellation"])
def test_unparsed_new_status_is_not_silent(status):
    bad = {"text": f"not-a-ticker\nLimit sell\nRRSP\n{status}"}
    assert unparsed_pending_controls([bad])


@pytest.mark.parametrize("bottom,early_bad,reset,expected", [(True, False, True, True), (False, False, True, False), (True, True, True, False), (True, False, False, False)])
def test_capture_completeness_and_earlier_viewport_miss(tmp_path, monkeypatch, bottom, early_bad, reset, expected):
    monkeypatch.setattr("src.full_account_inventory.time.sleep", lambda _: None)
    ensure_dirs(tmp_path)
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = RunState(out_dir=tmp_path, mode="FULL")
    class Driver:
        current_url = "fixture"
        def execute_script(self, script):
            return bottom if "return window.innerHeight" in script else None
    reader.driver = Driver()
    reader.go_app_path = lambda *a: None
    reader.wait_for_activity_cards = lambda *a: True
    reader.wait_for_pending_activity_cards = lambda *a: True
    reader.click_label = lambda *a, **k: reset
    reader.settle = lambda *a: True
    reader.disclosure_controls_present = lambda: True
    reader.capture = lambda *a, **k: {"url": "fixture", "visible_text": "fixture", "screenshot": "fixture"}
    reader._find_safe_button = lambda *a: None
    calls = 0
    def controls():
        nonlocal calls
        calls += 1
        return [{"text": "Limit sell\nPartially filled\nRRSP\nnot-a-ticker"}] if early_bad and calls == 1 else []
    reader.controls = controls
    reader.capture_activity()
    assert reader.state.pending_scan_complete is expected


def test_newly_exposed_bottom_viewport_is_parsed_before_completion(tmp_path, monkeypatch):
    monkeypatch.setattr("src.full_account_inventory.time.sleep", lambda _: None)
    ensure_dirs(tmp_path)
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = RunState(out_dir=tmp_path, mode="FULL")
    class Driver:
        current_url = "fixture"
        scrolls = 0
        def execute_script(self, script):
            if "window.scrollBy" in script:
                self.scrolls += 1
            if "return window.innerHeight" in script:
                return self.scrolls >= 4
    reader.driver = Driver()
    reader.go_app_path = lambda *a: None
    reader.wait_for_activity_cards = lambda *a: True
    reader.click_label = lambda *a, **k: True
    reader.settle = lambda *a: True
    reader.disclosure_controls_present = lambda: True
    reader.capture = lambda *a, **k: {"url": "fixture", "visible_text": "fixture", "screenshot": "fixture"}
    reader._find_safe_button = lambda *a: None
    reader.controls = lambda: [{"id": "last-row", "text": "XYZ\nLimit sell\nRRSP\n$120.00 CAD\nPartially filled"}] if reader.driver.scrolls >= 4 else []
    _, rows = reader.capture_activity()
    assert reader.state.pending_scan_complete is True
    assert len(rows) == 1 and rows[0]["source_control_id"] == "last-row"
