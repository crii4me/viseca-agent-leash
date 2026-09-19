# Function 2 — Decision engine

> **Status: hard-rules layer working, 29 tests green.** Owner: Omar.

Incoming purchase event → `approve` / `decline` / `step_up`, within the
8-second deadline.

## What's built

- **`hard_rules.py`** — deterministic evaluator for the `hard_rules[]` array
  Function 1 produces. Imports `FIELDS` straight from `mandate_compiler.models`
  so the two functions can't drift apart. Handles both field namespaces
  (`authorization.*` off the event, `rolling.*` off our own ledger over
  `period_days`), converts rule currency → CHF via `fx_rates`, and fails closed
  on anything it can't check. Accepts both `=` and `==` (the live schema uses
  `=`, the compiler emits `==`).
- **`ledger.py`** — per-card record of final approvals, for the `rolling.*`
  windows. Strictly-earlier rows only; idempotency by live `authorization_id`.
- **`decide.py`** — the approve/decline/step_up ladder (below).
- **`api_client.py`** — the live worker: bootstrap → compile+confirm mandate →
  run → poll → decide → submit. Ready to run; needs `TEAM_API_KEY`.

## The decision ladder (decided 2026-09-19)

| Situation | Decision | Why |
|---|---|---|
| Mandate not active | `decline` | Permission withdrawn/expired. |
| A hard rule was checked and **failed** | `decline` | An explicit customer limit was breached. |
| A hard rule **couldn't be checked** (missing field) | `step_up` | Don't fail a good purchase over a missing field — ask the human. Currency never causes this (fixed rates for CHF/EUR/GBP/USD). |
| All rules pass, **open questions remain** | `uncertainty_policy` | ask→step_up, decline→decline, approve→approve. |
| All rules pass, nothing open | `approve` | Within policy. |
| Engine throws mid-evaluation | `decline` | Fail-closed under the deadline — the one place "can't decide" means decline, not step_up. |

## What is NOT enforced yet

The hard-rules layer only enforces what Function 1 compiles into `hard_rules`
— today that's mostly the money caps. Everything the compiler leaves as
`guidance` / `open_questions` is **not** a gate: merchant allowlists,
returnability, size, session/velocity integrity, undeclared duplicates, and
prompt injection all currently pass if the amount is within cap. Those are the
risk-signal and injection layers, still ahead. Run the replay harness to see
exactly which scenario rows this affects.

## Run

```bash
cd decision-engine
pip install -e ../mandate-compiler        # Function 1, a sibling package
pip install -e . && pip install pytest requests
pytest                                     # 29 tests, offline, no key
```

Offline simulation over the real 45-attempt data pack lives in
[`/demo-harness`](../demo-harness) — `python replay.py`.

## Event day

1. `TEAM_API_KEY` into `.env`.
2. `Worker(LeashClient()).run(instruction, "SCEN0000")` for the connection check.
3. Reconcile the provisional field mappings in `hard_rules._resolve_authorization_field`
   against the live event (merchant fields, `item_count`) — only
   `authorization.billing_amount_chf` is confirmed.
4. Confirm the `/v1/reference-data` fx-rate JSON shape feeds
   `LeashClient.fx_rates()` (it falls back to the documented fixed rates if not).
