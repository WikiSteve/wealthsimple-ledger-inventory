import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from src.sell_coverage import quantity_evidence

from src.full_account_inventory import (
    BLOCKING_MODAL_ESCAPE_ATTEMPTS,
    BLOCKING_MODAL_SCRIPT,
    DANGEROUS_CLICK_TEXT,
    FORBIDDEN_STATE_PATTERNS,
    RunState,
    WealthsimpleReader,
    account_page_is_ready,
    account_page_readiness_failures,
    activity_row_is_older_than_cutoff,
    activity_bucket,
    classify_order_priority,
    extract_direct_account_value,
    parse_activity_row_text,
    parse_activity_rows_from_controls,
    parse_account_identity_and_balance,
    parse_completed_activity_detail,
    parse_terminal_activity_detail,
    parse_holdings_lines,
    parse_holdings_row_cells,
    parse_holdings_rows_from_dom,
    parse_home_account_values,
    parse_order_detail_blocks,
    parse_order_row_text,
    analyze_activity_export,
    canonical_activity_rows_from_export,
    cash_movement_row_is_complete,
    has_fresh_canonical_activity_export,
    merge_fresh_export_with_browser_terminal_activity,
    reconcile_activity_sources,
    reconcile_holdings_sources,
    render_activity_export_analysis_md,
    parse_pending_rows_from_controls,
    ACCOUNT_CARD_SETTLE_SECONDS,
    ACTIVITY_EXPORT_FIELDS,
    ACTIVITY_FILTER_SETTLE_SECONDS,
    APP_NAVIGATION_SETTLE_SECONDS,
    ACTIVITY_DETAIL_BATCH_CLOSE_SCRIPT,
    ACTIVITY_DETAIL_BATCH_SCRIPT,
    ACTIVITY_DETAIL_SNAPSHOT_SCRIPT,
    CONTROLS_SCRIPT,
    CONTROL_CLICK_SETTLE_SECONDS,
    EXTERNAL_NAVIGATION_SETTLE_SECONDS,
    POLL_INTERVAL_SECONDS,
    compute_cash_reserve,
    correlate_duplicate_fills_with_export,
    duplicate_activity_events,
    export_trade_fills,
    import_exports,
    normalize_export_rows,
    read_wealthsimple_export,
    reconciliation_status,
    looks_like_ticker,
    unparsed_pending_controls,
    unparsed_terminal_controls,
    ensure_dirs,
    is_non_trading_day,
    is_ignored_non_investing_account_href,
    load_user_account_context,
    looks_like_money,
    normalize_ticker_for_cross_reference,
    paired_exit_checks,
    reconcile_total_account_value,
    render_next_message,
    split_timestamp_value,
    total_cash_tooltip_is_exposed,
    terminal_activity_identity_key,
    terminal_activity_row_is_complete,
    sell_order_coverage,
    filled_buy_exit_checks,
    home_investing_account_cards_ready,
    value_currency,
    wait_until,
    write_bundle,
)


FIX = Path(__file__).parent / "fixtures"


def test_full_parse_order_row_text():
    row = parse_order_row_text("NOC\nNOC\nLimit sell\nTFSA\n$87.80 CAD\nPending")
    assert row is not None
    assert row["account"] == "TFSA"
    assert row["ticker"] == "NOC"
    assert row["side"] == "sell"
    assert row["estimated_total"] == "$87.80 CAD"
    assert row["confirmation_level"] == "row_confirmed"


def test_full_order_row_key_stable_across_relative_dates():
    today = parse_order_row_text("Today\nNOC\nNOC\nLimit sell\nTFSA\n$87.80 CAD\nPending")
    yesterday = parse_order_row_text("Yesterday\nNOC\nNOC\nLimit sell\nTFSA\n$87.80 CAD\nPending")
    assert today is not None
    assert yesterday is not None
    assert today["stable_row_key"] == yesterday["stable_row_key"]


def test_home_cards_are_ready_before_home_totals_finish_hydrating():
    hrefs = [
        "https://my.wealthsimple.com/app/account-details/ca-cash-msb-123",
        "https://my.wealthsimple.com/app/account-details/tfsa-123",
        "https://my.wealthsimple.com/app/account-details/rrsp-123",
        "https://my.wealthsimple.com/app/account-details/non-registered-123",
    ]
    assert home_investing_account_cards_ready(hrefs)
    assert not home_investing_account_cards_ready(hrefs[:-1])


def test_full_rrsp_space_orders_are_core_priority():
    assert classify_order_priority({"account": "RRSP", "ticker": "RKLB", "side": "buy"}) == "P1 CORE"
    assert classify_order_priority({"account": "RRSP", "ticker": "SPCX", "side": "buy"}) == "P1 CORE"


def test_full_parse_order_detail_blocks():
    text = (FIX / "order-detail-mp.txt").read_text()
    details = parse_order_detail_blocks(text, {"visible_text": "fixture.txt", "screenshot": "fixture.png", "url": "fixture"})
    mp = [o for o in details if o["account"] == "Non-registered" and o["ticker"] == "MP"][0]
    assert mp["side"] == "sell"
    assert mp["limit_price"] == "$79.11 USD"
    assert mp["quantity"] == "10 shares"
    assert mp["confirmation_level"] == "detail_confirmed"


def test_full_parse_twenty_four_hour_order_detail_blocks():
    text = """
Account
RRSP
Status
Pending
Submitted
June 23, 2026
9:14 am
Expires
June 24, 2026
10:30 am
Trading session
24-hour market
Open until end of 24-hour session
Type
Limit buy
Limit price
$44.70 USD
Entered quantity
2 shares
Estimated total cost
$130.07 CAD
Cancel order
Modify order
View RKLB details
"""
    details = parse_order_detail_blocks(text, {"visible_text": "fixture.txt", "screenshot": "fixture.png", "url": "fixture"})
    rklb = details[0]
    assert rklb["account"] == "RRSP"
    assert rklb["ticker"] == "RKLB"
    assert rklb["side"] == "buy"
    assert rklb["limit_price"] == "$44.70 USD"
    assert rklb["quantity"] == "2 shares"
    assert rklb["estimated_total"] == "$130.07 CAD"


def test_full_parse_truncated_order_detail_is_not_detail_confirmed():
    text = """
Account
RRSP
Status
Pending
Type
Limit buy
Estimated total cost
$130.07 CAD
Modify order
View RKLB details
"""
    details = parse_order_detail_blocks(text, {"visible_text": "fixture.txt", "screenshot": "fixture.png", "url": "fixture"})
    rklb = details[0]
    assert rklb["account"] == "RRSP"
    assert rklb["ticker"] == "RKLB"
    assert rklb["estimated_total"] == "$130.07 CAD"
    assert rklb["confirmation_level"] == "row_confirmed"
    assert "detail pane truncated" in rklb["uncertainty_notes"][0]


def test_full_parse_holdings_lines_dynamic():
    text = (FIX / "account-card-non-registered.txt").read_text()
    rows = parse_holdings_lines([line.strip() for line in text.splitlines() if line.strip()], "Non-registered", {"visible_text": "fixture.txt"})
    tickers = {row["ticker"] for row in rows}
    assert {"BAM", "BN", "F", "LUN", "MP", "SPIR", "UFO"} <= tickers
    mp = [row for row in rows if row["ticker"] == "MP"][0]
    assert mp["quantity"] == "10 shares"
    assert mp["market_value"] == "$606.53 USD"


def test_activity_parser_keeps_final_rows_out_of_pending_orders():
    filled = parse_activity_row_text("INTC\nLimit sell\nRRSP\n$160.00 CAD")
    expired = parse_activity_row_text("SPCX\nLimit buy\nRRSP\n$288.64 CAD\nExpired")
    pending = parse_activity_row_text("QCOM\nLimit buy\nTFSA\n$49.00 CAD\nPending")
    assert filled is not None and filled["status"] == "Status unconfirmed"
    assert expired is not None and expired["status"] == "Expired"
    assert pending is not None and pending["status"] == "Pending"
    assert filled["detail_status"] == "row_confirmed"


def test_money_parser_accepts_sign_space_rendered_by_activity_cards():
    assert looks_like_money("− $1,234.56 CAD")
    assert looks_like_money("- $1,234.56 CAD")


def test_terminal_activity_detail_confirms_deterministic_identity():
    row = parse_activity_row_text(
        "Withdrawal\nElectronic funds transfer\nTFSA\nCancelled\n− $1,234.56 CAD"
    )
    assert row is not None
    detail = parse_terminal_activity_detail(
        """
Withdrawal
Electronic funds transfer
TFSA
Cancelled
− $1,234.56 CAD
From
TFSA
To
Masked external account
Status
Cancelled
Date
January 15, 2025
Amount
− $1,234.56 CAD
""",
        row,
        {"visible_text": "fixture.txt", "screenshot": ""},
    )
    assert detail is not None
    assert detail["detail_status"] == "detail_confirmed"
    assert detail["status"] == "Cancelled"
    assert detail["total_value"] == "− $1,234.56 CAD"


def test_cancelled_efts_are_complete_cash_movements_not_broken_orders():
    withdrawal = parse_activity_row_text(
        "Withdrawal\nElectronic funds transfer\nTFSA\nCancelled\n− $1,234.56 CAD"
    )
    deposit = parse_activity_row_text(
        "Recurring deposit\nElectronic funds transfer\nTFSA\nCancelled\n$10.00 CAD"
    )
    for row, direction in [(withdrawal, "out"), (deposit, "in")]:
        assert row is not None
        row["date"] = "January 15, 2025"
        assert row["activity_domain"] == "cash_movement"
        assert row["movement_method"] == "Electronic funds transfer"
        assert row["movement_direction"] == direction
        assert cash_movement_row_is_complete(row)
        assert terminal_activity_row_is_complete(row)


def test_terminal_identity_key_exposes_same_day_same_amount_collision():
    first = parse_activity_row_text("NOC\nLimit sell\nTFSA\nCancelled\n$98.75 CAD")
    second = parse_activity_row_text("NOC\nLimit sell\nTFSA\nCancelled\n$98.75 CAD")
    assert first is not None and second is not None
    first["date"] = second["date"] = "August 20, 2026"
    assert terminal_activity_identity_key(first) == terminal_activity_identity_key(second)
    assert terminal_activity_row_is_complete(first)


def test_currency_conversion_export_has_evidence_backed_leg_fields():
    rows = canonical_activity_rows_from_export([{
        "transaction_date": "2026-08-09",
        "effective_date": "2026-08-09",
        "effective_time": "15:10:09",
        "account_type": "TFSA",
        "activity_type": "FxExchange",
        "activity_sub_type": "-",
        "cad_per_usd_rate": "1.36",
        "currency": "USD",
        "quantity": "-199.81",
        "net_cash_amount": "-199.81",
    }], year=2026)
    assert len(rows) == 1
    row = rows[0]
    assert row["activity_domain"] == "currency_conversion"
    assert row["conversion_leg_currency"] == "USD"
    assert row["conversion_leg_amount"] == "-199.81"
    assert row["conversion_leg_direction"] == "sold"
    assert row["cad_per_usd_rate"] == "1.36"
    assert row["conversion_pair_key"]


def test_activity_date_context_prevents_same_total_rows_from_conflating():
    controls = [
        {"text": "INTC\nLimit sell\nRRSP\n$160.00 CAD", "date_context": "June 30, 2026"},
        {"text": "INTC\nLimit sell\nRRSP\n$160.00 CAD", "date_context": "June 18, 2026"},
    ]
    rows = parse_activity_rows_from_controls(controls, {"url": "fixture"})
    assert len(rows) == 2
    assert {row["date"] for row in rows} == {"June 30, 2026", "June 18, 2026"}


def test_control_ids_keep_same_notional_rows_distinct():
    controls = [
        {"id": "pending-a", "text": "NOC\nLimit sell\nTFSA\n$87.80 CAD\nPending"},
        {"id": "pending-b", "text": "NOC\nLimit sell\nTFSA\n$87.80 CAD\nPending"},
    ]
    rows = parse_pending_rows_from_controls(controls, {"url": "fixture"})
    assert len(rows) == 2
    assert len({row["stable_row_key"] for row in rows}) == 2


def test_pending_unknown_action_is_retained_for_detail_confirmation():
    controls = [{"id": "fractional-1", "text": "TST\nFractional buy\nTFSA\n$10.00 CAD\nPending"}]
    rows = parse_pending_rows_from_controls(controls, {"url": "fixture"})
    assert len(rows) == 1
    assert rows[0]["side"] == "buy"

    unknown = parse_order_row_text("TST\nTrailing stop sell\nTFSA\n$10.00 CAD\nPending")
    assert unknown is not None
    assert unknown["side"] == "sell"
    assert unknown["order_type"] == "unknown"

    non_order = [{"id": "pending-dividend", "text": "NOC\nDividend\nTFSA\n$2.50 CAD\nPending"}]
    assert parse_pending_rows_from_controls(non_order, {"url": "fixture"}) == []


def test_activity_current_year_cutoff_keeps_unknown_and_current_rows():
    assert not activity_row_is_older_than_cutoff({"date": "Today"}, today=date(2026, 7, 9))
    assert not activity_row_is_older_than_cutoff({"date": "June 30, 2026"}, today=date(2026, 7, 9))
    assert activity_row_is_older_than_cutoff({"date": "December 31, 2025"}, today=date(2026, 7, 9))
    assert not activity_row_is_older_than_cutoff({"date": None}, today=date(2026, 7, 9))


def test_account_value_is_not_inferred_from_first_position_value():
    lines = ["TFSA", "CAD", "Account value", "Returns", "Holdings", "ADBE", "$851.00 CAD"]
    assert extract_direct_account_value(lines) is None
    assert extract_direct_account_value(["Account value", "$11,642.67 CAD", "Returns"]) == "$11,642.67 CAD"


def test_home_cards_supply_direct_account_totals_when_visible():
    lines = ["Accounts", "TFSA", "TFSA", "$30,296.54 CAD", "RRSP", "RRSP", "$4,662.47 CAD", "Non-registered", "$1,955.40 CAD"]
    assert parse_home_account_values(lines) == {
        "TFSA": "$30,296.54 CAD",
        "RRSP": "$4,662.47 CAD",
        "Non-registered": "$1,955.40 CAD",
    }


def test_chequing_card_is_an_intentional_non_investing_exclusion():
    assert is_ignored_non_investing_account_href("https://my.wealthsimple.com/app/account-details/ca-cash-msb-abc")
    assert not is_ignored_non_investing_account_href("https://my.wealthsimple.com/app/account-details/tfsa-abc")


def test_chatgpt_handoff_keeps_cash_balances_separated_by_currency():
    summary = {
        "accounts": [{
            "account": "TFSA", "total_account_value": "$100.00", "available_to_trade": "$75.00 CAD",
            "available_cash_cad": "$50.00 CAD", "available_cash_usd": "$20.00 USD",
        }],
        "holdings": [], "open_orders": [], "paired_exit_checks": [], "duplicate_checks": [],
        "filled_buy_exit_checks": [], "recent_activity": [], "activity_status_counts": {},
        "cash_reserve_reconciliation": {
            "TFSA": {
                "broker_displayed_available_trading_capacity_by_currency": {"CAD": 50.0, "USD": 20.0},
                "estimated_pending_buy_commitments_by_settlement_currency": {"CAD": 25.0},
                "reconstructed_cash_before_open_buy_holds_by_currency": {"CAD": 75.0, "USD": 20.0},
            },
        },
        "warnings": [], "blockers": [],
    }
    manifest = {"zip_path": "/tmp/test.zip", "status": "OK", "accounts_seen": ["TFSA"]}
    text = render_next_message(manifest, summary)
    assert "native available CAD $50.00 CAD" in text
    assert "native available USD $20.00 USD" in text
    assert 'reconstructed before open-buy holds {"CAD": 75.0, "USD": 20.0}' in text
    assert "reserved CAD Not shown" not in text


def test_chatgpt_handoff_ignores_non_status_findings_for_special_tickers():
    summary = {
        "accounts": [], "holdings": [], "open_orders": [],
        "paired_exit_checks": [
            {
                "type": "holding_without_open_sell_exit",
                "account": "TFSA", "ticker": "QCOM",
            },
            {
                "type": "special_attention_status",
                "ticker": "QCOM", "status": "activity_seen",
            },
        ],
        "duplicate_checks": [], "filled_buy_exit_checks": [],
        "recent_activity": [], "activity_status_counts": {},
        "cash_reserve_reconciliation": {}, "warnings": [], "blockers": [],
    }
    manifest = {
        "zip_path": "/tmp/test.zip", "status": "OK", "accounts_seen": []
    }

    text = render_next_message(manifest, summary)

    assert "- QCOM: activity_seen" in text


def test_chatgpt_handoff_lists_canonical_export_fills_only_once():
    fill = {
        "transaction_date": "2026-07-24",
        "account_type": "TFSA",
        "symbol": "CRM",
        "activity_sub_type": "BUY",
        "quantity": "75",
        "unit_price": "12",
        "currency": "CAD",
        "net_cash_amount": "-900",
    }
    summary = {
        "accounts": [],
        "holdings": [],
        "open_orders": [],
        "paired_exit_checks": [],
        "duplicate_checks": [],
        "filled_buy_exit_checks": [],
        "recent_activity": [{
            "account": "TFSA",
            "ticker": "CRM",
            "activity_type": "Buy trade",
            "status": "Completed",
            "quantity": "75",
            "execution_price": "$12 CAD",
            "total_value": "$900.00 CAD",
            "date": "2026-07-24",
        }],
        "activity_status_counts": {"Completed": 1},
        "cash_reserve_reconciliation": {},
        "warnings": [],
        "blockers": [],
        "settled_activity_source": "fresh_activity_csv_plus_browser_terminal_rows",
        "canonical_export_activity": {
            "target_year": 2026,
            "export_as_of": "2026-07-28 20:44 GMT-03:00",
            "export_coverage": {
                "earliest_transaction_date": "2025-07-31",
                "latest_transaction_date": "2026-07-28",
            },
            "current_year_rows_by_type": {"Trade": 1},
            "rows_outside_target_year": 0,
            "current_year_trade_fills": [fill],
        },
    }
    manifest = {
        "zip_path": "/tmp/test.zip",
        "status": "OK",
        "accounts_seen": [],
    }

    text = render_next_message(manifest, summary)

    assert text.count("2026-07-24 TFSA CRM BUY 75 @ 12 CAD net -900") == 1
    assert "Exact completed fills are listed once" in text
    assert "do not add the completed list" not in text


def test_pending_view_waits_past_a_loading_shell(tmp_path):
    import src.full_account_inventory as inventory

    class FakeTime:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class Driver:
        current_url = "https://my.wealthsimple.com/app/activity"

    calls = []
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = RunState(out_dir=tmp_path, mode="FULL")
    reader.driver = Driver()
    reader.controls = lambda: []
    reader.body_text = lambda: "Activity\nLoading"
    original_time = inventory.time
    original_parser = inventory.parse_pending_rows_from_controls
    try:
        inventory.time = FakeTime()

        def parser(_controls, _evidence):
            calls.append(1)
            return [] if len(calls) < 3 else [{"ticker": "QCOM"}]

        inventory.parse_pending_rows_from_controls = parser
        ready = reader.wait_for_pending_activity_cards(
            "test", timeout=2.0
        )
    finally:
        inventory.time = original_time
        inventory.parse_pending_rows_from_controls = original_parser

    assert ready
    assert len(calls) == 3


def test_activity_buckets_keep_in_flight_and_income_out_of_completed():
    assert activity_bucket("Completed/Filled") == "Completed"
    assert activity_bucket("Cancelled") == "Cancelled"
    assert activity_bucket("Expired") == "Expired"
    assert activity_bucket("In progress") == "In flight"
    assert activity_bucket("Status unconfirmed") == "Status unconfirmed"
    assert activity_bucket("Activity recorded") == "Income / transfer / other"


def test_filled_buy_with_no_current_holding_is_not_missing_exit():
    activity = [
        {"account": "TFSA", "ticker": "FM", "side": "buy", "status": "Completed", "quantity": "5 shares"},
        {"account": "RRSP", "ticker": "LUN", "side": "buy", "status": "Completed", "quantity": "10 shares"},
    ]
    findings = filled_buy_exit_checks(activity, [], [{"account": "RRSP", "ticker": "LUN", "quantity": "10 shares"}])
    assert [(row["account"], row["ticker"]) for row in findings] == [("RRSP", "LUN")]


def test_sell_coverage_flags_only_real_oversell_or_missing_position():
    holdings = [{"account": "TFSA", "ticker": "NOC", "quantity": "10 shares", "current_price_currency": "CAD"}]
    orders = [
        {"account": "TFSA", "ticker": "NOC", "side": "sell", "quantity": "4 shares"},
        {"account": "RRSP", "ticker": "NOC", "side": "sell", "quantity": "2 shares"},
        {"account": "TFSA", "ticker": "NOC", "side": "sell", "quantity": "12 shares"},
    ]
    # Legacy quantity-only records must NOT establish excess. The treatment
    # explicitly supplies independently observed remaining labels in fixtures.
    assert not any(r["type"] == "open_sell_exceeds_visible_holding" for r in sell_order_coverage(holdings, orders))
    for idx, order in enumerate(orders):
        order.update(quantity_evidence(order["quantity"], None, order["quantity"]))
        order.update(status="Pending", source_control_id=f"fixture-{idx}", security_quote_currency="CAD")
    findings = sell_order_coverage(holdings, orders, inventory_complete=True)
    kinds = {row["type"] for row in findings}
    assert "open_sell_without_visible_holding" in kinds
    assert "open_sell_exceeds_visible_holding" in kinds


def test_sell_coverage_flags_aggregate_ladder_oversell():
    holdings = [{"account": "TFSA", "ticker": "NOC", "quantity": "10 shares", "current_price_currency": "CAD"}]
    orders = [
        {"account": "TFSA", "ticker": "NOC", "side": "sell", "quantity": "6 shares"},
        {"account": "TFSA", "ticker": "NOC", "side": "sell", "quantity": "5 shares"},
    ]
    for idx, order in enumerate(orders):
        order.update(quantity_evidence(order["quantity"], None, order["quantity"]))
        order.update(status="Pending", source_control_id=f"fixture-{idx}", security_quote_currency="CAD")
    findings = sell_order_coverage(holdings, orders, inventory_complete=True)
    aggregate = [item for item in findings if item["type"] == "aggregate_open_sells_exceed_visible_holding"]
    assert len(aggregate) == 1
    assert aggregate[0]["holding_quantity"] == "10"
    assert aggregate[0]["aggregate_order_quantity"] == "11"
    assert aggregate[0]["excess_quantity_lower_bound"] == "1"


def test_cash_reserve_does_not_blend_cad_and_usd():
    accounts = [{"account": "TFSA", "available_to_trade": "$500.00 CAD"}]
    orders = [
        {"account": "TFSA", "side": "buy", "estimated_total": "$100.00 CAD", "order_currency": "CAD", "stable_row_key": "cad"},
        {"account": "TFSA", "side": "buy", "estimated_total": "$50.00 USD", "order_currency": "USD", "stable_row_key": "usd"},
    ]
    reserve = compute_cash_reserve(accounts, orders)["TFSA"]
    assert reserve["sum_estimated_open_buy_costs"] is None
    assert reserve["sum_estimated_open_buy_costs_by_currency"] == {"CAD": 100.0, "USD": 50.0}


def test_cash_reserve_follows_estimated_total_settlement_currency_not_usd_quote_currency():
    accounts = [{"account": "TFSA", "available_to_trade": "$500.00 CAD", "available_cash_cad": "$300.00 CAD"}]
    orders = [{
        "account": "TFSA", "side": "buy", "estimated_total": "$251.84 CAD",
        "order_currency": "USD", "settlement_currency": "CAD", "stable_row_key": "mp-usd-quote-cad-estimate",
    }]
    reserve = compute_cash_reserve(accounts, orders)["TFSA"]
    assert reserve["sum_estimated_open_buy_costs_by_currency"] == {"CAD": 251.84}
    assert reserve["broker_displayed_available_trading_capacity_by_currency"] == {"CAD": 300.0}
    assert reserve["reconstructed_cash_before_open_buy_holds_by_currency"] == {"CAD": 551.84}


def test_completed_activity_detail_extracts_exact_fill_fields():
    row = parse_activity_row_text("INTC\nINTC\nLimit sell\nRRSP\n$160.00 CAD")
    assert row is not None
    text = """
INTC
INTC
Limit sell
RRSP
$160.00 CAD
Account
RRSP
Status
Completed
Submitted
June 18, 2026
2:14 pm
Filled
June 30, 2026
2:20 pm
Type
Limit sell
Limit price
$80.00 CAD
Entered quantity
2 shares
Filled quantity
2 shares x $80.00 CAD
Total value
$160.00 CAD
View INTC details
"""
    parsed = parse_completed_activity_detail(text, row, {"visible_text": "fixture.txt", "screenshot": "fixture.png"})
    assert parsed is not None
    assert parsed["status"] == "Completed"
    assert parsed["quantity"] == "2 shares"
    assert parsed["execution_price"] == "$80.00 CAD"
    assert parsed["execution_price_source"] == "filled_quantity"
    assert parsed["filled_date"] == "June 30, 2026"
    assert parsed["exact_fill_fields_confirmed"] is True


# --------------------------------------------------------------------------
# Regression cover for the July 26 2026 live WARN: the account page renders a
# "Stocks" holdings grid and never prints "Positions", which timed out the
# readiness wait and emptied holdings for every account.
# --------------------------------------------------------------------------


def test_stocks_layout_holdings_parse_without_the_positions_word():
    text = (FIX / "account-card-tfsa-stocks-2026.txt").read_text()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert "Positions" not in lines
    rows = parse_holdings_lines(lines, "TFSA", {"visible_text": "fixture.txt"})
    assert [row["ticker"] for row in rows] == ["CRM", "MDA.TO", "MP"]
    crm = rows[0]
    assert crm["quantity"] == "75 shares"
    assert crm["current_price"] == "$12.49 CAD"
    assert crm["market_value"] == "$936.75 CAD"
    assert crm["allocation_percent"] == "50.00%"
    # The grid's Currency column is the only currency evidence; the amounts
    # themselves carry no suffix, so a USD row must not be mislabelled CAD.
    mp = rows[2]
    assert mp["market_value"] == "$207.00 USD"
    assert mp["market_value_currency"] == "USD"
    assert rows[1]["quantity"] == "7 shares"


def test_legacy_positions_layout_still_parses():
    text = (FIX / "account-card-non-registered.txt").read_text()
    rows = parse_holdings_lines([l.strip() for l in text.splitlines() if l.strip()], "Non-registered", {})
    assert {"BAM", "BN", "F", "LUN", "MP", "SPIR", "UFO"} <= {row["ticker"] for row in rows}


def test_single_share_row_is_labelled_share_not_shares():
    row = parse_holdings_row_cells(
        ["LMT", "LMT", "CAD", "0.96%", "1", "$33.12", "$33.12", "+$0.86", "2.67%"],
        "TFSA", {},
    )
    assert row is not None and row["quantity"] == "1 share"


def test_holding_row_accepts_a_sub_cent_market_price():
    row = parse_holdings_row_cells(
        ["MAXQ", "CAD", "1.77%", "100", "$0.425", "$42.50", "+$4.00", "10.39%"],
        "TFSA", {},
    )
    assert row is not None
    assert row["ticker"] == "MAXQ"
    assert row["quantity"] == "100 shares"
    assert row["current_price"] == "$0.425 CAD"
    assert row["market_value"] == "$42.50 CAD"


def test_holding_row_strips_accessible_details_suffix_from_ticker():
    row = parse_holdings_row_cells(
        ["MP Details", "USD", "100.00%", "3", "$54.54", "$163.62", "+$70.59", "75.89%"],
        "Non-registered", {},
    )
    assert row is not None
    assert row["ticker"] == "MP"
    assert row["quantity"] == "3 shares"
    assert row["market_value"] == "$163.62 USD"


def test_account_readiness_requires_rendered_grid_and_money_values():
    ready = {
        "url": "https://my.wealthsimple.com/app/account-details/tfsa-x",
        "account_name_visible": True, "holdings_rows": 13, "holdings_toolbar": 1,
        "column_headers": ["Holdings", "Currency"], "total_cash_value": "$17,511.24 CAD",
        "available_cad_value": "$17,487.20 CAD",
    }
    assert account_page_is_ready(ready, "TFSA") is True
    # The live page paints the cash labels ~1s before their values; at that
    # moment the line after "Total cash available" is the next label.
    skeleton = dict(ready, total_cash_value="Available CAD", available_cad_value="Available USD", holdings_rows=0, holdings_toolbar=0, column_headers=[])
    assert account_page_is_ready(skeleton, "TFSA") is False
    assert account_page_is_ready(dict(ready, url="https://my.wealthsimple.com/app/activity"), "TFSA") is False
    assert account_page_is_ready(dict(ready, account_name_visible=False), "TFSA") is False
    assert account_page_is_ready(None, "TFSA") is False


def test_account_readiness_accepts_a_rendered_but_empty_portfolio():
    empty = {
        "url": "https://my.wealthsimple.com/app/account-details/rrsp-x",
        "account_name_visible": True, "holdings_rows": 0, "holdings_toolbar": 1,
        "column_headers": ["Holdings", "Currency", "Quantity"],
        "total_cash_value": "$0.00 CAD", "available_cad_value": "$0.00 CAD",
    }
    assert account_page_is_ready(empty, "RRSP") is True


def test_cash_is_not_captured_from_a_label_on_a_partly_rendered_page():
    skeleton = "TFSA\nTotal cash available\nAvailable CAD\nAvailable USD\nKeep your portfolio on track\n"
    parsed = parse_account_identity_and_balance(skeleton, "TFSA", {"url": "u"})
    assert parsed["available_to_trade"] is None
    assert parsed["available_cash_cad"] is None
    assert parsed["available_cash_usd"] is None
    assert parsed["cash_values_rendered"] is False

    rendered = "TFSA\nTotal cash available\n$17,511.24 CAD\nAvailable CAD\n$17,487.20 CAD\nAvailable USD\n$17.06 USD\n"
    ok = parse_account_identity_and_balance(rendered, "TFSA", {"url": "u"})
    assert ok["available_to_trade"] == "$17,511.24 CAD"
    assert ok["available_cash_usd"] == "$17.06 USD"
    assert ok["cash_values_rendered"] is True


def test_holdings_rows_from_dom_drop_rows_belonging_to_another_account():
    rows = [
        {"testid": "holdings-row-a", "href": "/app/security-details/sec-s-1?account=tfsa-A&selectedAccount=tfsa-A",
         "cells": ["CRM", "CRM", "CAD", "27.20%", "75", "$12.49", "$936.75", "+$37.50", "4.17%"]},
        {"testid": "holdings-row-b", "href": "/app/security-details/sec-s-2?account=rrsp-B&selectedAccount=rrsp-B",
         "cells": ["GOOG", "GOOG", "CAD", "36.25%", "7", "$51.07", "$357.49", "+$0.70", "0.20%"]},
    ]
    parsed = parse_holdings_rows_from_dom(rows, "TFSA", {})
    assert [row["ticker"] for row in parsed] == ["CRM"]
    assert parsed[0]["row_account_slug"] == "tfsa-A"
    assert parsed[0]["holdings_row_testid"] == "holdings-row-a"


def test_exchange_suffix_is_normalized_but_share_class_is_preserved():
    assert normalize_ticker_for_cross_reference("MDA.TO") == "MDA"
    assert normalize_ticker_for_cross_reference("BN.TO") == "BN"
    assert normalize_ticker_for_cross_reference("BAM.TO") == "BAM"
    assert normalize_ticker_for_cross_reference("TD.TO") == "TD"
    # `.A` is a share class, not a listing venue.
    assert normalize_ticker_for_cross_reference("CTC.A") == "CTC.A"
    assert normalize_ticker_for_cross_reference("MP") == "MP"
    assert normalize_ticker_for_cross_reference(None) is None


def test_sell_coverage_matches_suffixed_holdings_against_bare_orders():
    holdings = [{"account": "Non-registered", "ticker": "BN.TO", "quantity": "9 shares", "current_price_currency": "CAD"}]
    orders = [{"account": "Non-registered", "ticker": "BN", "side": "sell", "quantity": "9 shares"}]
    assert sell_order_coverage(holdings, orders)[0]["coverage_status"] == "uncertain"
    orders[0].update(quantity_evidence("9 shares", None, "9 shares"))
    orders[0].update(status="Pending", security_quote_currency="CAD")
    assert sell_order_coverage(holdings, orders, inventory_complete=True)[0]["coverage_status"] == "fully_covered"


def test_paired_exit_special_ticker_matching_survives_the_to_suffix():
    holdings = [{"account": "TFSA", "ticker": "MDA.TO", "quantity": "7 shares"}]
    findings = paired_exit_checks(holdings, [], [{"ticker": "TD.TO"}])
    assert {"type": "holding_without_open_sell_exit", "account": "TFSA", "ticker": "MDA.TO"} in findings
    td = [f for f in findings if f.get("ticker") == "TD"][0]
    assert td["status"] == "activity_seen"


def test_value_currency_uses_the_trailing_settlement_token():
    assert value_currency("$251.84 CAD") == "CAD"
    assert value_currency("$79.11 USD") == "USD"
    # A CAD estimate annotated with its USD quote settles in CAD.
    assert value_currency("USD quote $35.00 — estimated total $251.84 CAD") == "CAD"
    assert value_currency("$100.00") is None


def test_looks_like_money_rejects_labels_and_accepts_negatives():
    assert looks_like_money("$17,511.24 CAD") is True
    assert looks_like_money("$0.00") is True
    assert looks_like_money("−$14.21") is True
    assert looks_like_money("Available CAD") is False
    assert looks_like_money("75") is False
    assert looks_like_money(None) is False


def test_expired_usd_access_is_nontradable_during_conversion_grace():
    grace = load_user_account_context(today=date(2026, 8, 6))["usd_trading_accounts"]
    assert grace["review_required"] is False
    assert grace["expired_at_capture"] is True
    assert grace["directly_tradable_usd"] is False
    assert grace["forced_conversion_on"] == "2026-11-12"
    assert grace["days_until_forced_conversion"] == 98
    assert "cannot be used for USD trading" in grace["interpretation"]

    overdue = load_user_account_context(today=date(2026, 11, 13))["usd_trading_accounts"]
    assert overdue["review_required"] is True
    assert "Reconfirm whether the forced conversion has occurred" in overdue["interpretation"]


def test_convert_money_and_preview_are_click_guarded():
    for label in ("Convert money", "Convert", "Preview", "Preview order", "Sell all"):
        assert label in DANGEROUS_CLICK_TEXT
    assert "Preview order" in FORBIDDEN_STATE_PATTERNS
    # A control that merely reads a balance must stay clickable.
    assert "Load more" not in DANGEROUS_CLICK_TEXT
    assert "Pending" not in DANGEROUS_CLICK_TEXT


def _all_accounts_map():
    """All expected accounts present, so status reflects holdings integrity only."""
    return {
        name: {"account": name, "available_to_trade": "$100.00 CAD", "available_cash_cad": "$100.00 CAD"}
        for name in ("TFSA", "RRSP", "Non-registered")
    }


def test_blank_holdings_skip_dependent_checks_and_raise_a_warning(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    accounts_map = _all_accounts_map()
    orders = [{
        "account": "TFSA", "ticker": "CRM", "side": "sell", "side_label": "Limit sell",
        "quantity": "50 shares", "estimated_total": "$725.00 CAD", "settlement_currency": "CAD",
        "stable_row_key": "k1", "confirmation_level": "detail_confirmed",
    }]
    write_bundle(state, accounts_map, [], orders, [], [])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    integrity = manifest["holdings_integrity"]
    assert integrity["holdings_parsed"] == 0
    assert integrity["accounts_without_parsed_holdings"] == ["TFSA", "RRSP", "Non-registered"]
    assert integrity["holdings_dependent_checks_skipped"] == [
        "paired_exit_checks", "filled_buy_exit_checks", "sell_order_coverage",
    ]
    # The 44 false "open sell without visible holding" findings must not reappear.
    assert json.loads((tmp_path / "sell-order-coverage.json").read_text()) == []
    assert json.loads((tmp_path / "logs" / "account-readiness.json").read_text()) == []
    assert manifest["account_readiness_telemetry"] == "logs/account-readiness.json"
    assert any("no holdings were parsed for any captured account" in w for w in manifest["warnings"])
    assert manifest["blockers"] == []
    assert manifest["status"] == "WARN"


def test_holdings_present_keeps_dependent_checks_running(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    accounts_map = _all_accounts_map()
    holdings = [{"account": "TFSA", "ticker": "CRM", "quantity": "10 shares", "market_value": "$100.00 CAD"}]
    orders = [{
        "account": "TFSA", "ticker": "CRM", "side": "sell", "side_label": "Limit sell",
        "quantity": "50 shares", "estimated_total": "$725.00 CAD", "settlement_currency": "CAD",
        "stable_row_key": "k1", "confirmation_level": "detail_confirmed",
    }]
    write_bundle(state, accounts_map, holdings, orders, [], [])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["holdings_integrity"]["holdings_dependent_checks_skipped"] == []
    coverage = json.loads((tmp_path / "sell-order-coverage.json").read_text())
    # This legacy fixture has no remaining-quantity evidence. Running the check
    # must report uncertainty, not resurrect the old original-size oversell.
    assert [row["type"] for row in coverage] == ["coverage_uncertain"]
    assert coverage[0]["uncovered_quantity"] is None


def test_fresh_export_verified_empty_account_is_not_reported_missing(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    accounts_map = _all_accounts_map()
    holdings = [
        {"account": "TFSA", "ticker": "CRM", "quantity": "10 shares"},
        {"account": "RRSP", "ticker": "GOOG", "quantity": "7 shares"},
    ]
    exports = {
        "holdings_export": {
            "age_hours_at_capture": 0.01,
            "is_stale_at_capture": False,
        },
        "holdings_export_rows": [
            {"Account Type": "TFSA", "Security Type": "EQUITY", "Symbol": "CRM", "Quantity": "10"},
            {"Account Type": "RRSP", "Security Type": "EQUITY", "Symbol": "GOOG", "Quantity": "7"},
            {"Account Type": "Non-registered", "Security Type": "CURRENCY", "Symbol": "USD", "Quantity": "179.85"},
        ],
    }
    write_bundle(state, accounts_map, holdings, [], [], [], exports)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    integrity = manifest["holdings_integrity"]
    assert integrity["accounts_without_parsed_holdings"] == []
    assert integrity["accounts_verified_empty_by_fresh_holdings_export"] == [
        "Non-registered"
    ]
    assert not any("no holdings were parsed for" in w for w in manifest["warnings"])


def test_unknown_settlement_currency_orders_are_reported_not_silently_bucketed():
    accounts = [{"account": "TFSA", "available_to_trade": "$100.00 CAD"}]
    orders = [{"account": "TFSA", "side": "buy", "estimated_total": "$50.00", "stable_row_key": "no-currency"}]
    reserve = compute_cash_reserve(accounts, orders)["TFSA"]
    assert reserve["orders_with_unknown_settlement_currency"] == ["no-currency"]
    assert reserve["sum_estimated_open_buy_costs_by_currency"] == {"UNKNOWN": 50.0}


# --------------------------------------------------------------------------
# Final-review hardening (2026-07-26 10:43 bundle).
# --------------------------------------------------------------------------


def test_submitted_timestamp_split_survives_the_non_breaking_space():
    # Wealthsimple writes "12:08 pm"; the old rsplit(" ", 2) moved the year
    # out of the date, producing ("July 2,", "2026 12:08 pm") for all orders.
    assert split_timestamp_value("July 2, 2026 12:08 pm") == ("July 2, 2026", "12:08 pm")
    assert split_timestamp_value("October 7, 2026 5:00 pm") == ("October 7, 2026", "5:00 pm")
    assert split_timestamp_value("July 2, 2026") == ("July 2, 2026", None)
    assert split_timestamp_value(None) == (None, None)


def test_order_detail_submitted_date_keeps_its_year():
    text = """
Account
TFSA
Status
Pending
Submitted
July 2, 2026
12:08 pm
Expires
September 29, 2026
5:00 pm
Type
Limit sell
Limit price
$20.00 CAD
Entered quantity
12 shares
Estimated total value
$240.00 CAD
View AVGO details
"""
    detail = parse_order_detail_blocks(text, {"url": "fixture"})[0]
    assert detail["submitted_date"] == "July 2, 2026"
    assert detail["submitted_time"] == "12:08 pm"
    assert detail["expiry"] == "September 29, 2026 5:00 pm"


def _readiness(**over):
    base = {
        "url": "https://my.wealthsimple.com/app/account-details/tfsa-x",
        "account_name_visible": True, "holdings_rows": 13, "holdings_toolbar": 1,
        "column_headers": ["Holdings", "Currency"], "total_cash_value": "$17,511.24 CAD",
        "available_cad_value": "$17,487.20 CAD",
    }
    base.update(over)
    return base


def test_readiness_requires_a_stable_empty_grid_before_accepting_zero_rows():
    painted = _readiness()
    assert account_page_is_ready(painted, "TFSA", allow_empty_grid=False) is True
    assert account_page_is_ready(painted, "TFSA", allow_empty_grid=True) is True
    # The 10:43 run recorded holdings_rows=0 for TFSA while 13 rows existed a
    # moment later: a toolbar can precede its rows, so the strict pass must
    # reject a rowless grid.
    rowless = _readiness(holdings_rows=0)
    assert account_page_is_ready(rowless, "TFSA", allow_empty_grid=False) is False
    assert account_page_is_ready(rowless, "TFSA", allow_empty_grid=True) is True


def test_readiness_accepts_cash_only_layout_when_fresh_export_proves_empty():
    cash_only = _readiness(
        holdings_rows=0,
        holdings_toolbar=0,
        column_headers=[],
        verified_empty_by_fresh_holdings_export=True,
    )
    assert account_page_is_ready(cash_only, "TFSA", allow_empty_grid=False) is False
    assert account_page_is_ready(cash_only, "TFSA", allow_empty_grid=True) is True
    assert account_page_readiness_failures(
        cash_only, "TFSA", allow_empty_grid=True
    ) == []


def test_readiness_rejects_cash_only_layout_without_independent_control():
    unverified = _readiness(
        holdings_rows=0,
        holdings_toolbar=0,
        column_headers=[],
        verified_empty_by_fresh_holdings_export=False,
    )
    assert account_page_is_ready(unverified, "TFSA", allow_empty_grid=True) is False
    assert account_page_readiness_failures(
        unverified, "TFSA", allow_empty_grid=True
    ) == ["holdings_grid_not_rendered"]


def test_readiness_accepts_cad_only_when_zero_usd_is_omitted():
    rrsp = _readiness(total_cash_value=None, available_usd_value=None)
    assert account_page_is_ready(rrsp, "RRSP", allow_empty_grid=False) is True
    assert account_page_readiness_failures(rrsp, "RRSP", allow_empty_grid=False) == []


def test_account_wait_rejects_a_positive_grid_that_is_still_streaming(
    tmp_path
):
    import src.full_account_inventory as inventory

    class FakeTime:
        def __init__(self):
            self.now = 0.0

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    counts = iter([4, 4, 5, 5, 5, 5])
    calls = []
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = RunState(out_dir=tmp_path, mode="FULL")
    reader.close_support_chat = lambda: None
    reader.clear_blocking_modal = lambda: True

    def readiness(_account):
        count = next(counts)
        calls.append(count)
        return _readiness(holdings_rows=count)

    reader.account_page_readiness = readiness
    original_time = inventory.time
    try:
        inventory.time = FakeTime()
        result = reader.wait_for_account_content("TFSA", timeout=10)
    finally:
        inventory.time = original_time

    assert calls == [4, 4, 5, 5, 5, 5]
    assert result["holdings_rows"] == 5
    assert result["holdings_rows_stable_confirmations"] == 4


def test_preload_rejects_readiness_returned_only_by_timeout():
    from src.full_account_inventory import (
        EMPTY_HOLDINGS_GRID_CONFIRMATIONS,
        HOLDINGS_GRID_STABLE_CONFIRMATIONS,
        preload_readiness_is_stable,
    )

    assert preload_readiness_is_stable({
        "holdings_rows": 5,
        "holdings_rows_stable_confirmations":
            HOLDINGS_GRID_STABLE_CONFIRMATIONS,
    })
    assert not preload_readiness_is_stable({"holdings_rows": 5})
    assert not preload_readiness_is_stable({
        "holdings_rows": 5,
        "holdings_rows_stable_confirmations": 2,
    })
    assert preload_readiness_is_stable({
        "holdings_rows": 0,
        "holdings_rows_stable_confirmations":
            EMPTY_HOLDINGS_GRID_CONFIRMATIONS,
    })
    assert not preload_readiness_is_stable({"holdings_rows": 0})


def test_special_attention_flags_an_open_order_when_activity_is_silent():
    orders = [{"account": "TFSA", "ticker": "TD.TO", "side": "buy", "quantity": "1 share",
               "estimated_total": "$145.00 CAD"}]
    findings = paired_exit_checks([], orders, [])
    td = [f for f in findings if f.get("ticker") == "TD"][0]
    assert td["status"] == "no_activity_but_open_order_pending"
    assert td["open_orders"] == [{"account": "TFSA", "ticker": "TD.TO", "side": "buy",
                                 "quantity": "1 share", "estimated_total": "$145.00 CAD"}]
    cvs = [f for f in findings if f.get("ticker") == "CVS"][0]
    assert cvs["status"] == "not_seen_in_captured_activity"
    assert cvs["open_orders"] == []
    seen = paired_exit_checks([], [], [{"ticker": "QCOM"}])
    assert [f for f in seen if f.get("ticker") == "QCOM"][0]["status"] == "activity_seen"


def test_handoff_usd_line_carries_evidence_basis_and_expiry_window():
    summary = {
        "accounts": [], "holdings": [], "open_orders": [], "paired_exit_checks": [],
        "duplicate_checks": [], "filled_buy_exit_checks": [], "recent_activity": [],
        "activity_status_counts": {}, "cash_reserve_reconciliation": {}, "warnings": [], "blockers": [],
    }
    manifest = {
        "zip_path": "/tmp/t.zip", "status": "WARN", "accounts_seen": [],
        "user_account_context": {"source": "user_reported", "usd_trading_accounts": {
            "status": "active_trial_user_reported", "trial_expires": "2026-08-04",
            "evidence_basis": "user_reported_not_verified_in_app_by_this_capture",
            "days_until_expiry": 9, "review_required": False,
            "interpretation": "A USD balance alone does not prove USD trading-account access.",
        }},
    }
    text = render_next_message(manifest, summary)
    assert "evidence basis user_reported_not_verified_in_app_by_this_capture" in text
    assert "9 days until stated expiry" in text


def test_account_total_reconciles_when_components_explain_it():
    account = {
        "account": "RRSP", "total_account_value": "$4,635.19",
        "available_to_trade": "$2,126.62 CAD", "available_cash_cad": "$2,126.62 CAD",
        "available_cash_usd": "$0.00 USD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    holdings = [{"account": "RRSP", "market_value": "$986.07 CAD", "market_value_currency": "CAD"}]
    orders = [{"account": "RRSP", "side": "buy", "estimated_total": "$1,522.50 CAD", "settlement_currency": "CAD"}]
    result = reconcile_total_account_value(account, holdings, orders)
    assert result["status"] == "reconciled"
    assert result["residual_cad"] == 0.0
    assert result["components_total_cad"] == 4635.19


def test_all_cad_account_reconciles_without_an_fx_rate():
    account = {
        "account": "RRSP", "total_account_value": "$4,714.27",
        "available_cash_cad": "$1,841.97 CAD",
        "total_cash_available_semantics": {},
    }
    holdings = [{
        "account": "RRSP", "market_value": "$949.80 CAD",
        "market_value_currency": "CAD",
    }]
    orders = [{
        "account": "RRSP", "side": "buy", "estimated_total": "$1,922.50 CAD",
        "settlement_currency": "CAD",
    }]

    result = reconcile_total_account_value(account, holdings, orders)

    assert result["status"] == "reconciled"
    assert result["reference_fx_rate"] is None
    assert result["components_total_cad"] == 4714.27


def test_account_total_residual_is_reported_not_absorbed():
    account = {
        "account": "TFSA", "total_account_value": "$30,278.26",
        "available_to_trade": "$17,511.24 CAD", "available_cash_cad": "$17,487.20 CAD",
        "available_cash_usd": "$17.06 USD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    holdings = [
        {"account": "TFSA", "market_value": "$3,061.89 CAD", "market_value_currency": "CAD"},
        {"account": "TFSA", "market_value": "$270.51 USD", "market_value_currency": "USD"},
    ]
    orders = [
        {"account": "TFSA", "side": "buy", "estimated_total": "$9,054.60 CAD", "settlement_currency": "CAD"},
        {"account": "TFSA", "side": "buy", "estimated_total": "$95.02 USD", "settlement_currency": "USD"},
    ]
    result = reconcile_total_account_value(account, holdings, orders)
    assert result["status"] == "residual_unexplained"
    assert result["residual_cad"] == 135.35
    # The note must not speculate about a cause, and must not call it a fee.
    assert "does not attribute it to a fee" in result["note"]
    assert "unsettled cash" not in result["note"]


def test_account_total_reconciliation_warns_and_is_written(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    accounts_map = {
        name: {
            "account": name, "total_account_value": "$1,000.00",
            "available_to_trade": "$100.00 CAD", "available_cash_cad": "$100.00 CAD",
            "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
        } for name in ("TFSA", "RRSP", "Non-registered")
    }
    holdings = [{"account": "TFSA", "ticker": "CRM", "quantity": "10 shares",
                 "market_value": "$100.00 CAD", "market_value_currency": "CAD"}]
    write_bundle(state, accounts_map, holdings, [], [], [])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    recon = manifest["account_total_reconciliation"]
    assert recon["TFSA"]["status"] == "residual_unexplained"
    assert recon["TFSA"]["residual_cad"] == 800.0
    assert json.loads((tmp_path / "account-total-reconciliation.json").read_text())["TFSA"]["residual_cad"] == 800.0
    assert any("account total not fully explained" in w for w in manifest["warnings"])


def test_account_total_not_reconcilable_without_a_reference_rate():
    account = {"account": "RRSP", "total_account_value": "$100.00", "available_to_trade": "$100.00 CAD",
               "total_cash_available_semantics": {}}
    holdings = [{
        "account": "RRSP", "market_value": "$1.00 USD",
        "market_value_currency": "USD",
    }]
    result = reconcile_total_account_value(account, holdings, [])
    assert result["status"] == "not_reconcilable_without_reference_fx_rate"


def test_authenticated_cad_position_value_reconciles_without_fx_rate():
    account = {
        "account": "TFSA",
        "total_account_value": "$2,046.43",
        "available_cash_cad": "$0.00 CAD",
        "total_cash_available_semantics": {},
        "authenticated_position_valuations_cad": [{
            "ticker": "MP",
            "quantity": "1",
            "market_value_cad": "75.51",
            "source": "authenticated GraphQL CAD valuation",
            "captured_at": "2026-08-11T12:00:00-03:00",
        }],
    }
    holdings = [
        {
            "account": "TFSA", "ticker": "MSFT",
            "market_value": "$1,970.92 CAD", "market_value_currency": "CAD",
        },
        {
            "account": "TFSA", "ticker": "MP",
            "market_value": "$54.20 USD", "market_value_currency": "USD",
        },
    ]

    result = reconcile_total_account_value(account, holdings, [])

    assert result["status"] == "reconciled"
    assert result["reference_fx_rate"] is None
    assert result["foreign_holdings_direct_cad_total"] == 75.51
    assert result["components_total_cad"] == 2046.43
    assert result["authenticated_foreign_position_valuations_cad"][0]["ticker"] == "MP"


def test_convert_money_visibility_is_recorded_in_the_safety_log():
    reader = WealthsimpleReader.__new__(WealthsimpleReader)  # no browser needed for scan_state
    reader.state = RunState(out_dir=Path("/tmp"), mode="FULL")
    reader.scan_state("Add money\nTransfer money\nConvert money\nTotal cash available\n",
                      strict=False, evidence="fixture.txt")
    seen = {e.get("control") for e in reader.state.safety_log if e["event"] == "dangerous_control_visible"}
    assert "Convert money" in seen
    assert reader.state.blockers == []


def test_total_cash_tooltip_exposure_predicate():
    assert total_cash_tooltip_is_exposed(
        "This is a combination of your CAD and USD balances. Current rate: 1.4094") is True
    assert total_cash_tooltip_is_exposed("Total cash available\n$2,126.62 CAD") is False
    assert total_cash_tooltip_is_exposed(None) is False


# --------------------------------------------------------------------------
# TFSA C$135.35 residual RCA: an FX spread cannot explain it, and the bundle
# must not imply that it can.
# --------------------------------------------------------------------------


def _tfsa_residual_case():
    account = {
        "account": "TFSA", "total_account_value": "$30,278.26",
        "available_to_trade": "$17,511.24 CAD", "available_cash_cad": "$17,487.20 CAD",
        "available_cash_usd": "$17.06 USD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    holdings = [
        {"account": "TFSA", "market_value": "$3,061.89 CAD", "market_value_currency": "CAD"},
        {"account": "TFSA", "market_value": "$270.51 USD", "market_value_currency": "USD"},
    ]
    orders = [
        {"account": "TFSA", "side": "buy", "estimated_total": "$9,054.60 CAD", "settlement_currency": "CAD"},
        {"account": "TFSA", "side": "buy", "estimated_total": "$95.02 USD", "settlement_currency": "USD"},
    ]
    return account, holdings, orders


def test_residual_is_not_attributed_to_an_fx_spread():
    result = reconcile_total_account_value(*_tfsa_residual_case())
    analysis = result["residual_analysis"]
    assert analysis["classification"] == "unexplained_not_fx_spread"
    # US$95.02 of holds at 1.4094 is C$133.92; the documented 1.5% ceiling is C$2.01.
    assert analysis["foreign_currency_open_buy_holds_cad"] == 133.92
    assert analysis["max_fx_spread_could_explain_cad"] == 2.01
    assert analysis["residual_after_max_fx_spread_cad"] == 133.34
    # Closing the gap on those holds would need ~2.83 CAD/USD, about 2x the reference.
    assert analysis["implied_rate_to_close_on_foreign_holds"] == 2.83385
    assert analysis["implied_rate_multiple_of_reference"] == 2.0107
    assert analysis["residual_percent_of_account_total"] == 0.447
    assert "only at fill" in analysis["note"]


def test_residual_inside_the_documented_spread_bound_is_labelled_as_such():
    account = {
        "account": "TFSA", "total_account_value": "$1,100.30",
        "available_to_trade": "$100.00 CAD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    # US$700 of holds = C$986.58; 1.5% of that is C$14.80, and the residual is C$13.72.
    orders = [{"account": "TFSA", "side": "buy", "estimated_total": "$700.00 USD", "settlement_currency": "USD"}]
    result = reconcile_total_account_value(account, [], orders)
    analysis = result["residual_analysis"]
    assert analysis["classification"] == "within_documented_fx_spread_bound"
    assert analysis["max_fx_spread_could_explain_cad"] == 14.80
    assert "implied_rate_to_close_on_foreign_holds" not in analysis


def test_residual_without_foreign_holds_cannot_be_an_fx_spread():
    account = {
        "account": "RRSP", "total_account_value": "$1,000.00", "available_to_trade": "$100.00 CAD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    result = reconcile_total_account_value(account, [], [])
    analysis = result["residual_analysis"]
    assert analysis["classification"] == "unexplained_no_foreign_currency_holds"
    assert analysis["max_fx_spread_could_explain_cad"] == 0.0
    assert "cannot explain" in analysis["note"]


def test_reconciled_account_is_classified_reconciled():
    account = {
        "account": "RRSP", "total_account_value": "$4,635.19", "available_to_trade": "$2,126.62 CAD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    holdings = [{"account": "RRSP", "market_value": "$986.07 CAD", "market_value_currency": "CAD"}]
    orders = [{"account": "RRSP", "side": "buy", "estimated_total": "$1,522.50 CAD", "settlement_currency": "CAD"}]
    result = reconcile_total_account_value(account, holdings, orders)
    assert result["status"] == "reconciled"
    assert result["residual_analysis"]["classification"] == "reconciled"


def test_settled_cash_breakdown_is_recorded_as_not_displayed():
    parsed = parse_account_identity_and_balance(
        "TFSA\nTotal cash available\n$17,511.24 CAD\nAvailable CAD\n$17,487.20 CAD\n", "TFSA", {"url": "u"})
    assert parsed["settled_cash"] is None
    assert parsed["settled_unsettled_cash_breakdown"] == "not_displayed_by_wealthsimple"


def test_residual_warning_states_materiality_and_classification(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    account, holdings, orders = _tfsa_residual_case()
    accounts_map = {"TFSA": account}
    for name in ("RRSP", "Non-registered"):
        accounts_map[name] = {"account": name, "total_account_value": "$0.00",
                              "available_to_trade": "$0.00 CAD",
                              "total_cash_available_semantics": {"reference_fx_rate": "1.4094"}}
    write_bundle(state, accounts_map, holdings, orders, [], [])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    warning = next(w for w in manifest["warnings"] if "account total not fully explained" in w)
    assert "0.447% of the account" in warning
    assert "unexplained_not_fx_spread" in warning
    assert "fee" not in warning


# --------------------------------------------------------------------------
# Browser follow-up (2026-07-28). Live re-check showed the same model and the
# same US$95.02 USD-settled reserve reconciling to C$-0.28 on a trading day,
# against C$+135.35 on the Sunday capture; and it showed only ONE July 23 CRM
# fill where the Sunday bundle carried two.
# --------------------------------------------------------------------------


def test_one_fill_rendered_twice_is_reported_not_merged():
    fill = {
        "account": "TFSA", "ticker": "CRM", "activity_type": "Limit buy", "status": "Completed",
        "quantity": "75 shares", "execution_price": "$12.00 CAD", "date": "July 23, 2026", "time": "11:41 am",
    }
    activity = [
        {**fill, "stable_row_key": "a", "source_control_id": "ac250ef6"},
        {**fill, "stable_row_key": "b", "source_control_id": "52311a16"},
    ]
    findings = duplicate_activity_events(activity)
    assert len(findings) == 1
    assert findings[0]["type"] == "activity_event_reported_more_than_once"
    assert findings[0]["ticker"] == "CRM"
    assert findings[0]["row_count"] == 2
    assert findings[0]["stable_row_keys"] == ["a", "b"]
    assert findings[0]["source_control_ids"] == ["52311a16", "ac250ef6"]


def test_genuinely_distinct_fills_are_not_flagged_as_duplicates():
    base = {"account": "TFSA", "ticker": "SPCX", "activity_type": "Limit buy", "status": "Completed",
            "quantity": "5 shares", "execution_price": "$20.00 CAD", "date": "July 22, 2026"}
    # same security and size, different fill times -> two real fills
    activity = [
        {**base, "time": "10:31 am", "stable_row_key": "a"},
        {**base, "time": "2:15 pm", "stable_row_key": "b"},
    ]
    assert duplicate_activity_events(activity) == []
    # a pending row is not a settled event
    assert duplicate_activity_events([
        {**base, "time": "10:31 am", "stable_row_key": "a", "status": "Pending"},
        {**base, "time": "10:31 am", "stable_row_key": "b", "status": "Pending"},
    ]) == []


def test_duplicate_activity_events_reach_the_bundle_and_warn(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    fill = {"account": "TFSA", "ticker": "CRM", "activity_type": "Limit buy", "status": "Completed",
            "quantity": "75 shares", "execution_price": "$12.00 CAD", "date": "July 23, 2026", "time": "11:41 am"}
    activity = [{**fill, "stable_row_key": "a"}, {**fill, "stable_row_key": "b"}]
    accounts_map = {name: {"account": name, "available_to_trade": "$0.00 CAD"}
                    for name in ("TFSA", "RRSP", "Non-registered")}
    write_bundle(state, accounts_map, [], [], [], activity)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["duplicate_activity_events"]) == 1
    assert json.loads((tmp_path / "duplicate-activity-events.json").read_text())[0]["ticker"] == "CRM"
    assert any("appear more than once" in w for w in manifest["warnings"])


def test_weekend_capture_is_recorded_as_context_not_a_downgrade():
    account = {
        "account": "TFSA", "total_account_value": "$30,278.26", "available_to_trade": "$17,511.24 CAD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4094"},
    }
    holdings = [
        {"account": "TFSA", "market_value": "$3,061.89 CAD", "market_value_currency": "CAD"},
        {"account": "TFSA", "market_value": "$270.51 USD", "market_value_currency": "USD"},
    ]
    orders = [
        {"account": "TFSA", "side": "buy", "estimated_total": "$9,054.60 CAD", "settlement_currency": "CAD"},
        {"account": "TFSA", "side": "buy", "estimated_total": "$95.02 USD", "settlement_currency": "USD"},
    ]
    # A weekend capture is context, never a reason to downgrade the finding:
    # the residual that prompted the earlier downgrade turned out to be a
    # C$135.00 pending SHOP.TO buy dropped by the ticker parser.
    sunday = reconcile_total_account_value(account, holdings, orders, capture_date=date(2026, 7, 26))
    assert sunday["residual_cad"] == 135.35
    assert sunday["residual_analysis"]["classification"] == "unexplained_not_fx_spread"
    assert sunday["residual_analysis"]["capture_weekday"] == "Sunday"
    assert sunday["residual_analysis"]["capture_on_non_trading_day"] is True
    assert sunday["residual_analysis"]["max_fx_spread_could_explain_cad"] == 2.01

    tuesday = reconcile_total_account_value(account, holdings, orders, capture_date=date(2026, 7, 28))
    assert tuesday["residual_analysis"]["classification"] == "unexplained_not_fx_spread"
    assert tuesday["residual_analysis"]["capture_on_non_trading_day"] is False


def test_is_non_trading_day_covers_the_weekend_only():
    assert is_non_trading_day(date(2026, 7, 25)) is True   # Saturday
    assert is_non_trading_day(date(2026, 7, 26)) is True   # Sunday
    assert is_non_trading_day(date(2026, 7, 27)) is False  # Monday
    assert is_non_trading_day(None) is False


# --------------------------------------------------------------------------
# Export gap analysis (2026-07-28). The exported Trade/positions CSVs exposed a
# C$135.00 pending SHOP.TO buy that never reached any bundle, and a
# Non-registered cash figure the reconciler had been treating as zero.
# --------------------------------------------------------------------------


def test_exchange_suffixed_ticker_longer_than_six_characters_is_not_dropped():
    assert looks_like_ticker("SHOP.TO") is True      # 7 chars: the dropped case
    assert looks_like_ticker("SHOP") is True
    assert looks_like_ticker("MDA.TO") is True
    assert looks_like_ticker("CTC.A") is True        # share class, still fine
    assert looks_like_ticker("BRK.B") is True
    assert looks_like_ticker("CAD") is False
    assert looks_like_ticker("USD") is False
    assert looks_like_ticker("Pending") is False
    row = parse_order_row_text("SHOP.TO\nLimit buy\nTFSA\n$135.00 CAD\nPending")
    assert row is not None
    assert row["ticker"] == "SHOP.TO"
    assert row["estimated_total"] == "$135.00 CAD"
    assert normalize_ticker_for_cross_reference("SHOP.TO") == "SHOP"


def test_a_pending_card_that_will_not_parse_is_reported_not_ignored():
    controls = [
        {"text": "SHOP.TO\nLimit buy\nTFSA\n$135.00 CAD\nPending"},
        {"text": "¡BAD\nLimit buy\nTFSA\n$99.00 CAD\nPending"},   # unparsable symbol
        {"text": "NOC\nDividend\nTFSA\n$2.50 CAD\nPending"},          # not an order: ignored
    ]
    missed = unparsed_pending_controls(controls)
    assert len(missed) == 1
    assert "BAD" in missed[0]
    assert all("SHOP.TO" not in m for m in missed)


def test_a_terminal_card_that_will_not_parse_is_reported_not_ignored():
    controls = [
        {"text": "Withdrawal\nElectronic funds transfer\nTFSA\nCancelled\n− $1,234.56 CAD"},
        {"text": "Unknown final event\nCancelled\n$10.00 CAD"},
        {"text": "Withdrawal\nElectronic funds transfer\nTFSA\n− $1,234.56 CAD"},
    ]
    missed = unparsed_terminal_controls(controls)
    assert len(missed) == 1
    assert "Unknown final event" in missed[0]


def test_cash_that_was_never_captured_is_not_treated_as_zero():
    account = {
        "account": "Non-registered", "total_account_value": "$1,764.17",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4089"},
    }
    holdings = [{"account": "Non-registered", "market_value": "$950.23 CAD", "market_value_currency": "CAD"}]
    result = reconcile_total_account_value(account, holdings, [])
    assert result["status"] == "not_reconcilable_cash_not_captured"
    assert "residual_cad" not in result
    assert "not treated as zero" in result["note"]


def test_cash_aggregate_is_derived_from_native_balances_when_the_label_is_interrupted():
    # Live 2026-07-28: the Non-registered page rendered "Boost with margin"
    # between the Total cash available label and its amount, so the aggregate
    # was never captured while both native balances were.
    account = {
        "account": "Non-registered", "total_account_value": "$1,764.17",
        "available_cash_cad": "$64.36 CAD", "available_cash_usd": "$0.01 USD",
        "total_cash_available_semantics": {"reference_fx_rate": "1.4089"},
    }
    holdings = [
        {"account": "Non-registered", "market_value": "$950.23 CAD", "market_value_currency": "CAD"},
        {"account": "Non-registered", "market_value": "$533.48 USD", "market_value_currency": "USD"},
    ]
    result = reconcile_total_account_value(account, holdings, [])
    assert result["total_cash_available_source"] == "derived_from_displayed_native_balances"
    assert result["total_cash_available_cad_aggregate"] == 64.37
    # The zero substitution had reported a false C$62.32 unexplained residual.
    assert result["residual_cad"] == -2.05
    # Small, with every live component captured: graded rather than alarming.
    assert result["status"] == "minor_snapshot_residual"


def test_readiness_accepts_the_cash_block_when_the_aggregate_label_is_interrupted():
    base = {
        "url": "https://my.wealthsimple.com/app/account-details/non-registered-x",
        "account_name_visible": True, "holdings_rows": 6, "holdings_toolbar": 1,
        "column_headers": ["Holdings", "Currency"],
    }
    interrupted = dict(base, total_cash_value="Boost with margin",
                       available_cad_value="$64.36 CAD", available_usd_value="$0.01 USD")
    assert account_page_is_ready(interrupted, "Non-registered") is True
    # A genuinely unrendered cash block is still rejected.
    skeleton = dict(base, total_cash_value="Available CAD",
                    available_cad_value="Available USD", available_usd_value=None)
    assert account_page_is_ready(skeleton, "Non-registered") is False


# --------------------------------------------------------------------------
# Export importer. Exports are canonical for completed activity and holdings;
# the browser stays canonical for live Pending orders and native cash.
# --------------------------------------------------------------------------

ACTIVITY_CSV = (
    "transaction_date,settlement_date,account_id,account_type,activity_type,activity_sub_type,"
    "description,direction,symbol,name,currency,quantity,unit_price,commission,net_cash_amount\n"
    "2026-07-24,2026-07-24,ACCT1,TFSA,Trade,BUY,desc,LONG,CRM,Salesforce,CAD,75,12,0,-900\n"
    "2026-07-28,2026-07-28,ACCT1,TFSA,Trade,SELL,desc,LONG,CRM,Salesforce,CAD,-25,13.25,0,331.25\n"
    "2026-07-08,2026-07-08,ACCT1,TFSA,FxExchange,-,Convert USD,,,,USD,65.41,,,65.41\n"
    "2026-07-15,2026-07-15,ACCT2,Non-registered,Interest,-,Stock lending,,,,USD,0.01,,,0.01\n"
    "\n"
    '"As of 2026-07-28 12:45 GMT-03:00"\n'
)


def test_export_footer_is_read_as_an_as_of_stamp_not_a_transaction(tmp_path):
    path = tmp_path / "activities-export-2026-07-28.csv"
    path.write_text(ACTIVITY_CSV, encoding="utf-8")
    rows, as_of = read_wealthsimple_export(path)
    assert len(rows) == 4
    assert as_of == "2026-07-28 12:45 GMT-03:00"
    assert all(row["transaction_date"].startswith("2026-") for row in rows)


def test_normalized_export_rows_drop_account_identifiers(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text(ACTIVITY_CSV, encoding="utf-8")
    rows, _ = read_wealthsimple_export(path)
    normalized = normalize_export_rows(rows, ACTIVITY_EXPORT_FIELDS)
    assert "account_id" not in normalized[0]
    assert "description" not in normalized[0]
    assert normalized[0]["account_type"] == "TFSA"


def test_export_trade_fills_takes_only_buy_and_sell_trades(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text(ACTIVITY_CSV, encoding="utf-8")
    rows, _ = read_wealthsimple_export(path)
    fills = export_trade_fills(normalize_export_rows(rows, ACTIVITY_EXPORT_FIELDS))
    assert [(f["symbol"], f["side"], f["quantity"], f["unit_price"]) for f in fills] == [
        ("CRM", "buy", 75.0, 12.0), ("CRM", "sell", 25.0, 13.25),
    ]  # FxExchange and Interest are not fills; a sell's negative quantity is absolute


def _browser_duplicate_group():
    group = {
        "type": "activity_event_reported_more_than_once", "account": "TFSA", "ticker": "CRM",
        "activity_type": "Limit buy", "quantity": "75 shares", "execution_price": "$12.00 CAD",
        "filled_date": "July 23, 2026", "time": "11:41 am", "row_count": 2,
        "stable_row_keys": ["a", "b"],
    }
    activity = [
        {"stable_row_key": "a", "account": "TFSA", "ticker": "CRM", "filled_date": "July 23, 2026"},
        {"stable_row_key": "b", "account": "TFSA", "ticker": "CRM", "filled_date": "July 23, 2026"},
    ]
    return group, activity


def test_export_correlated_browser_duplicate_is_labelled_not_dropped():
    group, activity = _browser_duplicate_group()
    # One export fill, booked the next day by the 24-hour session.
    fills = [{"account_type": "TFSA", "symbol": "CRM", "side": "buy", "quantity": 75.0,
              "unit_price": 12.0, "currency": "CAD", "transaction_date": "2026-07-24"}]
    resolved = correlate_duplicate_fills_with_export([group], activity, fills)
    assert resolved[0]["resolution"] == "export_correlated_browser_duplicate"
    assert resolved[0]["export_matching_fills"] == 1
    assert resolved[0]["browser_rows_not_supported_by_export"] == 1
    assert resolved[0]["export_transaction_dates"] == ["2026-07-24"]
    # both browser rows survive; only the surplus is excluded from counts
    assert [row["stable_row_key"] for row in activity] == ["a", "b"]
    assert "fill_count_status" not in activity[0]
    assert activity[1]["fill_count_status"] == "export_correlated_browser_duplicate"
    assert "excluded from aggregate fill counts" in activity[1]["uncertainty_notes"][0]


def test_duplicate_group_fully_supported_by_the_export_stays_counted():
    group, activity = _browser_duplicate_group()
    fills = [
        {"account_type": "TFSA", "symbol": "CRM", "side": "buy", "quantity": 75.0,
         "unit_price": 12.0, "currency": "CAD", "transaction_date": "2026-07-23"},
        {"account_type": "TFSA", "symbol": "CRM", "side": "buy", "quantity": 75.0,
         "unit_price": 12.0, "currency": "CAD", "transaction_date": "2026-07-24"},
    ]
    resolved = correlate_duplicate_fills_with_export([group], activity, fills)
    assert resolved[0]["resolution"] == "export_confirms_every_browser_row"
    assert resolved[0]["browser_rows_not_supported_by_export"] == 0
    assert all("fill_count_status" not in row for row in activity)


def test_duplicate_with_no_export_match_is_kept_for_manual_review():
    group, activity = _browser_duplicate_group()
    far_away = [{"account_type": "TFSA", "symbol": "CRM", "side": "buy", "quantity": 75.0,
                 "unit_price": 12.0, "currency": "CAD", "transaction_date": "2026-06-18"}]
    resolved = correlate_duplicate_fills_with_export([group], activity, far_away)
    assert resolved[0]["resolution"] == "no_matching_export_fill_review_manually"
    assert resolved[0]["export_matching_fills"] == 0
    assert all("fill_count_status" not in row for row in activity)


def test_reconciliation_status_grades_residuals_by_evidence():
    fx = {"classification": "within_documented_fx_spread_bound"}
    plain = {"classification": "unexplained_not_fx_spread"}
    assert reconciliation_status(-0.54, 4646.16, plain, True) == "reconciled"
    assert reconciliation_status(-1.08, 30411.52, fx, True) == "within_documented_fx_bound"
    assert reconciliation_status(-2.89, 1764.17, plain, True) == "minor_snapshot_residual"
    # the same small residual is NOT excused when a component was not captured
    assert reconciliation_status(-2.89, 1764.17, plain, False) == "residual_unexplained"
    # material residuals stay findings whatever the evidence
    assert reconciliation_status(138.04, 30411.52, plain, True) == "residual_unexplained"
    # 0.25% of a large account is the wider of the two thresholds
    assert reconciliation_status(60.0, 100000.0, plain, True) == "minor_snapshot_residual"


def test_import_exports_copies_files_and_records_provenance(tmp_path):
    ensure_dirs(tmp_path)
    source = tmp_path / "activities-export-2026-07-28.csv"
    source.write_text(ACTIVITY_CSV, encoding="utf-8")
    state = RunState(out_dir=tmp_path, mode="FULL")
    # This test exercises copying/provenance, not wall-clock freshness. Keep
    # its fixed July 2026 fixture fresh as real time advances.
    with patch("src.full_account_inventory.export_age_hours", return_value=1.0):
        provenance = import_exports(state, source, None)
    entry = provenance["activity_export"]
    assert entry["source_basename"] == "activities-export-2026-07-28.csv"
    assert (tmp_path / "raw-exports" / source.name).is_file()
    assert entry["as_of"] == "2026-07-28 12:45 GMT-03:00"
    assert entry["row_count"] == 4
    assert len(entry["sha256"]) == 64
    assert "account_id" not in provenance["activity_export_rows"][0]
    assert "holdings_export" not in provenance
    assert state.warnings == []


def test_activity_export_analysis_keeps_exact_settled_fills_and_ancillary_rows():
    rows = [
        {"transaction_date": "2026-07-24", "account_type": "TFSA", "activity_type": "Trade", "activity_sub_type": "BUY", "symbol": "GOOG", "currency": "CAD", "quantity": "10", "unit_price": "51.57", "net_cash_amount": "-515.70"},
        {"transaction_date": "2026-07-15", "account_type": "TFSA", "activity_type": "Interest", "activity_sub_type": "-", "symbol": "", "currency": "CAD", "quantity": "0.01", "unit_price": "", "net_cash_amount": "0.01"},
        {"transaction_date": "2025-12-31", "account_type": "TFSA", "activity_type": "Dividend", "activity_sub_type": "-", "symbol": "GOOG", "currency": "CAD", "quantity": "0.05", "unit_price": "", "net_cash_amount": "0.05"},
    ]
    analysis = analyze_activity_export(rows, year=2026)
    assert analysis["current_year_rows"] == 2
    assert analysis["current_year_rows_by_type"] == {"Interest": 1, "Trade": 1}
    assert analysis["current_year_trade_fills"][0]["symbol"] == "GOOG"
    rendered = render_activity_export_analysis_md(analysis)
    assert "GOOG" in rendered
    assert "Interest" in rendered


def test_fresh_activity_export_normalizes_settled_rows_for_the_fast_path():
    rows = [
        {"transaction_date": "2026-07-24", "account_type": "TFSA", "activity_type": "Trade", "activity_sub_type": "BUY", "symbol": "GOOG", "currency": "CAD", "quantity": "10", "unit_price": "51.57", "net_cash_amount": "-515.70"},
        {"transaction_date": "2026-07-15", "account_type": "TFSA", "activity_type": "Interest", "activity_sub_type": "-", "symbol": "", "currency": "CAD", "quantity": "0.01", "unit_price": "", "net_cash_amount": "0.01"},
        {"transaction_date": "2025-12-31", "account_type": "TFSA", "activity_type": "Dividend", "activity_sub_type": "-", "symbol": "GOOG", "currency": "CAD", "quantity": "0.05", "unit_price": "", "net_cash_amount": "0.05"},
    ]
    normalized = canonical_activity_rows_from_export(rows, as_of="2026-07-28 15:18 GMT-03:00", year=2026)
    assert len(normalized) == 2
    trade = normalized[0]
    assert trade["account"] == "TFSA"
    assert trade["ticker"] == "GOOG"
    assert trade["side"] == "buy"
    assert trade["status"] == "Completed"
    assert trade["detail_status"] == "export_confirmed"
    assert normalized[1]["status"] == "Activity recorded"


def test_csv_fast_path_requires_fresh_dated_rows():
    rows = [{"transaction_date": "2026-07-24", "account_type": "TFSA"}]
    assert has_fresh_canonical_activity_export({
        "activity_export_rows": rows,
        "activity_export": {"age_hours_at_capture": 0.5, "is_stale_at_capture": False},
    }) is True
    assert has_fresh_canonical_activity_export({
        "activity_export_rows": rows,
        "activity_export": {"age_hours_at_capture": None, "is_stale_at_capture": False},
    }) is False
    assert has_fresh_canonical_activity_export({
        "activity_export_rows": rows,
        "activity_export": {"age_hours_at_capture": 25.0, "is_stale_at_capture": True},
    }) is False


def test_csv_fast_path_keeps_browser_only_terminal_statuses_without_browser_fills():
    export = [{
        "transaction_date": "2026-07-24", "account_type": "TFSA", "activity_type": "Trade",
        "activity_sub_type": "BUY", "symbol": "GOOG", "currency": "CAD", "quantity": "10",
        "unit_price": "51.57", "net_cash_amount": "-515.70",
    }]
    browser = [
        {"account": "TFSA", "ticker": "GOOG", "side": "buy", "status": "Completed"},
        {"account": "TFSA", "ticker": "OLD", "side": "buy", "status": "Cancelled"},
        {"account": "RRSP", "ticker": "LMT", "side": "buy", "status": "Expired"},
        {
            "account": "Non-registered",
            "ticker": "F",
            "side": "sell",
            "status": "Status unconfirmed",
            "total_value": "$62.00 CAD",
        },
    ]
    activity = merge_fresh_export_with_browser_terminal_activity(export, browser)
    assert [(row.get("ticker"), row.get("status")) for row in activity] == [
        ("GOOG", "Completed"), ("OLD", "Cancelled"), ("LMT", "Expired"),
    ]


def test_missing_export_path_warns_and_does_not_abort(tmp_path):
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")
    provenance = import_exports(state, tmp_path / "nope.csv", None)
    assert provenance == {}
    assert any("not found and was ignored" in w for w in state.warnings)


# --------------------------------------------------------------------------
# Capture speed. Adaptive settles must never wait longer than the fixed sleeps
# they replaced, and the control inventory must stay a single DOM pass.
# --------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock that only advances when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_until_returns_as_soon_as_the_probe_passes():
    clock = _FakeClock()
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return calls["n"] >= 3

    assert wait_until(probe, timeout=2.5, interval=0.15, clock=clock.time, sleeper=clock.sleep) is True
    assert calls["n"] == 3
    # two sleeps of the poll interval, far short of the 2.5s budget it replaced
    assert clock.sleeps == [0.15, 0.15]
    assert clock.now == pytest.approx(0.30)


def test_wait_until_never_waits_longer_than_the_budget():
    clock = _FakeClock()
    assert wait_until(lambda: False, timeout=2.5, interval=0.15, clock=clock.time, sleeper=clock.sleep) is False
    # the final partial sleep is trimmed so the total is exactly the budget
    assert clock.now == pytest.approx(2.5)
    assert max(clock.sleeps) <= 0.15


def test_wait_until_treats_a_raising_probe_as_not_ready():
    clock = _FakeClock()
    attempts = {"n": 0}

    def probe():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("browser hiccup")
        return True

    assert wait_until(probe, timeout=1.0, interval=0.1, clock=clock.time, sleeper=clock.sleep) is True
    assert attempts["n"] == 2


def test_wait_until_checks_the_probe_before_sleeping_at_all():
    clock = _FakeClock()
    assert wait_until(lambda: True, timeout=2.5, clock=clock.time, sleeper=clock.sleep) is True
    assert clock.sleeps == []
    assert clock.now == 0.0


def test_settle_budgets_do_not_exceed_the_sleeps_they_replaced():
    # Each budget is the unconditional sleep that used to run in that spot, so
    # a slow page can never take longer than it does today.
    assert APP_NAVIGATION_SETTLE_SECONDS == 2.5
    assert EXTERNAL_NAVIGATION_SETTLE_SECONDS == 3.0
    assert ACCOUNT_CARD_SETTLE_SECONDS == 2.2
    assert CONTROL_CLICK_SETTLE_SECONDS == 1.8
    assert ACTIVITY_FILTER_SETTLE_SECONDS == 1.5
    assert POLL_INTERVAL_SECONDS < min(
        APP_NAVIGATION_SETTLE_SECONDS, ACCOUNT_CARD_SETTLE_SECONDS,
        CONTROL_CLICK_SETTLE_SECONDS, ACTIVITY_FILTER_SETTLE_SECONDS,
    )


def test_control_inventory_stays_a_single_dom_pass():
    # The heading query used to run once per control element. Keeping it hoisted
    # is the whole optimisation, so pin it.
    assert CONTROLS_SCRIPT.count("querySelectorAll('h3')") == 1
    assert CONTROLS_SCRIPT.count("compareDocumentPosition") == 1
    # the fields every parser depends on must all still be produced
    for field in ("tag", "role", "type", "text", "href", "id", "date_context"):
        assert f"{field}:" in CONTROLS_SCRIPT or f"{field}," in CONTROLS_SCRIPT
    # heading text is read once per heading, not once per element
    assert "headingText" in CONTROLS_SCRIPT


def test_accumulate_builds_running_totals(tmp_path):
    state = RunState(out_dir=tmp_path, mode="FULL")
    state.accumulate("dom_control_inventory", 0.20)
    state.accumulate("dom_control_inventory", 0.30)
    row = state.performance["dom_control_inventory"]
    assert row["elapsed_seconds"] == pytest.approx(0.50)
    assert row["count"] == 2
    assert row["milliseconds_per_item"] == pytest.approx(250.0)
    # accumulate must not disturb a one-shot metric recorded under another name
    state.metric("order_detail_capture", 40.0, 114)
    assert state.performance["order_detail_capture"]["count"] == 114
    assert state.performance["dom_control_inventory"]["count"] == 2


def test_pending_capture_scans_unfiltered_activity_after_clear(tmp_path):
    class ActivityDriver:
        current_url = "https://my.wealthsimple.com/app/activity"

        def execute_script(self, _script, *_args):
            return True if "return window.innerHeight" in _script else None

    state = RunState(out_dir=tmp_path, mode="FULL")
    ensure_dirs(tmp_path)
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = ActivityDriver()
    clicked = []
    reader.go_app_path = lambda _path, _label: None
    reader.wait_for_activity_cards = lambda _purpose: True
    reader.wait_for_pending_activity_cards = lambda _purpose: True
    reader.click_label = lambda label, **_kwargs: clicked.append(label) or True
    reader.settle = lambda *_args, **_kwargs: True
    reader.disclosure_controls_present = lambda: True
    reader.capture = lambda *_args, **_kwargs: {
        "visible_text": "",
        "screenshot": "",
        "url": reader.driver.current_url,
    }
    reader.controls = lambda: []
    reader._find_safe_button = lambda _label: None

    _evidence, rows = reader.capture_activity()

    assert clicked == ["Clear"]
    assert state.pending_scan_complete is True
    assert rows == []


def test_activity_detail_snapshot_keeps_full_evidence_and_exact_region(tmp_path):
    class SnapshotDriver:
        current_url = "https://my.wealthsimple.com/app/activity"
        title = "Activity"

        def __init__(self):
            self.calls = []

        def execute_script(self, script, *args):
            self.calls.append(script)
            if script == ACTIVITY_DETAIL_SNAPSHOT_SCRIPT:
                assert args == ("NOC pending row", None)
                return {
                    "body_text": "Activity\nAccount\nTFSA\nStatus\nPending",
                    "detail_text": "Account\nTFSA\nStatus\nPending\nType\nLimit sell\nEntered quantity\n2 shares",
                    "url": self.current_url,
                    "title": self.title,
                }
            # close_support_chat is intentionally still run before every
            # capture; it is read-only and keeps an overlay out of evidence.
            return None

    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = SnapshotDriver()

    evidence, detail = reader.capture_open_activity_detail("order-details", "noc", "NOC pending row")

    assert detail.startswith("Account\nTFSA")
    assert Path(evidence["visible_text"]).read_text(encoding="utf-8") == "Activity\nAccount\nTFSA\nStatus\nPending\n"
    dom = json.loads(Path(evidence["dom"]).read_text(encoding="utf-8"))
    assert dom == {"url": reader.driver.current_url, "title": reader.driver.title, "controls": []}
    assert evidence["screenshot"] == ""
    assert reader.driver.calls.count(ACTIVITY_DETAIL_SNAPSHOT_SCRIPT) == 1
    assert state.performance["activity_detail_snapshot"]["count"] == 1


def test_activity_detail_snapshot_prefers_exact_control_id(tmp_path):
    class SnapshotDriver:
        current_url = "https://my.wealthsimple.com/app/activity"
        title = "Activity"

        def execute_script(self, script, *args):
            if script == ACTIVITY_DETAIL_SNAPSHOT_SCRIPT:
                assert args == ("duplicate ladder text", "pending-header-42")
                return {
                    "body_text": "Activity\nAccount\nTFSA\nStatus\nPending",
                    "detail_text": "Account\nTFSA\nStatus\nPending",
                    "url": self.current_url,
                    "title": self.title,
                }
            return None

    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = SnapshotDriver()

    _evidence, detail = reader.capture_open_activity_detail(
        "order-details",
        "duplicate",
        "duplicate ladder text",
        "pending-header-42",
    )

    assert detail == "Account\nTFSA\nStatus\nPending"


def test_account_capture_uses_preload_then_serial_fallback(tmp_path):
    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    calls = []
    reader.preload_home_account_cards = lambda: calls.append("preloaded") or None
    reader.click_home_account_cards_serial = (
        lambda: calls.append("serial") or {"TFSA": {"account": "TFSA"}}
    )

    accounts = reader.click_home_account_cards("preloaded")

    assert accounts == {"TFSA": {"account": "TFSA"}}
    assert calls == ["preloaded", "serial"]
    assert any(
        event["event"] == "account_preload_serial_fallback"
        for event in state.safety_log
    )


def test_account_capture_serial_control_skips_preload(tmp_path):
    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.preload_home_account_cards = lambda: pytest.fail(
        "serial control must not preload account tabs"
    )
    reader.click_home_account_cards_serial = lambda: {
        "TFSA": {"account": "TFSA"}
    }

    assert reader.click_home_account_cards("serial") == {
        "TFSA": {"account": "TFSA"}
    }


def test_account_worker_cleanup_blocks_if_a_created_tab_remains(tmp_path):
    class CleanupDriver:
        def __init__(self):
            self.window_handles = ["anchor", "worker"]
            self.current = "anchor"

        def switch_to_window(self, handle):
            self.current = handle

        @property
        def switch_to(self):
            outer = self

            class Switcher:
                def window(self, handle):
                    outer.switch_to_window(handle)

            return Switcher()

        def close(self):
            # Model a renderer that ignored the close request.
            return None

    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = CleanupDriver()

    assert not reader._close_readonly_worker_tabs(
        "anchor", {"anchor"}, {"worker"}
    )
    assert any("worker tab" in blocker for blocker in state.blockers)


def test_activity_detail_batch_uses_exact_pending_headers_and_preserves_evidence(tmp_path):
    rows = [
        {
            "stable_row_key": "key-a",
            "source_control_id": "header-a",
            "row_text": "AAA\nLimit buy\nTFSA\n$10.00 CAD\nPending",
        },
        {
            "stable_row_key": "key-b",
            "source_control_id": "header-b",
            "row_text": "BBB\nLimit sell\nRRSP\n$20.00 CAD\nPending",
        },
    ]
    detail_template = (
        "Account\n{account}\nStatus\nPending\nSubmitted\nJuly 28, 2026\n"
        "Expires\nOctober 23, 2026\nTrading session\nMarket hours\nType\nLimit buy\n"
        "Entered quantity\n1 share\nEstimated total cost\n$10.00 CAD"
    )

    class BatchDriver:
        current_url = "https://my.wealthsimple.com/app/activity"
        title = "Activity"

        def __init__(self):
            self.timeout = None
            self.closed = []

        def set_script_timeout(self, timeout):
            self.timeout = timeout

        def execute_async_script(self, script, specs, timeout_ms):
            assert script == ACTIVITY_DETAIL_BATCH_SCRIPT
            assert [spec["source_control_id"] for spec in specs] == ["header-a", "header-b"]
            assert timeout_ms > 0
            return {
                "ok": True,
                "body_text": "Activity\nPending order details",
                "url": self.current_url,
                "title": self.title,
                "details": [
                    {
                        "stable_row_key": "key-a",
                        "ready": True,
                        "detail_text": detail_template.format(account="TFSA"),
                    },
                    {
                        "stable_row_key": "key-b",
                        "ready": True,
                        "detail_text": detail_template.format(account="RRSP"),
                    },
                ],
            }

        def execute_script(self, script, control_ids=None):
            if script == ACTIVITY_DETAIL_BATCH_CLOSE_SCRIPT:
                self.closed = control_ids
                return [True, True]
            return None

    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = BatchDriver()
    reader.close_support_chat = lambda: None
    reader.clear_blocking_modal = lambda: True

    snapshots = reader.capture_open_activity_detail_batch(
        "order-details",
        rows,
        {"key-a": "detail-a", "key-b": "detail-b"},
    )

    assert set(snapshots) == {"key-a", "key-b"}
    assert snapshots["key-a"][1].startswith("Account\nTFSA")
    assert snapshots["key-b"][1].startswith("Account\nRRSP")
    assert all(Path(snapshot[0]["visible_text"]).is_file() for snapshot in snapshots.values())
    assert reader.driver.closed == ["header-a", "header-b"]
    assert reader.driver.timeout > 0
    assert len(state.click_log) == 4
    assert not [event for event in state.safety_log if event["event"] == "activity_detail_batch_fallback"]
    assert "document.getElementById(row.source_control_id)" in ACTIVITY_DETAIL_BATCH_SCRIPT
    assert "activeStatuses.includes(value.toLowerCase())" in ACTIVITY_DETAIL_BATCH_SCRIPT
    assert '"partially filled"' in ACTIVITY_DETAIL_BATCH_SCRIPT
    assert '"pending cancellation"' in ACTIVITY_DETAIL_BATCH_SCRIPT
    assert "Cancel order" not in ACTIVITY_DETAIL_BATCH_SCRIPT
    assert "Modify order" not in ACTIVITY_DETAIL_BATCH_SCRIPT


def test_activity_detail_batch_key_mismatch_falls_back_and_closes(tmp_path):
    class MismatchDriver:
        current_url = "https://my.wealthsimple.com/app/activity"
        title = "Activity"

        def set_script_timeout(self, _timeout):
            pass

        def execute_async_script(self, _script, _specs, _timeout_ms):
            return {
                "ok": True,
                "body_text": "Activity",
                "details": [{"stable_row_key": "wrong", "ready": True, "detail_text": "detail"}],
            }

        def execute_script(self, script, control_ids=None):
            if script == ACTIVITY_DETAIL_BATCH_CLOSE_SCRIPT:
                self.closed = control_ids
            return []

    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = MismatchDriver()
    reader.close_support_chat = lambda: None
    reader.clear_blocking_modal = lambda: True
    rows = [{
        "stable_row_key": "expected",
        "source_control_id": "header",
        "row_text": "AAA\nLimit buy\nTFSA\n$10.00 CAD\nPending",
    }]

    assert reader.capture_open_activity_detail_batch(
        "order-details", rows, {"expected": "detail"}
    ) == {}
    assert reader.driver.closed == ["header"]
    assert any(
        event["event"] == "activity_detail_batch_fallback"
        and event["reason"] == "batch detail key/count mismatch"
        for event in state.safety_log
    )


def test_activity_detail_snapshot_fallback_is_evidenced_without_warning_flood(tmp_path):
    class FallbackDriver:
        current_url = "https://my.wealthsimple.com/app/activity"
        title = "Activity"

        def execute_script(self, script, *args):
            if script == ACTIVITY_DETAIL_SNAPSHOT_SCRIPT:
                raise RuntimeError("stale detail region")
            if "Close support chat" in script:
                return None
            if "aria-expanded" in script:
                return "Account\nTFSA\nStatus\nPending"
            if "document.body ? document.body.innerText" in script:
                return "Activity\nAccount\nTFSA\nStatus\nPending"
            return None

    state = RunState(out_dir=tmp_path, mode="FULL")
    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = FallbackDriver()

    for index in range(3):
        evidence, detail = reader.capture_open_activity_detail("order-details", f"fallback-{index}", "row")
        assert Path(evidence["visible_text"]).is_file()
        assert detail

    warnings = [warning for warning in state.warnings if "snapshot fell back" in warning]
    assert len(warnings) == 1
    assert state.performance["activity_detail_snapshot_fallback"]["count"] == 3
    assert sum(1 for event in state.safety_log if event["event"] == "activity_detail_snapshot_fallback") == 3


def test_order_detail_uses_clicked_ticker_when_region_omits_it():
    text = """Account
TFSA
Status
Pending
Submitted
June 17, 2026
12:16 pm
Expires
September 14, 2026
5:00 pm
Type
Limit buy
Limit price
$60.00 CAD
Entered quantity
5 shares
Estimated total cost
$300.00 CAD
View details
"""
    details = parse_order_detail_blocks(text, {"url": "https://my.wealthsimple.com/app/activity"}, ticker_hint="BAM.TO")
    assert len(details) == 1
    assert details[0]["ticker"] == "BAM.TO"
    assert details[0]["confirmation_level"] == "detail_confirmed"


def test_snapshot_body_still_feeds_the_strict_forbidden_state_scan(tmp_path):
    """The merged snapshot must not narrow the safety scan to the drawer.

    capture_open_activity_detail reads the full body and the card region in one
    script. If a later optimisation returned only the region, every detail would
    still parse and every evidence file would still be written, but the strict
    forbidden-state scan would silently stop seeing the rest of the page. Pin the
    full-body scan so that change fails loudly.
    """
    ensure_dirs(tmp_path)
    state = RunState(out_dir=tmp_path, mode="FULL")

    class _Driver:
        current_url = "https://my.wealthsimple.com/app/activity"
        title = "Wealthsimple | Smart investing"

        def execute_script(self, script, *args):
            if script is ACTIVITY_DETAIL_SNAPSHOT_SCRIPT:
                return {
                    # "Review order" appears on the page but NOT in the drawer
                    "body_text": "TFSA\nReview order\nAccount\nTFSA\nStatus\nPending\n",
                    "detail_text": "Account\nTFSA\nStatus\nPending\nType\nLimit buy\n",
                    "url": self.current_url,
                    "title": self.title,
                }
            return None

        def save_screenshot(self, path):
            raise AssertionError("a detail capture must never take a screenshot")

    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = _Driver()
    evidence, region = reader.capture_open_activity_detail("order-details", "order-detail-900", "SPCX\nLimit buy")

    assert "Review order" not in region                      # the drawer itself is clean
    assert any("Review order" in blocker for blocker in state.blockers)
    assert any(event["event"] == "forbidden_state_text" for event in state.safety_log)
    # and the absent screenshot is recorded as empty, never as "."
    assert evidence["screenshot"] == ""
    assert evidence["screenshot"] != "."


def _modal_reader(tmp_path, headings):
    """Reader whose page reports `headings` in turn, one per probe."""
    state = RunState(out_dir=tmp_path, mode="FULL")

    class _Driver:
        current_url = "https://my.wealthsimple.com/app/activity"

        def execute_script(self, script, *args):
            if script is BLOCKING_MODAL_SCRIPT:
                return headings.pop(0) if headings else None
            return None

    reader = WealthsimpleReader.__new__(WealthsimpleReader)
    reader.state = state
    reader.driver = _Driver()
    reader.escapes = 0

    def _escape():
        reader.escapes += 1

    reader.send_escape = _escape
    return reader, state


def test_a_clear_page_needs_no_escape(tmp_path):
    ensure_dirs(tmp_path)
    reader, state = _modal_reader(tmp_path, [None])
    assert reader.clear_blocking_modal() is True
    assert reader.escapes == 0
    assert state.status == "OK"


def test_a_covering_dialog_is_escaped_not_clicked(tmp_path):
    ensure_dirs(tmp_path)
    reader, state = _modal_reader(tmp_path, ["Download activities", None])
    assert reader.clear_blocking_modal() is True
    assert reader.escapes == 1
    assert state.status == "OK"
    assert any(event["event"] == "blocking_modal_dismissed" for event in state.safety_log)
    assert state.click_log == []


def test_a_dialog_that_will_not_close_blocks_the_run(tmp_path):
    ensure_dirs(tmp_path)
    reader, state = _modal_reader(tmp_path, ["Download activities"] * 10)
    assert reader.clear_blocking_modal() is False
    assert reader.escapes == BLOCKING_MODAL_ESCAPE_ATTEMPTS
    assert state.status == "ERROR"
    assert any("modal dialog is covering the page" in blocker for blocker in state.blockers)
    assert any(event["event"] == "blocking_modal_persisted" for event in state.safety_log)
    assert state.click_log == []


# Four account workers loading during the pending drawer pass caused missing
# details in live measurements. Keep that pass uncontended, then overlap the
# workers with recent-activity scanning.
def test_workers_are_launched_after_the_pending_drawer_pass():
    import inspect

    from src.full_account_inventory import run_live

    source = inspect.getsource(run_live)
    detail = source.index("open_order_details(")
    launch = source.index("launch_home_account_preload_workers(")
    assert detail < launch
    assert launch < source.index("finish_home_account_preload(")


def test_worker_baseline_is_taken_at_launch_and_anchor_is_preserved():
    import inspect

    from src.full_account_inventory import WealthsimpleReader

    launch = inspect.getsource(
        WealthsimpleReader.launch_home_account_preload_workers
    )
    assert 'preload["baseline_handles"] = set(self.driver.window_handles)' in launch
    assert 'current_window_handle != preload["anchor"]' in launch


def test_activity_source_reconciliation_is_multiset_not_set_based():
    browser = [
        {
            "account": "TFSA",
            "ticker": "CRM",
            "side": "buy",
            "status": "Status unconfirmed",
            "total_value": "$900.00 CAD",
            "currency": "USD",
            "date": "July 24, 2026",
            "source_control_id": "browser-a",
        },
        {
            "account": "TFSA",
            "ticker": "CRM",
            "side": "buy",
            "status": "Status unconfirmed",
            "total_value": "$900.00 CAD",
            "currency": "CAD",
            "date": "July 24, 2026",
            "source_control_id": "browser-b",
        },
    ]
    exported = [
        {
            "transaction_date": "2026-07-24",
            "settlement_date": "2026-07-24",
            "account_type": "TFSA",
            "activity_type": "Trade",
            "activity_sub_type": "BUY",
            "symbol": "CRM",
            "currency": "CAD",
            "quantity": "75",
            "unit_price": "12",
            "net_cash_amount": "-900",
        },
        {
            "transaction_date": "2026-07-24",
            "settlement_date": "2026-07-24",
            "account_type": "TFSA",
            "activity_type": "Trade",
            "activity_sub_type": "BUY",
            "symbol": "GOOG",
            "currency": "CAD",
            "quantity": "10",
            "unit_price": "51.57",
            "net_cash_amount": "-515.70",
        },
    ]

    result = reconcile_activity_sources(
        browser, exported, year=2026, today=date(2026, 7, 28)
    )

    assert result["matched_browser_occurrences"] == 1
    assert len(result["browser_only"]) == 1
    assert result["browser_only"][0]["source_control_id"] == "browser-b"
    assert len(result["browser_only_review_required"]) == 1
    assert result["browser_only_recent_unsettled"] == []
    assert len(result["export_only"]) == 1
    assert result["export_only"][0]["ticker"] == "GOOG"
    assert result["status"] == "differences_found"


def test_activity_source_reconciliation_separates_recent_unsettled_rows():
    browser = [{
        "account": "Non-registered",
        "ticker": "F",
        "side": "sell",
        "status": "Completed",
        "total_value": "$62.00 CAD",
        "currency": "USD",
        "date": "Today",
        "source_control_id": "ford-fill",
    }]

    result = reconcile_activity_sources(
        browser, [], year=2026, today=date(2026, 7, 28)
    )

    assert result["status"] == "differences_expected_recent_settlement"
    assert result["browser_only_review_required"] == []
    assert result["browser_only_recent_unsettled"][0]["ticker"] == "F"
    assert result["browser_only_recent_unsettled"][0]["currency"] == "CAD"


def test_holdings_source_reconciliation_controls_quantity_not_market_value():
    browser = [
        {
            "account": "Non-registered",
            "ticker": "BAM.TO",
            "quantity": "1 share",
            "market_value": "$67.64 CAD",
        },
        {
            "account": "TFSA",
            "ticker": "QCOM",
            "quantity": "2 shares",
            "market_value": "$43.36 CAD",
        },
    ]
    exported = [
        {
            "Account Type": "Non-registered",
            "Symbol": "BAM.TO",
            "Quantity": "1",
            "Market Value": "68.00",
            "Market Value Currency": "CAD",
        },
        {
            "Account Type": "TFSA",
            "Symbol": "QCOM",
            "Quantity": "3",
            "Market Value": "65.04",
            "Market Value Currency": "CAD",
        },
    ]

    result = reconcile_holdings_sources(browser, exported)

    assert result["status"] == "differences_found"
    assert result["matched"][0]["ticker"] == "BAM"
    assert result["matched"][0]["browser_market_value"] != result["matched"][0]["export_market_value"]
    assert result["differences"] == [{
        "type": "holding_quantity_mismatch",
        "account": "TFSA",
        "ticker": "QCOM",
        "browser_quantity": 2.0,
        "export_quantity": 3.0,
    }]


def test_holdings_source_reconciliation_separates_currency_cash_rows():
    browser = [{
        "account": "RRSP", "ticker": "GOOG", "quantity": "7 shares",
        "market_value": "$390.95 CAD",
    }]
    exported = [
        {
            "Account Type": "RRSP", "Symbol": "GOOG", "Security Type": "EQUITY",
            "Quantity": "7", "Market Value": "390.95", "Market Value Currency": "CAD",
        },
        {
            "Account Type": "RRSP", "Symbol": "CAD", "Security Type": "CURRENCY",
            "Quantity": "3764.47", "Market Value": "3764.47", "Market Value Currency": "CAD",
        },
    ]

    result = reconcile_holdings_sources(browser, exported)

    assert result["status"] == "quantities_match"
    assert result["browser_holding_count"] == 1
    assert result["export_holding_count"] == 1
    assert result["differences"] == []
    assert result["export_cash_positions"] == [{
        "account": "RRSP", "currency": "CAD", "quantity": 3764.47,
        "market_value": 3764.47, "market_value_currency": "CAD",
    }]


def test_activity_source_reconciliation_filters_both_sources_to_target_year():
    browser = [{
        "account": "TFSA",
        "ticker": "CRM",
        "side": "buy",
        "status": "Completed",
        "total_value": "$900.00 CAD",
        "date": "December 20, 2025",
    }]

    result = reconcile_activity_sources(
        browser, [], year=2026, today=date(2026, 1, 5)
    )

    assert result["browser_candidate_count"] == 0
    assert result["browser_only"] == []


def test_activity_source_reconciliation_uses_t1_settlement_window():
    browser = [{
        "account": "Non-registered",
        "ticker": "F",
        "side": "sell",
        "status": "Completed",
        "total_value": "$62.00 CAD",
        "date": "July 28, 2026",
    }]

    same_day = reconcile_activity_sources(
        browser,
        [],
        year=2026,
        today=date(2026, 7, 28),
        export_as_of="2026-07-28 12:45 GMT-03:00",
    )
    assert same_day["status"] == "differences_expected_recent_settlement"

    after_one_business_day = reconcile_activity_sources(
        browser,
        [],
        year=2026,
        today=date(2026, 7, 30),
        export_as_of="2026-07-30 12:45 GMT-03:00",
    )
    assert after_one_business_day["status"] == "differences_found"
    assert len(after_one_business_day["browser_only_review_required"]) == 1


def test_activity_source_reconciliation_keeps_empty_browser_scan_empty():
    exported = [{
        "transaction_date": "2026-07-24",
        "account_type": "TFSA",
        "activity_type": "Trade",
        "activity_sub_type": "BUY",
        "symbol": "CRM",
        "currency": "CAD",
        "quantity": "75",
        "unit_price": "12",
        "net_cash_amount": "-900",
    }]

    result = reconcile_activity_sources([], exported, year=2026)

    assert result["browser_candidate_count"] == 0
    assert result["matched_browser_occurrences"] == 0
    assert len(result["export_only"]) == 1


def test_holdings_source_reconciliation_separates_out_of_scope_accounts():
    exported = [{
        "Account Type": "Managed",
        "Symbol": "VTI",
        "Quantity": "1.5",
        "Market Value": "500",
        "Market Value Currency": "CAD",
    }]

    result = reconcile_holdings_sources([], exported)

    assert result["status"] == "quantities_match"
    assert result["differences"] == []
    assert result["out_of_scope_export_holdings"] == [{
        "account": "Managed",
        "ticker": "VTI",
        "quantity": 1.5,
    }]
