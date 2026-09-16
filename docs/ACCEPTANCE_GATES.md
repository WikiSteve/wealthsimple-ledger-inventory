# Acceptance Gates

- No forbidden actions clicked.
- All 3 accounts captured.
- All holdings captured.
- Pending orders captured from expanded Activity.
- Non-registered sells resolved when visible.
- Account balances not null.
- JSON output validates as parseable JSON.
- Totals computed by account.
- Diffs are explicit.
- PASS/WARN/FAIL/ABORT status emitted.
- Evidence files listed.
- Inventory completeness is evidence-based: filter defaults observed before and after traversal, traversal exhausted, every trade-shaped row placed, detail confirmation complete, and no applicable broker-count contradiction. Broker pending-count agreement is corroborative and never sufficient for set membership (enforced by `tests/test_inventory_completeness.py`).
- Missing broker counts are not contradictions; comparable contradictory counts block completeness.
- Every nonzero unexplained residual is disclosed in reconciliation JSON, warnings, account/all-account reports, and the ChatGPT handoff. Materiality controls severity only.
- Rebuilds reassess retained evidence and never manufacture historical observations.
