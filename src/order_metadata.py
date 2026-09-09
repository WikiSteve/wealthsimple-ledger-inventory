"""Optional, row-bound React identity evidence. Not a quantity authority."""
from __future__ import annotations

import json
from typing import Any


ACTIVITY_FIELDS = ("canonicalId", "externalCanonicalId", "accountId", "securityId",
                   "assetQuantity", "status", "unifiedStatus")
ORDER_FIELDS = ("id", "canonicalAccountId", "securityId", "side", "status",
                "submittedQuantity", "fillQuantity", "averageFillPrice", "filledValue",
                "lastFilledAtUtc", "submittedAtUtc", "securityCurrency")

# Called only with an exact, uniquely identified expanded Activity header.
# Never serialize an entire React fiber, query cache, identity or auth object.
METADATA_FUNCTION = r"""
function orderMetadata(header) {
  const activityFields = __ACTIVITY_FIELDS__;
  const orderFields = __ORDER_FIELDS__;
  const project = (o, keys) => Object.fromEntries(keys.filter(k => k in o)
    .filter(k => o[k] === null || ['string','number','boolean'].includes(typeof o[k]))
    .map(k => [k, o[k]]));
  if (!header) return {reason: 'exact_header_unavailable'};
  let fiber = header[Object.keys(header).find(k => k.startsWith('__reactFiber'))];
  let activity = null;
  for (let i=0; fiber && i<25; i++, fiber=fiber.return) {
    if (fiber.memoizedProps?.activity) {
      activity = project(fiber.memoizedProps.activity, activityFields); break;
    }
  }
  if (!activity) return {reason: 'activity_metadata_unavailable'};
  const records = new Map(), seen = new Set();
  const nodes = header.parentElement?.querySelectorAll('*') || [];
  if (nodes.length > 1500) return {reason: 'disclosure_scope_too_large'};
  for (const el of nodes) {
    let f=el[Object.keys(el).find(k => k.startsWith('__reactFiber'))];
    for (let i=0; f && i<8; i++, f=f.return) {
      if (seen.has(f)) break;
      seen.add(f);
      let h=f.memoizedState;
      for (let j=0; h && j<45; j++, h=h.next) {
        const o=h.memoizedState?.data?.orderServiceExtendedOrderByExternalId;
        if (o && typeof o === 'object') {
          const record=project(o, orderFields);
          records.set(JSON.stringify(record), record);
        }
      }
    }
  }
  return {activity, records: [...records.values()]};
}
""".replace("__ACTIVITY_FIELDS__", json.dumps(ACTIVITY_FIELDS)).replace(
    "__ORDER_FIELDS__", json.dumps(ORDER_FIELDS))


def validate_metadata(raw: Any) -> dict[str, Any]:
    """Revalidate exact three-way linkage; null and numeric fills stay raw."""
    result: dict[str, Any] = {"version": 1, "source": "rendered_react_order_record",
                              "identity_verified": False,
                              "quantity_semantics_verified": False}
    if not isinstance(raw, dict):
        return {**result, "reason": "metadata_unavailable"}
    def project(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {k: value[k] for k in fields if k in value
                and (value[k] is None or type(value[k]) in (str, int, float, bool))}
    activity = project(raw.get("activity"), ACTIVITY_FIELDS)
    records = raw.get("records")
    result["activity"] = activity
    if not isinstance(records, list) or not records or len(records) > 50:
        return {**result, "reason": "order_record_unavailable"}
    unique = {json.dumps(project(r, ORDER_FIELDS), sort_keys=True): project(r, ORDER_FIELDS)
              for r in records}
    if len(unique) != 1:
        return {**result, "reason": "order_record_ambiguous"}
    record = next(iter(unique.values()))
    result["record"] = record
    for a, b in (("externalCanonicalId", "id"), ("accountId", "canonicalAccountId"),
                 ("securityId", "securityId")):
        if (not isinstance(activity.get(a), str) or not activity[a].strip()
                or activity[a] != record.get(b)):
            return {**result, "reason": "identity_link_mismatch"}
    return {**result, "identity_verified": True, "reason": None}


def enrich_order(order: dict[str, Any], evidence: Any) -> dict[str, Any]:
    """Add identity only; never change entered, filled or remaining quantities."""
    raw = evidence if isinstance(evidence, dict) else {}
    # Do not trust a saved identity_verified flag without checking its inputs.
    checked = validate_metadata({"activity": raw.get("activity"),
                                 "records": [raw["record"]] if "record" in raw else []})
    checked["observed_at"] = raw.get("observed_at")
    checked["evidence_file"] = raw.get("evidence_file")
    enriched = dict(order)
    if raw.get("identity_verified") is not True:
        checked = {**raw, "identity_verified": False, "quantity_semantics_verified": False}
    if checked.get("identity_verified"):
        record = checked["record"]
        if record.get("side", "").lower() != order.get("side"):
            checked.update(identity_verified=False, reason="visible_side_mismatch")
        elif any(order.get(k) and order[k] != record[r]
                 for k, r in (("order_id", "id"), ("security_id", "securityId"))):
            checked.update(identity_verified=False, reason="existing_identity_conflict")
        else:
            enriched.update(order_id=record["id"], security_id=record["securityId"])
    enriched["order_metadata"] = checked
    return enriched
