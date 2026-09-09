# Targeted read-only Activity inspection — 2026-09-09

## Subsequent implementation update

The notes below describe the initial inspection, not the final implementation.
Serial capture now optionally links the exact Activity externalCanonicalId,
accountId and securityId to an order record, retaining whitelisted raw metadata.
This was live-tested on two distinct pending orders. Hidden fill quantities are
not used to derive remaining quantity. Ordinary Pending coverage now uses
displayed entered size under the documented reporting convention in README.md.
Partial-fill semantics and identity stability across replacements remain untested.
The complete suite now passes 281 tests.

## Verified improvement

The live Activity sidebar had no Clear button because its inspected controls
were at their defaults: all nine quick filters unpressed; Account, Type,
Holdings and Status checkboxes unchecked; Timeframe's `all` radio selected.
The collector previously required a successful Clear click and ignored this
alternative evidence. It now accepts the complete explicit default state.

The production `verify_activity_filter_defaults` method was exercised on the
authenticated Activity page, first with expanded sections and then with five
collapsed sections. Both returned true. The second invocation opened and
closed five disclosures (10 clicks), restoring their original expansion
states. No filter selections, account settings or orders were changed.
Private live evidence was retained locally and is intentionally not published.

This observation is current, not proof that an earlier capture had identical
state. Existing bundles are not retroactively upgraded. Complete order scope
still additionally requires the existing scan/detail/completeness gates.

## Quantity and identity findings

The inspected pending sell disclosure still exposed Entered quantity only.
Its DOM attributes included an accessibility panel ID, account link and
security link, but no established broker order ID or remaining quantity.
Accessibility IDs identify UI panels, not broker orders.

A bounded, read-only inspection of the React ancestors of the specific
Activity row found an `ActivityFeedItem` object with `canonicalId`,
`externalCanonicalId`, `securityId`, `assetQuantity`, `status`, and
`unifiedStatus`. The canonical ID had an `order-` prefix. The inspected pending
rows had SUBMITTED/PENDING states; assetQuantity matched entered quantity.

These are useful candidates for future capture provenance, not an established
remaining-quantity contract. We have not verified canonical-to-broker order
identity, stability across replacement/lifecycle changes, or the meaning of
assetQuantity after a partial fill. No filled-zero inference is justified.
The collector therefore does not consume React internals as verified order
identity or remaining quantity. Those internals are private and version-fragile.

No authenticated private API requests, order actions, full audit, or trading
operations were performed. Network-response schemas and other routes were not
exhaustively inspected. This is not a claim that Wealthsimple never exposes
remaining quantity elsewhere. Coverage remains uncertain where its provenance
is unknown. Holdings, cash and deposit provenance were not re-audited here.

## Regression coverage

Tests accept the complete explicit default state and reject collapsed/missing
sections, selected or missing checkbox evidence, active quick filters,
non-default timeframe, a search term, and a Clear control. Full suite: 239
passing tests after this change. The live method check above is separate from
the synthetic tests; no full capture was rerun.

No user action is required. A future ordinary capture will use the new filter
evidence check. Exact remaining-quantity reporting still needs independently
verified quantity semantics, ideally including a naturally occurring partial
fill; do not create a trade just to manufacture a fixture.
