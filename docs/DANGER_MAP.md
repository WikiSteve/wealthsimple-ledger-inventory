# Wealthsimple Ledger Inventory Danger Map

- READ ONLY.
- Do not create, stage, edit, cancel, or submit trades.
- Do not click final submit, cancel, modify, place, or confirm controls.
- Do not expose session cookies, auth tokens, credentials, or browser profile state.
- Do not pass secrets to sidecars.
- Do not rely on stale finance connector data.
- Do not treat a partial Activity list as complete until Load More or filtering has been exhausted.
- Direct account-detail URLs can render incomplete or redirect; prefer Home account-card click navigation.
- CDR versus U.S. common share mismatch is dangerous.
- Collapsed Activity rows prove existence/status/estimated total only. They do not prove quantity, limit, expiry, or submitted time.
- A collapsed Activity row carrying **no** status token proves nothing on its own. It is resolved only by source reconciliation against a fresh Activity export (export-corroborated terminal activity), independent pending-order proof, or an explicit unresolved/review-required bucket. When status-token-free rows exist and no fresh corroborating export is available, completeness fails closed. Enforcing tests: `tests/test_inventory_completeness.py`.
- Complete, unique EFT rows remain collapsed when account, direction, method, status, date, and amount are present. This deliberately avoids collecting masked counterparty details; incomplete or colliding rows require disclosure evidence.
- Any raw Activity control carrying a final-status token must parse into the terminal-row pipeline or produce a warning; parser rejection must not silently bypass selective expansion.
- Order detail views may display Cancel/Modify buttons. Seeing those controls must be logged; clicking them is forbidden.
- A filter Clear/reset click is provenance only and never proves filter state. Only a post-action sidebar snapshot proves defaults.
