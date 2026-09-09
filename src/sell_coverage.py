"""Read-only, evidence-qualified quantity coverage; never a trading instruction."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any, Callable


OPEN_STATUSES = frozenset({
    "pending", "open", "new", "partially filled", "partially executed",
    "pending cancellation", "pending cancel", "cancel requested",
    "cancellation requested", "pending replace", "pending replacement",
})
TERMINAL_STATUSES = frozenset({
    "cancelled", "canceled", "expired", "rejected", "failed", "filled",
    "completed", "completed/filled", "done for day", "done for the day",
})


def order_state(value: Any) -> str:
    status = str(value or "").strip().casefold()
    if status in OPEN_STATUSES:
        return "open"
    return "terminal" if status in TERMINAL_STATUSES else "unknown"


def open_status_from_lines(lines: list[str]) -> str | None:
    # Do not let an incidental Pending label override an explicit terminal state.
    if any(order_state(line) == "terminal" for line in lines):
        return None
    return next((line for line in lines if order_state(line) == "open"), None)


def quantity(value: Any) -> Decimal | None:
    """Strict nonnegative share units. Missing, malformed and nonfinite != zero."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if len(text) > 100:
        return None
    match = re.fullmatch(r"((?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)(?:\s+shares?)?", text)
    if not match:
        return None
    try:
        return Decimal(match[1].replace(",", ""))
    except InvalidOperation:
        return None


def units(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def quantity_evidence(original: Any, filled: Any, remaining: Any) -> dict[str, Any]:
    """Normalize three separately observed labels from ONE order disclosure.

    Filled quantity may contain the UI's 'N shares x $price' display. Raw label
    values are retained. Never use the presence of Pending as evidence of zero
    fills. Versioned provenance prevents legacy entered-as-remaining promotion.
    """
    raw = {"original": original, "filled": filled, "remaining": remaining}
    filled_units = re.split(r"\s+[x×]\s+", filled, maxsplit=1)[0] if isinstance(filled, str) else filled
    values = {"original": quantity(original), "filled": quantity(filled_units), "remaining": quantity(remaining)}
    problems = [f"invalid_{key}_quantity" for key, val in raw.items() if val is not None and values[key] is None]
    a, b, c = (values[k] for k in ("original", "filled", "remaining"))
    with localcontext() as ctx:
        ctx.prec = 120
        if a is not None and b is not None and b > a:
            problems.append("filled_exceeds_original")
        if a is not None and c is not None and c > a:
            problems.append("remaining_exceeds_original")
        if all(v is not None for v in (a, b, c)) and a - b != c:
            problems.append("contradictory_quantities")
        basis = "unknown"
        if not problems:
            if c is not None:
                basis = "explicit_remaining"
            elif a is not None and b is not None:
                c = a - b
                basis = "original_minus_filled"
        if problems:
            c = None
    return {
        "original_quantity": units(a), "filled_quantity": units(b),
        "remaining_quantity": units(c), "remaining_quantity_basis": basis,
        "quantity_provenance": {
            "version": 1, "raw": raw, "problems": problems,
            "raw_labels": {key: label if raw[key] is not None else None for key, label in (
                ("original", "Entered quantity"), ("filled", "Filled quantity"),
                ("remaining", "Remaining quantity"))},
        },
    }


def verified_remaining(order: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    proof = order.get("quantity_provenance")
    if not isinstance(proof, dict) or proof.get("version") != 1 or not isinstance(proof.get("raw"), dict):
        return None, "remaining_quantity_unverified_or_legacy"
    raw = proof["raw"]
    recomputed = quantity_evidence(raw.get("original"), raw.get("filled"), raw.get("remaining"))
    if proof.get("raw_labels") != recomputed["quantity_provenance"]["raw_labels"]:
        return None, "quantity_label_provenance_mismatch"
    for key in ("original_quantity", "filled_quantity", "remaining_quantity", "remaining_quantity_basis"):
        if order.get(key) != recomputed[key]:
            return None, "quantity_provenance_mismatch"
    if recomputed["quantity_provenance"]["problems"]:
        return None, ",".join(recomputed["quantity_provenance"]["problems"])
    result = quantity(recomputed["remaining_quantity"])
    return result, None if result is not None else "remaining_quantity_unknown"


def coverage_quantity(order: dict[str, Any]) -> tuple[Decimal | None, str | None, str]:
    """Coverage convention: ordinary Pending uses its displayed entered size.

    This does not rewrite the raw remaining field or assert that null fills
    mean zero. Explicit quantity evidence takes priority.
    """
    remaining, problem = verified_remaining(order)
    if remaining is not None:
        return remaining, None, order['remaining_quantity_basis']
    if problem != 'remaining_quantity_unknown' or str(order.get('status', '')).strip().casefold() != 'pending':
        return None, problem, 'unknown'
    metadata = order.get('order_metadata') or {}
    record = metadata.get('record') or {}
    activity = metadata.get('activity') or {}
    states = [record.get('status'), activity.get('status'), activity.get('unifiedStatus')]
    if any(s and any(t in str(s).casefold() for t in ('partial', 'fill', 'execut', 'cancel', 'replac', 'reject', 'expir', 'posted')) for s in states):
        return None, 'pending_quantity_conflicting_status', 'unknown'
    for field in ('fillQuantity', 'filledValue', 'averageFillPrice', 'lastFilledAtUtc'):
        value = record.get(field)
        if value is not None and quantity(value) != Decimal(0):
            return None, 'pending_quantity_fill_evidence', 'unknown'
    original = quantity(order.get('original_quantity'))
    if record.get('submittedQuantity') is not None and quantity(record['submittedQuantity']) != original:
        return None, 'pending_quantity_submitted_conflict', 'unknown'
    return original, None if original is not None else 'original_quantity_unknown', 'pending_entered_quantity'


def safe_quantity_record(order: dict[str, Any]) -> dict[str, Any]:
    """Do not re-export a legacy/copied remaining field as if it were evidence."""
    row = dict(order)
    remaining, problem = verified_remaining(row)
    if remaining is None and row.get("remaining_quantity") is not None:
        row["unverified_remaining_quantity_raw"] = row["remaining_quantity"]
        row["remaining_quantity"] = None
        row["remaining_quantity_basis"] = "unknown"
        row["quantity_validation_problem"] = problem
    return row


def _quote_currency(row: dict[str, Any]) -> str | None:
    # Settlement currency is NOT security identity (USD quotes may settle CAD).
    return row.get("current_price_currency") or row.get("security_quote_currency")


def _identity_problem(holding: dict[str, Any], order: dict[str, Any]) -> str | None:
    same_id = any(holding.get(k) and holding[k] == order.get(k) for k in ("security_id", "isin"))
    for field in ("security_id", "isin", "exchange"):
        if holding.get(field) and order.get(field) and holding[field] != order[field]:
            return "instrument_identity_conflict"
    hc, oc = _quote_currency(holding), _quote_currency(order)
    if hc and oc and hc != oc:
        return "quote_currency_conflict"
    if not same_id and not (hc and hc == oc):
        return "instrument_quote_currency_unverified"
    hclass, oclass = holding.get("classification"), order.get("classification")
    if hclass and oclass and "unknown" not in hclass and "unknown" not in oclass and hclass != oclass:
        return "security_class_conflict"
    if not same_id and hclass == "CDR" and oclass != "CDR":
        return "cdr_order_identity_unverified"
    return None


def sell_coverage(
    holdings: list[dict[str, Any]], orders: list[dict[str, Any]],
    normalize: Callable[[str | None], str | None], *, inventory_complete: bool = False,
) -> list[dict[str, Any]]:
    """One summary per captured holding group plus compatible excess findings.

    Caller must explicitly assess completeness for live/rebuilt inventories.
    Missing completeness evidence defaults to uncertainty, including fixtures.
    Decimal strings preserve exact share units in JSON without float rounding.
    """
    hs: dict[tuple, list] = defaultdict(list)
    sells: dict[tuple, list] = defaultdict(list)
    for h in holdings:
        hs[(h.get("account"), normalize(h.get("ticker")))].append(h)
    for o in orders:
        if o.get("side") in {"sell", "unknown", None} and order_state(o.get("status")) != "terminal":
            sells[(o.get("account"), normalize(o.get("ticker")))].append(o)
    findings = []
    with localcontext() as ctx:
        ctx.prec = 140
        for key in sorted(hs.keys() | sells.keys(), key=str):
            group, candidates = hs.get(key, []), sells.get(key, [])
            reasons = [] if inventory_complete else ["open_order_inventory_incomplete_or_unverified"]
            if not key[0] or not key[1]:
                reasons.append("missing_account_or_security")
            if len(group) != 1:
                reasons.append("holding_missing_or_ambiguous")
            holding = group[0] if len(group) == 1 else {}
            held = quantity(holding.get("quantity"))
            if held is None:
                reasons.append("holding_quantity_unknown")
            if any(o.get("side") in {"sell", "unknown", None} and order_state(o.get("status")) != "terminal" and
                   ((not o.get("account") and normalize(o.get("ticker")) in {None, key[1]})
                    or (o.get("account") == key[0] and not normalize(o.get("ticker"))))
                   for o in orders):
                reasons.append("unidentified_order_in_account")
            seen: dict[str, dict] = {}
            conflicts: set[str] = set()
            unique = []
            for o in candidates:
                # Text hashes cannot distinguish identical real ladder legs.
                oid = o.get("order_id") or o.get("source_control_id")
                if oid and oid in seen:
                    comparable = ("account", "ticker", "side", "status", "original_quantity", "filled_quantity",
                                  "remaining_quantity", "quantity_provenance", "limit_price", "security_quote_currency",
                                  "security_id", "isin", "classification", "exchange", "oco_group", "linked_order_id", "parent_order_id")
                    if any(o.get(k) != seen[oid].get(k) for k in comparable):
                        reasons.append("conflicting_duplicate_order")
                        conflicts.add(oid)
                    continue
                if oid:
                    seen[oid] = o
                unique.append(o)
            total = Decimal(0)
            known_count = 0
            bases = set()
            excess_orders = []
            for o in unique:
                identity_problem = _identity_problem(holding, o) if holding else "holding_identity_unknown"
                problems = [identity_problem] if identity_problem else []
                oid = o.get("order_id") or o.get("source_control_id")
                if oid in conflicts or (not oid and len(candidates) > 1):
                    reasons.append("order_identity_ambiguous")
                    continue
                if o.get("side") != "sell":
                    problems.append("order_side_unknown")
                if order_state(o.get("status")) != "open":
                    problems.append("order_status_unknown")
                if any(o.get(k) for k in ("oco_group", "linked_order_id", "parent_order_id")):
                    problems.append("linked_order_semantics_unknown")
                remaining, qty_problem, basis = coverage_quantity(o)
                if qty_problem:
                    problems.append(qty_problem)
                if problems:
                    reasons.extend(problems)
                    continue
                if not key[0] or not key[1]:
                    continue
                total += remaining
                known_count += 1
                bases.add(basis)
                if held is not None and remaining > held:
                    excess_orders.append({
                        "type": "open_sell_exceeds_visible_holding", "severity": "warning",
                        "account": key[0], "ticker": key[1],
                        "holding_quantity": units(held), "order_quantity": units(remaining),
                        "quantity_basis": basis,
                        "order_reference": oid,
                    })
            uncertain = bool(reasons)
            excess = held is not None and total > held
            status = "excess_open_sells" if excess else "uncertain" if uncertain else (
                "fully_covered" if total == held else "uncovered" if total == 0 else "partially_uncovered"
            )
            kind = {"excess_open_sells": "aggregate_open_sells_exceed_visible_holding",
                    "uncertain": "coverage_uncertain", "fully_covered": "fully_sell_covered",
                    "uncovered": "no_open_sell_coverage", "partially_uncovered": "partial_sell_coverage"}[status]
            findings.extend(excess_orders)
            if not group:
                findings.append({"type": "open_sell_without_visible_holding", "severity": "warning",
                                 "account": key[0], "ticker": key[1], "order_quantity": None})
            findings.append({
                "type": kind, "severity": "warning" if excess or uncertain else "info",
                "account": key[0], "ticker": holding.get("ticker") or key[1],
                "coverage_status": status, "holding_quantity": units(held),
                "open_sell_orders_found": sum(o.get("side") == "sell" for o in unique),
                "known_remaining_order_count": known_count,
                "coverage_quantity_bases": sorted(bases),
                "known_open_sell_remaining": units(total),
                "open_sell_remaining": None if uncertain else units(total),
                "uncovered_quantity": units(max(held - total, Decimal(0))) if not uncertain else None,
                "excess_quantity_lower_bound": units(total - held) if excess else None,
                "aggregate_order_quantity": units(total) if excess else None,
                "uncertainty_reasons": sorted(set(reasons)),
                "scope": "captured_holdings_and_open_orders_not_atomic",
            })
    return findings


def render_coverage(rows: list[dict[str, Any]]) -> str:
    lines = ["## Open-sell quantity coverage", "",
             "Informational inventory comparison, not a recommendation or guaranteed exit. "
             "Capture is not atomic. Unknown quantities are not zero.", ""]
    lines += ["Ordinary Pending orders use displayed entered quantities for coverage unless partial-fill or conflicting evidence is present. Explicit remaining/fill evidence takes priority.", ""]
    for row in rows:
        if "coverage_status" not in row:
            if row.get("type") == "open_sell_exceeds_visible_holding":
                lines.append(f"- {row.get('account')} {row.get('ticker')}: order {row.get('order_reference') or 'unidentified'} "
                             f"has {row.get('order_quantity')} coverage units ({row.get('quantity_basis', 'verified_remaining')}) versus {row.get('holding_quantity')} held.")
            elif row.get("type") == "open_sell_without_visible_holding":
                lines.append(f"- {row.get('account')} {row.get('ticker')}: sell found without a uniquely visible holding; not proof of an unbacked sale.")
            continue
        def show(key: str) -> str:
            value = row.get(key)
            return "unknown" if value is None else str(value)
        lines.append(
            f"- {row.get('account')} {row.get('ticker')}: held {show('holding_quantity')}; "
            f"open sell orders found {show('open_sell_orders_found')}; {'pending sell quantity' if 'pending_entered_quantity' in row.get('coverage_quantity_bases', []) else 'remaining sell quantity'} "
            f"{show('open_sell_remaining')}; coverage **{row['coverage_status']}**; "
            f"uncovered {show('uncovered_quantity')}."
        )
        if row.get("uncertainty_reasons"):
            lines.append("  Reasons: " + ", ".join(row["uncertainty_reasons"]) + ".")
            if row.get('known_remaining_order_count', 0):
                lines.append(f"  Captured orders with usable quantities total {show('known_open_sell_remaining')} shares; the complete coverage assessment remains qualified by the reasons above.")
        if row.get("excess_quantity_lower_bound") is not None:
            lines.append(f"  Known excess open-sell units: at least {row['excess_quantity_lower_bound']}; not an executed oversale.")
    if not rows:
        lines.append("- Not assessed: no usable holdings evidence.")
    return "\n".join(lines) + "\n"
