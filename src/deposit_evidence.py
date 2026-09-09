"""Deposit availability is evidence, not an automatic residual adjustment."""
import re
from decimal import Decimal


def parse_deposit_availability(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    def after(label):
        if lines.count(label) != 1:
            return None
        i = lines.index(label)
        return lines[i + 1] if i + 1 < len(lines) else None

    amount = after("Amount")
    total = re.fullmatch(r"\$([\d,]+\.\d{2}) CAD", amount or "")
    instant = None
    if lines.count("Available to trade instantly") == 1:
        start = lines.index("Available to trade instantly") + 1
        end = lines.index("Amount") if lines.count("Amount") == 1 else start
        values = [re.fullmatch(r"\$([\d,]+\.\d{2})(?: CAD)?", s) for s in lines[start:end]]
        values = [v for v in values if v]
        if len(values) == 1:
            instant = Decimal(values[0][1].replace(",", ""))
    total = Decimal(total[1].replace(",", "")) if total else None
    valid = total is not None and instant is not None and 0 <= instant <= total
    return {
        "currency": "CAD" if total is not None else None,
        "status": lines[3] if len(lines) > 3 and lines[:2] == ["Deposit", "Electronic funds transfer"] else None,
        "account": after("To"), "deposit_date": after("Date"),
        "estimated_completion": after("Estimated completion"),
        "deposit_amount_cad": str(total) if total is not None else None,
        "instant_available_cad": str(instant) if instant is not None else None,
        "amount_not_instantly_available_cad": str(total - instant) if valid else None,
        "availability_evidence_valid": valid,
        "interpretation": "Difference from initial instant availability, not a verified current hold or an automatic account-residual adjustment.",
    }


def render_deposits(rows):
    if not rows:
        return []
    lines = ["", "Observed pending-deposit availability (read-only disclosures):"]
    for r in rows:
        lines.append(f"- {r.get('account')}: deposit CAD {r.get('deposit_amount_cad')}; "
                     f"instant availability CAD {r.get('instant_available_cad')}; "
                     f"difference CAD {r.get('amount_not_instantly_available_cad')}; "
                     f"estimated completion {r.get('estimated_completion')}; observed {r.get('observed_at')}. "
                     f"Evidence: {r.get('evidence_file')}. {r.get('interpretation')}")
    lines.append("Do not silently subtract these differences from residuals. Capture times differ; release status and account-value inclusion need separate evidence. Unknown values are not zero.")
    return lines


def compare_deposit_residuals(reconciliations, deposits, *, inventory_complete=False):
    """Add an interpretation, never mutate the original reconciliation.

    Multiple deposits are deliberately not summed without broker identities:
    doing so could double count repeated observations or incomplete evidence.
    """
    results = []
    for account, reconciliation in reconciliations.items():
        candidates = [d for d in deposits if d.get("account") == account]
        if not candidates:
            continue
        result = {"account": account, "currency": "CAD",
                  "assessment": "comparison_unavailable",
                  "original_residual_cad": reconciliation.get("residual_cad"),
                  "deposit_gap_cad": None, "residual_after_interpretation_cad": None,
                  "broker_confirmed_current_hold": False,
                  "caveat": "Inferred pending-deposit explanation using non-atomic captured data, not broker confirmation of a current hold. Original residual and warning remain unchanged."}
        reason = None
        if not inventory_complete:
            reason = "incomplete_order_inventory"
        elif len(candidates) != 1:
            reason = "multiple_deposits_not_uniquely_resolved"
        elif reconciliation.get("components_captured") is not True:
            reason = "incomplete_account_components"
        else:
            d = candidates[0]
            result.update(observed_at=d.get("observed_at"), evidence_file=d.get("evidence_file"))
            if (d.get("currency") != "CAD" or d.get("status") not in {"Pending", "In progress"}
                    or d.get("availability_evidence_valid") is not True
                    or not all(d.get(k) for k in ("observed_at", "evidence_file", "deposit_date", "estimated_completion"))):
                reason = "incomplete_or_nonpending_deposit_evidence"
            else:
                def number(v, signed=False):
                    s = str(v)
                    pattern = r"-?\d{1,15}(?:\.\d{1,2})?" if signed else r"\d{1,15}(?:\.\d{1,2})?"
                    return Decimal(s) if re.fullmatch(pattern, s) else None
                total = number(d.get("deposit_amount_cad"))
                instant = number(d.get("instant_available_cad"))
                recorded_gap = number(d.get("amount_not_instantly_available_cad"))
                residual = number(reconciliation.get("residual_cad"), signed=True)
                if None in (total, instant, recorded_gap, residual) or not 0 <= instant < total or total - instant != recorded_gap:
                    reason = "invalid_or_inconsistent_amounts"
                else:
                    difference = residual - recorded_gap
                    result.update(deposit_gap_cad=format(recorded_gap, '.2f'),
                                  residual_after_interpretation_cad=format(difference, '.2f'),
                                  assessment="exact_numeric_match" if difference == 0 else "remaining_difference")
        result["unavailable_reason"] = reason
        results.append(result)
    return results


def render_deposit_comparisons(rows):
    if not rows:
        return []
    lines = ["", "Pending-deposit interpretation of account residuals:"]
    for r in rows:
        assessment = ("Numerically reconciled under the pending-deposit interpretation"
                      if r['assessment'] == 'exact_numeric_match' else
                      "Remaining difference under the pending-deposit interpretation"
                      if r['assessment'] == 'remaining_difference' else
                      "Comparison unavailable: " + str(r.get('unavailable_reason')))
        lines.append(f"- {r['account']}: original residual CAD {r['original_residual_cad']}; "
                     f"candidate deposit gap CAD {r['deposit_gap_cad']}; "
                     f"residual after interpretation CAD {r['residual_after_interpretation_cad']}. "
                     f"{assessment}. {r['caveat']}")
    return lines
