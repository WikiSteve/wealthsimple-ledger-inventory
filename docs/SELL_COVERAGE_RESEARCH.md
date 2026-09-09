# Open-sell quantity coverage

## Final policy update

This document preserves the initial research and stricter first implementation
below. The current reporting policy is documented in README.md: ordinary Pending
orders use displayed entered quantities for coverage unless partial-fill or
conflicting evidence is present. The basis is explicitly recorded as
`pending_entered_quantity`; raw remaining/fill evidence is not overwritten.
Explicit remaining or visible original-minus-filled evidence takes priority.
Legacy provenance, ambiguous identity and incomplete inventory still fail closed.
This is a reporting convention, not a newly proven broker lifecycle guarantee.
The final suite passes 281 tests, and the production capture path was smoke-tested
against two live Pending orders. No partial fill was manufactured or observed.

## Conclusion

The existing audit has a confirmed partial-coverage reporting gap and an
additional capture-model assumption. The correct repair must address both.
This review does not certify any particular account balance, holding, or order
book. Those require their own time-aligned source evidence.

The quantity-provenance and coverage repair is now implemented in
`src/sell_coverage.py`, integrated with capture, parsing, bundle output and
browser-free rebuild. The findings below describe the pre-fix implementation.
Regression evidence is in `tests/test_sell_coverage.py`. The tests use synthetic
disclosure shapes and the real parser/report path; they do not claim a live
partially filled order was observed during this repair.
Source pages and local implementation were inspected on September 9, 2026.

## Implementation verification

The full offline suite passes 227 tests (177 before this repair). Fifty new
tests cover the quantity and identity contract, real parser-to-bundle rendering,
legacy rebuild uncertainty, same-notional ladder legs, unparsed partial-status
cards, scan exhaustion, failed filter reset, and final-viewport capture. Python
compilation and `git diff --check` also pass. No fresh account capture or trade
was run as part of this repair; runtime and live partial-fill layout validation
are not claimed.

Independent red-team review prompted safe defaults, label-source provenance,
missing-account handling and stricter duplicate handling. Parser evidence also
corrected two review assumptions: explicitly displayed remaining may equal
entered with no filled label, and the existing order-currency field sometimes
comes from the limit quote, sometimes from estimated settlement. A separate
`security_quote_currency` field now avoids that ambiguity. A second code review
caught final-viewport omission and same-notional detail reuse; both have tests.

## External evidence

Wealthsimple explicitly documents partially filled orders: only part of the
requested quantity has traded, and cancellation affects the unfilled portion.
Consequently original order size is not a reliable measure of outstanding
sell coverage.[1]

The FIX specification defines LeavesQty as quantity available for further
execution. For active orders it generally equals OrderQty minus CumQty.
Terminal states require separate treatment; an expired or cancelled order must
not contribute coverage merely because arithmetic leaves an unfilled balance.
FIX provides a terminology and state-model reference here, not proof that the
Wealthsimple browser exposes FIX fields or uses a particular backend.[2]

Pending cancellation is not completed cancellation. FIX's state examples show
executions occurring while cancellation is pending. Excluding every status
containing the word "cancel" would therefore be unsafe.[3]

Wealthsimple also documents product-specific expiry for partially filled
stop-limit orders. The report must use observed current order state, not infer
that a displayed original expiry guarantees the remainder is still open.[4]

Ticker matching needs instrument identity, not just company identity. CIBC
distinguishes its CDRs from underlying shares and defines a CDR ratio. Combining
them one-for-one after stripping a listing suffix would be incorrect.[5]

## Confirmed local implementation findings

In `src/full_account_inventory.py`:

- `paired_exit_checks` constructs a set of account/ticker pairs with any sell.
  Its missing-exit finding is restricted to `SPECIAL_TICKERS` and is boolean.
- `filled_buy_exit_checks` also treats any sell for the pair as an exit.
- `sell_order_coverage` sums `quantity` and checks individual and aggregate
  excess, but never reports under-coverage. Its arithmetic does not consume
  `remaining_quantity` and it does not independently filter terminal states.
- `parse_order_detail_blocks` reads `Entered quantity`, sets `filled_quantity`
  to null, and copies the entered quantity into `remaining_quantity`.
  Thus changing the coverage consumer to the latter field alone is insufficient.
- `parse_order_row_text` requires the literal `Pending` line. Wealthsimple
  documents a `Partially filled` status; whether the live page also supplies
  `Pending` for such cards remains unverified. Partial-order discovery needs a
  captured shape or explicit uncertainty, not an invented UI contract.
- `normalize_ticker_for_cross_reference` strips selected exchange suffixes and
  preserves share classes. It is useful alias handling but is not, by itself,
  proof that two records denote the same instrument.
- The holdings lookup in `sell_order_coverage` is a dictionary comprehension:
  duplicate normalized keys overwrite rather than aggregate. Multiple lots,
  duplicate captures and instrument collisions must be distinguished before
  changing that behavior.

A synthetic position of 12 shares with two sell orders of 3 and 2 produces no
coverage finding today. It should yield 7 uncovered only when 3 and 2 are
verified outstanding quantities, not merely entered sizes.

## Implemented reporting contract

For each unique account and resolved instrument, compare current held units
with the sum of remaining units on unique, applicable, nonterminal sell orders.
Use exact decimal quantities. Preserve security class and account isolation;
do not merge a CAD CDR with a USD underlying share.

| Evidence and comparison | Output |
| --- | --- |
| Complete evidence, remaining sells equal held units | Fully quantity-covered |
| Complete evidence, remaining sells below held units | Informational uncovered units; distinguish zero from partial coverage |
| Known remaining sells exceed held units | Open-sell quantity exceeds visible holding; not a claim an oversale executed |
| Missing/conflicting quantities, identity, status, or incomplete capture | Coverage uncertain; no invented exact uncovered amount |

Known coverage is a lower bound when additional sell quantities are unknown.
It can still prove excess when that lower bound alone exceeds holdings, but
cannot prove exact coverage or an exact shortfall. Conditional/linked orders
need their own treatment if encountered; do not sum alternatives as independent
commitments without evidence. A limit sell is not guaranteed to fill, so the
term "covered" means quantity associated with orders, not guaranteed protection.

## Quantity provenance

Prefer explicitly observed remaining quantity. Alternatively derive it from
explicitly observed original and cumulative filled quantities for the same
order state. Retain those inputs and the derivation label. Zero is valid data;
missing is not zero. Reject negative, nonfinite, malformed and contradictory
values. An original size with no fill evidence is insufficient.

Older bundles cannot be upgraded merely by trusting their existing
`remaining_quantity`: that field may be the parser's entered-size copy.
Reparse saved disclosure evidence where available, or mark uncertain. Introduce
quantity provenance so new and legacy records can be distinguished reliably.

Do not infer account correctness from this repair. Browser orders and holdings
exports may represent different instants. A mismatch can reflect intervening
fills or incomplete capture; report observation times and limitations.

## Acceptance tests before release

1. Multiple independent remaining sells aggregate within one account/instrument.
2. Partial fills use explicit remaining or validated original-minus-filled.
3. Entered quantity alone remains uncertain, including legacy parser records.
4. Cancelled, expired, rejected and filled terminal orders contribute nothing;
   pending cancellation is not silently treated as terminal.
5. Fractions and exact zero work; malformed/nonfinite/negative inputs fail closed.
6. Confirmed excess remains visible even with other unknown orders.
7. Account separation, instrument collisions and share classes stay distinct.
8. Duplicate orders are not double-counted; duplicated holdings are not silently
   overwritten or summed without identity evidence.
9. Actual parser-to-report tests cover partial-order card and disclosure shapes,
   not just hand-built dictionaries with trustworthy-looking field names.
10. Report output is informational for uncovered units and contains uncertainty
    when completeness or source timing prevents a trustworthy comparison.

## Scope and limitations

No browser session, order or financial account was modified. This investigation
does not establish the accuracy of a deposit-provenance explanation, individual
cash totals, current trade recommendations, earnings dates or price targets.
Public documentation establishes order semantics but cannot establish the
contents of a private account or undocumented browser field names.

## Sources

1. Wealthsimple. [View or cancel a pending market, stop-market, fractional, or stop-limit order](https://help.wealthsimple.com/hc/en-ca/articles/360056581374-View-or-cancel-a-pending-market-stop-market-fractional-or-stop-limit-order). Page dated August 5, 2026; cancellation details.
2. FIX Trading Community. [LeavesQty, field 151](https://fiximate.fixtrading.org/en/FIX.Latest/tag151.html). FIX Latest field definition; undated live reference.
3. FIX Trading Community. [Order State Changes](https://www.fixtrading.org/online-specification/order-state-changes/). Sections B.1.b and B.1.c; undated live specification.
4. Wealthsimple. [Understanding stop-limit orders](https://help.wealthsimple.com/hc/en-ca/articles/4413542667675-Understanding-stop-limit-orders). Partial-fill FAQ; live page.
5. CIBC. [CDR FAQ](https://cdr.cibc.com/en/faq) and [Charles Schwab CDR security details](https://cdr.cibc.com/en/cdr-directory/SCHW). Instrument/underlying distinction; security page updated September 8, 2026. No price or ratio is relied upon here.
