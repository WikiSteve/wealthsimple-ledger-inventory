"""Focused tests for inventory completeness, broker counts, and residuals."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.activity_filter_evidence import confirms_unfiltered, clear_label_matches, CLEAR_LABEL_CANONICAL
from src.inventory_completeness import (
    assess_inventory_completeness,
    classify_broker_count_corroboration,
    classify_status_token_free_rows,
    derive_sell_coverage_scope,
    make_count_observation,
    parse_broker_pending_count,
    residual_disclosure_rows,
    format_residual_warning,
)
from src.full_account_inventory import (
    RunState,
    reconcile_total_account_value,
    rebuild_bundle_from_existing,
    write_bundle,
)


PINNED = Path("/tmp/wealthsimple-full-account-inventory-20260916T160652-0300")


def _count(value):
    return make_count_observation(
        value,
        scope="all_accounts_activity",
        account_filter="all",
        status_filter="pending_transactions_label",
        population_meaning="broker_visible_pending_transactions",
        timestamp="2026-09-16T16:07:00-03:00",
        source="test",
    )


def test_parse_broker_pending_count_split_and_single_node():
    assert parse_broker_pending_count("Pending\n28 transactions\n") == 28
    assert parse_broker_pending_count("28 pending transactions") == 28
    assert parse_broker_pending_count("Pending") is None


def test_broker_count_classifications():
    agree = classify_broker_count_corroboration(_count(None), _count(28), parsed_orders=28, pending_non_orders=0)
    assert agree["classification"] == "partial"
    assert agree["blocks_completeness"] is False

    both_none = classify_broker_count_corroboration(_count(None), _count(None), parsed_orders=28, pending_non_orders=0)
    assert both_none["classification"] == "unavailable"
    assert both_none["blocks_completeness"] is False

    churn = classify_broker_count_corroboration(_count(28), _count(29), parsed_orders=28, pending_non_orders=0)
    assert churn["classification"] == "observed_churn"
    assert churn["blocks_completeness"] is True

    contradiction = classify_broker_count_corroboration(_count(28), _count(29), parsed_orders=28, pending_non_orders=0)
    # churn takes precedence when both present and differ
    assert contradiction["classification"] == "observed_churn"

    excess = classify_broker_count_corroboration(_count(30), _count(30), parsed_orders=28, pending_non_orders=0)
    assert excess["classification"] == "contradiction"
    assert excess["severity"] == "blocker"

    final_mismatch = classify_broker_count_corroboration(_count(30), _count(30), parsed_orders=28, pending_non_orders=0)
    assert final_mismatch["classification"] == "contradiction"
    assert final_mismatch["severity"] == "blocker"

    short = classify_broker_count_corroboration(_count(27), _count(27), parsed_orders=28, pending_non_orders=0)
    assert short["classification"] == "contradiction"
    assert short["severity"] == "warning"

    with_n = classify_broker_count_corroboration(_count(29), _count(29), parsed_orders=28, pending_non_orders=1)
    assert with_n["classification"] == "agreement"


def test_equal_counts_with_unresolved_status_unknown_still_incomplete():
    classification = {
        "status_unknown_row_count": 1,
        "export_corroborated_terminal": [],
        "proven_pending_order": [],
        "unresolved_review_required": [{"account": "TFSA"}],
        "u3_unresolved_count": 1,
        "corroboration_ran": True,
        "fail_closed_missing_export": False,
        "reason_codes": ["status_unknown_rows_unresolved_after_reconciliation"],
    }
    result = assess_inventory_completeness(
        filter_default_before=True,
        filter_default_after=True,
        traversal_exhausted=True,
        unparsed_pending_count=0,
        parsed_orders=28,
        detail_confirmed_orders=28,
        pending_non_orders=0,
        unresolved_orders=0,
        status_unknown_classification=classification,
        fresh_export=True,
        blockers=[],
        broker_count_before=_count(28),
        broker_count_after=_count(28),
    )
    assert result["inventory_complete"] is False
    assert result["count_corroboration"]["classification"] == "agreement"


def test_missing_historical_post_traversal_is_unverified_not_invented():
    result = assess_inventory_completeness(
        filter_default_before=True,
        filter_default_after=None,
        traversal_exhausted=True,
        unparsed_pending_count=0,
        parsed_orders=28,
        detail_confirmed_orders=28,
        pending_non_orders=0,
        unresolved_orders=0,
        status_unknown_classification={
            "status_unknown_row_count": 0,
            "u3_unresolved_count": 0,
            "reason_codes": [],
        },
        fresh_export=True,
        blockers=[],
        broker_count_before=None,
        broker_count_after=_count(28),
    )
    assert result["inventory_complete"] is False
    assert result["completeness_state"] == "unverified"
    assert result["post_traversal_filter_state"] == "not_observed_historically"


def test_status_unknown_fail_closed_without_export():
    rows = [{"side": "buy", "status": "Status unconfirmed", "account": "TFSA", "ticker": "X"} for _ in range(3)]
    out = classify_status_token_free_rows(rows, None, fresh_export=False)
    assert out["u3_unresolved_count"] == 3
    assert out["fail_closed_missing_export"] is True


@pytest.mark.skipif(not PINNED.exists(), reason="pinned bundle not present")
def test_pinned_bundle_status_unknown_rows_are_export_corroborated():
    browser = json.loads((PINNED / "browser-activity-control-rows.json").read_text())
    recon = json.loads((PINNED / "source-reconciliation.json").read_text())["activity"]
    out = classify_status_token_free_rows(browser, recon, fresh_export=True)
    assert out["status_unknown_row_count"] == 107
    assert out["u3_unresolved_count"] == 0
    assert len(out["export_corroborated_terminal"]) == 107


@pytest.mark.skipif(not PINNED.exists(), reason="pinned bundle not present")
def test_pinned_filter_snapshot_confirms_unfiltered_under_new_rule():
    snapshot = json.loads((PINNED / "logs" / "activity-filter-state.json").read_text())["snapshot"]
    assert confirms_unfiltered(snapshot) is True


def test_mixed_conflict_fails_closed():
    snapshot = json.loads((PINNED / "logs" / "activity-filter-state.json").read_text())["snapshot"] if PINNED.exists() else None
    if snapshot is None:
        pytest.skip("pinned bundle not present")
    s = deepcopy(snapshot)
    holdings = next(g for g in s["groups"] if g["name"] == "Holdings")
    holdings["checkboxes"] = [True]
    holdings["pressed"] = ["false"] * len(holdings["pressed"])
    assert confirms_unfiltered(s) is False


def test_clear_label_table_agrees():
    for label in list(CLEAR_LABEL_CANONICAL) + ["clear", " Clear ", "Clear all"]:
        assert clear_label_matches(label)
    assert not clear_label_matches("Clear cart")


def test_residual_note_and_disclosure():
    account = {
        "account": "TFSA",
        "total_account_value": "C$100.40",
        "available_to_trade": "C$100.00",
        "total_cash_available_semantics": {"reference_fx_rate": "1.0"},
    }
    result = reconcile_total_account_value(account, [], [], components_captured=True)
    assert result["residual_cad"] == 0.4
    assert "fully explained" not in result["note"]
    rows = residual_disclosure_rows({"TFSA": result, "RRSP": {"residual_cad": 2.2, "status": "minor_snapshot_residual"}})
    assert {r["account"] for r in rows} == {"TFSA", "RRSP"}
    assert format_residual_warning(rows)


def test_scope_text_omits_unsupported_reset():
    text = derive_sell_coverage_scope(
        reset_attempted=False,
        reset_click_succeeded=False,
        filter_default_before=False,
        filter_default_after=None,
        inventory_complete=False,
    )
    assert "explicit filter reset" not in text
    assert "not_observed_historically" in text


@pytest.mark.skipif(not PINNED.exists(), reason="pinned bundle not present")
def test_rebuild_preserves_residual_warnings_and_does_not_invent_post_traversal(tmp_path):
    out = tmp_path / "rebuilt"
    rebuild_bundle_from_existing(PINNED, out)
    manifest = json.loads((out / "manifest.json").read_text())
    warnings = "\n".join(manifest.get("warnings") or [])
    assert "TFSA" in warnings and "0.4" in warnings
    assert "RRSP" in warnings and "2.2" in warnings
    assert "explicit filter reset" not in (manifest.get("sell_coverage_scope") or "")
    completeness = manifest.get("inventory_completeness") or {}
    assert completeness.get("post_traversal_filter_state") == "not_observed_historically"
    assert completeness.get("inventory_complete") is False
    # Source reconciliation invariant
    sr = json.loads((out / "source-reconciliation.json").read_text())["activity"]
    assert sr.get("matched_browser_occurrences") == 107
    assert sr.get("browser_only_review_required") == []
    # Idempotence of original timestamp
    assert manifest.get("source_capture_generated_at")
    handoff = (out / "next-message-for-chatgpt.md").read_text()
    assert "0.4" in handoff and "2.2" in handoff
    capture_warnings = (out / "capture-warnings.md").read_text()
    assert "## Unexplained residuals" in capture_warnings
    assert "## Inventory completeness" in capture_warnings
