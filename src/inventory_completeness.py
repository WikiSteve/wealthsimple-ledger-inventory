"""Evidence-based open-order inventory completeness assessment.

Reassessment may reinterpret retained evidence. It may never create an
observation that was not captured.
"""

from __future__ import annotations

import re
from typing import Any

# Split-node form: "Pending" / "28 transactions" and single-node variants.
_PENDING_COUNT_RE = re.compile(
    r"(?i)(?:^|\b)pending(?:\s+transactions?)?\s*[:=\-]?\s*(\d+)\b"
    r"|(\d+)\s+pending(?:\s+transactions?)?\b"
    r"|(\d+)\s+transactions?\b"
)


def parse_broker_pending_count(text: str | None) -> int | None:
    """Parse a broker pending-total label from visible text.

    Handles the split-node form where ``Pending`` and ``28 transactions`` are
    separate lines, and single-node ``28 pending transactions`` forms.
    Returns None when absent or unparsable — never invents a count.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Prefer the Activity chrome label: a "Pending" line followed by "N transactions".
    for index, line in enumerate(lines):
        if re.fullmatch(r"(?i)pending", line) and index + 1 < len(lines):
            follow = lines[index + 1]
            match = re.fullmatch(r"(?i)(\d+)\s+transactions?", follow)
            if match:
                return int(match.group(1))
    joined = " ".join(lines)
    match = _PENDING_COUNT_RE.search(joined)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return int(group)
    return None


def make_count_observation(
    value: int | None,
    *,
    scope: str,
    account_filter: str,
    status_filter: str,
    population_meaning: str,
    timestamp: str | None,
    source: str,
) -> dict[str, Any]:
    """Record a broker count with comparable-population metadata."""
    return {
        "value": value,
        "unavailable": value is None,
        "scope": scope,
        "account_filter": account_filter,
        "status_filter": status_filter,
        "population_meaning": population_meaning,
        "timestamp": timestamp,
        "source": source,
    }


def counts_comparable(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """True when two count observations describe the same population."""
    if not a or not b:
        return False
    keys = ("scope", "account_filter", "status_filter", "population_meaning")
    return all(a.get(k) == b.get(k) for k in keys)


def classify_broker_count_corroboration(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    parsed_orders: int,
    pending_non_orders: int,
) -> dict[str, Any]:
    """Classify broker pending-count evidence against P+N.

    Equal counts are corroborative and never prove set membership.
    A missing count is not a contradiction.
    """
    expected = parsed_orders + pending_non_orders
    b0 = None if not before or before.get("unavailable") else before.get("value")
    b1 = None if not after or after.get("unavailable") else after.get("value")

    result: dict[str, Any] = {
        "classification": "unavailable",
        "b0": b0,
        "b1": b1,
        "expected_p_plus_n": expected,
        "comparable": True,
        "blocks_completeness": False,
        "severity": None,
        "reason_codes": [],
    }

    if before and after and not counts_comparable(before, after):
        result["comparable"] = False
        result["classification"] = "incomparable"
        result["reason_codes"].append("broker_count_populations_incomparable")
        return result

    if b0 is not None and b1 is not None and b0 != b1:
        result["classification"] = "observed_churn"
        result["blocks_completeness"] = True
        result["severity"] = "blocker"
        result["reason_codes"].append("broker_pending_count_changed_during_scan")
        return result

    # Prefer the post-traversal reading when present; else the pre-traversal one.
    observed = b1 if b1 is not None else b0
    if observed is None:
        result["classification"] = "unavailable"
        result["reason_codes"].append("broker_pending_count_unavailable")
        return result

    if b0 is None or b1 is None:
        result["classification"] = "partial"
        result["reason_codes"].append("broker_pending_count_partial")

    if observed != expected:
        result["classification"] = "contradiction"
        result["blocks_completeness"] = True
        if observed > expected:
            result["severity"] = "blocker"
            result["reason_codes"].append("broker_pending_count_exceeds_parsed")
        else:
            result["severity"] = "warning"
            result["reason_codes"].append("broker_pending_count_below_parsed")
        return result

    if result["classification"] == "partial":
        return result
    result["classification"] = "agreement"
    result["reason_codes"].append("broker_pending_count_agrees")
    return result


OPEN_STATUS_TOKENS = {"Pending", "Partial", "Cancelling", "Cancel requested"}
TERMINAL_STATUS_TOKENS = {"Completed", "Filled", "Completed/Filled", "Cancelled", "Expired", "Rejected", "Failed"}


def is_status_token_free_trade_row(row: dict[str, Any]) -> bool:
    """Trade-shaped browser row carrying neither an open nor a terminal status token."""
    if row.get("side") not in {"buy", "sell"}:
        return False
    status = row.get("status")
    if status in OPEN_STATUS_TOKENS or status in TERMINAL_STATUS_TOKENS:
        return False
    # Explicit collapsed marker used by this collector.
    return status in {None, "", "Status unconfirmed"} or status is None


def classify_status_token_free_rows(
    browser_rows: list[dict[str, Any]],
    activity_source_reconciliation: dict[str, Any] | None,
    *,
    fresh_export: bool,
) -> dict[str, Any]:
    """Three-way terminal classification for status-token-free trade rows.

    Buckets: export_corroborated_terminal, proven_pending_order,
    unresolved_review_required. Fail closed when status-unknown rows exist and
    no fresh corroborating export ran. U3ᵤ counts only review-required rows
    (and the fail-closed missing-export case), not T+1 recent unsettled rows.
    """
    status_unknown = [row for row in browser_rows if is_status_token_free_trade_row(row)]
    recon = activity_source_reconciliation or {}

    if not fresh_export and status_unknown:
        return {
            "status_unknown_row_count": len(status_unknown),
            "export_corroborated_terminal": [],
            "proven_pending_order": [],
            "unresolved_review_required": list(status_unknown),
            "u3_unresolved_count": len(status_unknown),
            "corroboration_ran": False,
            "fail_closed_missing_export": True,
            "reason_codes": ["missing_fresh_activity_export_for_status_unknown_rows"],
        }

    review_required_ids = {
        row.get("source_control_id")
        for row in (recon.get("browser_only_review_required") or [])
        if row.get("source_control_id")
    }
    recent_ids = {
        row.get("source_control_id")
        for row in (recon.get("browser_only_recent_unsettled") or [])
        if row.get("source_control_id")
    }

    export_corroborated: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    proven_pending: list[dict[str, Any]] = []
    recent_unsettled: list[dict[str, Any]] = []

    for row in status_unknown:
        key = row.get("source_control_id") or row.get("stable_row_key")
        if key and key in review_required_ids:
            unresolved.append(row)
        elif key and key in recent_ids:
            recent_unsettled.append(row)
        else:
            export_corroborated.append(row)

    return {
        "status_unknown_row_count": len(status_unknown),
        "export_corroborated_terminal": export_corroborated,
        "proven_pending_order": proven_pending,
        "unresolved_review_required": unresolved,
        "recent_may_be_unsettled": recent_unsettled,
        "u3_unresolved_count": len(unresolved),
        "corroboration_ran": bool(fresh_export),
        "fail_closed_missing_export": False,
        "reason_codes": (
            ["status_unknown_rows_unresolved_after_reconciliation"] if unresolved else []
        ),
    }


def derive_sell_coverage_scope(
    *,
    reset_attempted: bool,
    reset_click_succeeded: bool,
    filter_default_before: bool | None,
    filter_default_after: bool | None,
    inventory_complete: bool,
) -> str:
    """Compose manifest scope text from recorded observations only."""
    parts: list[str] = ["captured all-status Activity inventory"]
    if reset_attempted and reset_click_succeeded:
        parts.append("filter Clear action was performed")
    elif reset_attempted and not reset_click_succeeded:
        parts.append("filter Clear action was attempted but did not succeed")
    else:
        parts.append("no filter Clear action was performed")
    if filter_default_before is True:
        parts.append("defaults observed before traversal")
    elif filter_default_before is False:
        parts.append("defaults not confirmed before traversal")
    else:
        parts.append("pre-traversal filter state not_observed_historically")
    if filter_default_after is True:
        parts.append("defaults re-verified after traversal")
    elif filter_default_after is False:
        parts.append("defaults changed or failed after traversal")
    else:
        parts.append("post-traversal filter state not_observed_historically")
    if inventory_complete:
        parts.append("scoped observed open-order inventory assessed complete")
    else:
        parts.append("scoped observed open-order inventory not assessed complete")
    parts.append("not an atomic broker snapshot")
    return "; ".join(parts)


def assess_inventory_completeness(
    *,
    filter_default_before: bool | None,
    filter_default_after: bool | None,
    traversal_exhausted: bool,
    unparsed_pending_count: int,
    parsed_orders: int,
    detail_confirmed_orders: int,
    pending_non_orders: int,
    unresolved_orders: int,
    status_unknown_classification: dict[str, Any],
    fresh_export: bool,
    blockers: list[str],
    broker_count_before: dict[str, Any] | None = None,
    broker_count_after: dict[str, Any] | None = None,
    historical_missing_observations: list[str] | None = None,
) -> dict[str, Any]:
    """Reason-carrying completeness record. Consumers requiring completeness treat unverified as false."""
    reasons: list[str] = []
    missing = list(historical_missing_observations or [])

    if filter_default_before is None:
        missing.append("pre_traversal_filter_state")
        reasons.append("missing_historical_pre_traversal_scope_verification")
    elif filter_default_before is not True:
        reasons.append("filter_defaults_not_proven_before_traversal")

    if filter_default_after is None:
        missing.append("post_traversal_filter_state")
        reasons.append("missing_historical_post_traversal_scope_verification")
    elif filter_default_after is not True:
        reasons.append("filter_defaults_not_proven_after_traversal")

    if not traversal_exhausted:
        reasons.append("traversal_not_exhausted")
    if unparsed_pending_count:
        reasons.append("unparsed_pending_controls_present")
    if unresolved_orders:
        reasons.append("unresolved_row_only_orders_present")
    if detail_confirmed_orders != parsed_orders:
        reasons.append("detail_confirmation_incomplete")
    if blockers:
        reasons.append("capture_blockers_present")

    u3 = int(status_unknown_classification.get("u3_unresolved_count") or 0)
    status_unknown_count = int(status_unknown_classification.get("status_unknown_row_count") or 0)
    if u3:
        reasons.append("status_unknown_rows_unresolved_after_reconciliation")
    if status_unknown_count and not fresh_export:
        reasons.append("status_unknown_rows_without_fresh_export")
    reasons.extend(status_unknown_classification.get("reason_codes") or [])

    count_eval = classify_broker_count_corroboration(
        broker_count_before,
        broker_count_after,
        parsed_orders=parsed_orders,
        pending_non_orders=pending_non_orders,
    )
    if count_eval.get("blocks_completeness"):
        reasons.extend(count_eval.get("reason_codes") or [])

    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered_reasons = []
    for code in reasons:
        if code not in seen:
            seen.add(code)
            ordered_reasons.append(code)

    inventory_complete = not ordered_reasons and not missing
    completeness_state = (
        "complete" if inventory_complete
        else ("unverified" if missing and not any(
            r for r in ordered_reasons
            if r not in {
                "missing_historical_pre_traversal_scope_verification",
                "missing_historical_post_traversal_scope_verification",
            }
        ) or (missing and all(
            r.startswith("missing_historical_") for r in ordered_reasons
        )) else "incomplete")
    )
    if missing and completeness_state != "complete":
        # Prefer unverified when the only gaps are historically absent observations
        # and no positive contradiction was demonstrated.
        contradiction_codes = {
            "broker_pending_count_exceeds_parsed",
            "broker_pending_count_below_parsed",
            "broker_pending_count_changed_during_scan",
            "filter_defaults_not_proven_before_traversal",
            "filter_defaults_not_proven_after_traversal",
            "traversal_not_exhausted",
            "unparsed_pending_controls_present",
            "unresolved_row_only_orders_present",
            "detail_confirmation_incomplete",
            "capture_blockers_present",
            "status_unknown_rows_unresolved_after_reconciliation",
            "status_unknown_rows_without_fresh_export",
            "missing_fresh_activity_export_for_status_unknown_rows",
        }
        if not (set(ordered_reasons) & contradiction_codes):
            completeness_state = "unverified"

    return {
        "inventory_complete": bool(inventory_complete),
        "completeness_state": completeness_state,
        "reason_codes": ordered_reasons,
        "filter_observed_default_before": filter_default_before,
        "filter_observed_default_after": filter_default_after,
        "post_traversal_filter_state": (
            True if filter_default_after is True
            else False if filter_default_after is False
            else "not_observed_historically"
        ),
        "pre_traversal_filter_state": (
            True if filter_default_before is True
            else False if filter_default_before is False
            else "not_observed_historically"
        ),
        "traversal_exhausted": traversal_exhausted,
        "parsed_pending_orders": parsed_orders,
        "detail_confirmed_orders": detail_confirmed_orders,
        "pending_non_order_activities": pending_non_orders,
        "unparsed_pending_count": unparsed_pending_count,
        "unresolved_row_only_orders": unresolved_orders,
        "status_unknown_row_count": status_unknown_count,
        "u3_unresolved_count": u3,
        "fresh_corroborating_export": fresh_export,
        "broker_count_before": broker_count_before,
        "broker_count_after": broker_count_after,
        "count_corroboration": count_eval,
        "historical_missing_observations": missing,
        "status_unknown_classification": {
            "export_corroborated_terminal_count": len(
                status_unknown_classification.get("export_corroborated_terminal") or []
            ),
            "proven_pending_order_count": len(
                status_unknown_classification.get("proven_pending_order") or []
            ),
            "unresolved_review_required_count": u3,
            "corroboration_ran": status_unknown_classification.get("corroboration_ran"),
            "fail_closed_missing_export": status_unknown_classification.get(
                "fail_closed_missing_export"
            ),
        },
    }


def residual_disclosure_rows(
    total_value_reconciliation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every account with a nonzero residual, regardless of severity grade."""
    rows = []
    for account, values in total_value_reconciliation.items():
        if not isinstance(values, dict):
            continue
        if "residual_cad" not in values:
            continue
        try:
            residual = float(values["residual_cad"])
        except (TypeError, ValueError):
            continue
        if residual == 0:
            continue
        rows.append({
            "account": account,
            "residual_cad": residual,
            "status": values.get("status"),
            "classification": (values.get("residual_analysis") or {}).get("classification"),
            "note": values.get("note"),
        })
    return rows


def format_residual_warning(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    parts = [
        f"{r['account']} (residual {r['residual_cad']} CAD, status {r.get('status')}, "
        f"classification {r.get('classification')})"
        for r in rows
    ]
    return (
        "account total residual unexplained by visible cash, holdings, and open-buy "
        "commitments for: " + ", ".join(parts)
    )
