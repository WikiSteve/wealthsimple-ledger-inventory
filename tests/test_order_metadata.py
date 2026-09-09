"""Synthetic metadata; never real account identifiers."""
from copy import deepcopy

import pytest

from src.order_metadata import validate_metadata, enrich_order
from src.full_account_inventory import parse_order_detail_blocks, merge_rows_and_details
from src.sell_coverage import sell_coverage, quantity_evidence


def raw(oid="order-a", fill=None):
    return {"activity": {"externalCanonicalId": oid, "accountId": "acct-a", "securityId": "sec-a"},
            "records": [{"id": oid, "canonicalAccountId": "acct-a", "securityId": "sec-a",
                         "submittedQuantity": "3", "fillQuantity": fill, "side": "SELL"}]}


@pytest.mark.parametrize("fill", [None, "0", "1", "3"])
def test_raw_fill_never_changes_quantity_provenance(fill):
    order = {"side": "sell", **quantity_evidence("3 shares", None, None)}
    got = enrich_order(order, validate_metadata(raw(fill=fill)))
    assert got["order_id"] == "order-a"
    assert got["order_metadata"]["record"]["fillQuantity"] == fill
    assert got["order_metadata"]["quantity_semantics_verified"] is False
    assert all(got[k] == v for k, v in order.items())
    assert got["remaining_quantity"] is None


@pytest.mark.parametrize("field", ["externalCanonicalId", "accountId", "securityId"])
@pytest.mark.parametrize("value", [None, "", "different", 3])
def test_linkage_mismatch_rejects(field, value):
    data = raw()
    data["activity"][field] = value
    evidence = validate_metadata(data)
    assert evidence["identity_verified"] is False
    assert "order_id" not in enrich_order({"side": "sell"}, evidence)


def test_duplicate_and_conflicting_records():
    data = raw()
    data["records"].append(deepcopy(data["records"][0]))
    assert validate_metadata(data)["identity_verified"] is True
    data["records"][1]["fillQuantity"] = "1"
    assert validate_metadata(data)["reason"] == "order_record_ambiguous"


@pytest.mark.parametrize("data", [None, {}, {"records": []}, {"records": "not a list"}])
def test_unavailable_falls_back(data):
    assert not validate_metadata(data)["identity_verified"]
    assert "order_id" not in enrich_order({"side": "sell"}, data)


def test_unrelated_fields_not_retained():
    data = raw()
    data["records"][0]["identityId"] = "not-for-capture"
    data["activity"]["counterpartyEmail"] = "not-for-capture"
    evidence = validate_metadata(data)
    assert "identityId" not in evidence["record"]
    assert "counterpartyEmail" not in evidence["activity"]


def test_forged_flag_and_side_conflict():
    evidence = validate_metadata(raw())
    evidence["record"]["id"] = "other"
    assert "order_id" not in enrich_order({"side": "sell"}, evidence)
    assert "order_id" not in enrich_order({"side": "buy"}, validate_metadata(raw()))


def test_parser_merge_to_report_two_distinct_identities_quantity_still_unknown():
    rows, details = [], []
    for i in (1, 2):
        text = (f"Account\nRRSP\nStatus\nPending\nSubmitted\nSeptember 9, 2026\n9:00 am\n"
                f"Expires\nSeptember 10, 2026\n4:00 pm\nType\nLimit sell\n"
                f"Limit price\n$10.00 CAD\nEntered quantity\n{i} shares\n"
                f"Estimated total proceeds\n${i}0.00 CAD\nView XYZ details\n")
        parsed = parse_order_detail_blocks(text, {"url": "fixture"})[0]
        rows.append(deepcopy(parsed))
        parsed["detail_capture_row_key"] = parsed["stable_row_key"]
        details.append(enrich_order(parsed, validate_metadata(raw(f"order-{i}"))))
    merged, unresolved = merge_rows_and_details(rows, details)
    assert not unresolved
    assert len({r["order_id"] for r in merged}) == 2
    result = sell_coverage([{"account": "RRSP", "ticker": "XYZ", "quantity": "12",
                             "security_quote_currency": "CAD"}], merged, lambda s: s,
                           inventory_complete=True)
    summary = next(r for r in result if "coverage_status" in r)
    assert summary["coverage_status"] == "uncertain"
    assert summary["uncovered_quantity"] is None
    assert "order_identity_ambiguous" not in str(summary)
    assert "pending_quantity_submitted_conflict" in str(summary)
