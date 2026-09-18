# Function 2 — Decision engine

> **Status: not started.** This folder is a stub. Owner: _TBD_.

Incoming purchase event → `approve` / `decline` / `step_up`, **within an
8-second deadline**.

## What it does

Evaluates the `hard_rules` that [`/mandate-compiler`](../mandate-compiler) produced
against a self-maintained rolling-spend ledger, keeps untrusted shop/item text
structurally isolated from anything with decision authority, and applies
`uncertainty_policy` when nothing else resolves the case.

**Read [`/contracts/README.md`](../contracts/README.md) before writing any code.**
It defines the exact JSON you receive and lists the four things most likely to
bite you (the two field namespaces, non-CHF currency, `<` vs `<=`, and the fact
that `guidance` is not a rule).

## Design shape worth copying

From `leashd` (brainbytes-dev/leashd, AGPL-3.0). Wrong payment rail for us
(Lightning/Cashu/x402, not CHF cards) so **don't depend on its code** — but its
evaluation shape is worth stealing as design:

- **Hard stops first, cheaply.** A revoked or unconfirmed (draft) mandate denies
  immediately, before touching `hard_rules`.
- **Denylist beats allowlist, explicitly.** Decide and document this precedence
  now rather than letting array order decide by accident.
- **No policy → deny.** An authorization referencing a card with no active
  mandate declines. Never fall through to approve.
- **Every decision returns `{decision, reasons: [], matched}`** — including
  approvals ("within policy"). Cheap to implement, direct hit on the
  explainability judging criterion.
- **Errors are caught and forced to decline, never left to throw.** Under an
  8-second deadline an uncaught exception becomes a timeout; both must resolve to
  decline, and the error handling itself must be fast.
- **Unit/currency mismatch fails closed.** Convert via `fx_rates` before
  comparing. Never silently skip a rule you couldn't compare.

## Test list

Start here; these are the cases where a plausible implementation is wrong.

- [ ] Currency-mismatched hard rule — must still enforce, via `fx_rates`.
- [ ] Revoked or unconfirmed (draft) mandate referenced by an authorization → deny.
- [ ] Malformed rule / missing field / bad history row → engine throws → must
      resolve to decline **inside** the 8s window, not hang.
- [ ] Rolling-period aggregation over the wrong window — `period_days` must sum
      only strictly-earlier rows, per card.
- [ ] An explicit deny-type hard rule vs. a permissive `uncertainty_policy` —
      decide precedence now.
- [ ] Step-up requested but never answered within 120s → decline on timeout,
      never silently approve.
- [ ] SCEN0004-style injected text in item/merchant descriptions attempting to
      alter a decision → verify no code path lets extracted text set a rule value.

## Getting started

1. Clone [START-Hack/viseca-2026](https://github.com/START-Hack/viseca-2026) for
   fixtures: `data/scenario_fixtures/example_authorization_request.json`,
   `data/schemas/authorization_event.schema.json`,
   `data/purchase_attempts.csv` + `purchase_attempt_items.csv` (sorted by
   `replay_order`), `data/authorization_history.csv`.
2. Get one event evaluating end-to-end against a **hardcoded** mandate before
   wiring in real history replay.
3. On event day, swap "read JSON from file" for the long-poll
   (`/v1/decision-requests/next?wait=25`) and "print decision" for
   `POST /v1/authorizations/{id}/decision`. If this is built right, that swap is
   small.

Read timeout and window values from `/v1/bootstrap` at runtime rather than
hardcoding them.

## Language

Not fixed. Python keeps it consistent with `/mandate-compiler` and lets you reuse
`jsonschema` for contract validation — but the contract is a plain JSON Schema
precisely so this can be anything.
