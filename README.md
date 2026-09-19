# Viseca — Agent on a Leash

Wallet control layer for AI-agent purchases. Swiss {ai} Weeks 2026,
[START-Hack/viseca-2026](https://github.com/START-Hack/viseca-2026). 3 people, 15 hours.

**The problem:** an AI agent has a card. The customer writes one sentence saying
what it may spend. Every purchase the agent attempts must be answered
`approve` / `decline` / `step_up` within 8 seconds — against that sentence.

**The split:** turning the sentence into rules is a different problem from
enforcing them, so they're different services.

```
 "Buy one ordinary grocery item for CHF 20          purchase event
  or less from a shop I use regularly.                    │
  Ask me when uncertain."                                 │
              │                                           │
              ▼                                           ▼
   ┌───────────────────────┐   mandate JSON   ┌───────────────────────┐
   │  /mandate-compiler    │ ───────────────► │   /decision-engine    │
   │  Function 1           │   /contracts     │   Function 2          │
   │  once per mandate     │                  │   once per purchase   │
   └───────────────────────┘                  └───────────┬───────────┘
                                                          │
                                              approve / decline / step_up
                                                          │
                                              ┌───────────▼───────────┐
                                              │    /demo-harness      │
                                              │  replay + the demo    │
                                              └───────────────────────┘
```

## Which folder is yours

| Folder | What | Owner | Status |
|---|---|---|---|
| [`/mandate-compiler`](mandate-compiler) | **Function 1.** Instruction text → structured mandate | [@crii4me](https://github.com/crii4me) | ✅ Working, 88 tests green |
| [`/decision-engine`](decision-engine) | **Function 2.** Purchase event → decision, in <8s | _TBD_ | 🔲 Stub |
| [`/demo-harness`](demo-harness) | Offline replay harness + the demo we show judges | _TBD_ | 🔲 Stub |
| [`/risk-signals`](risk-signals) | Duplicate / lookalike / velocity **evidence** for Function 2 to consume | [@crii4me](https://github.com/crii4me) | ✅ Working, 51 tests green |
| [`/contracts`](contracts) | **The interface between 1 and 2.** Read this first | shared | ✅ Published |

Each folder's README is a real briefing, not a placeholder — start with yours.

## The interface

**[`/contracts/mandate.schema.json`](contracts/mandate.schema.json) is the single
source of truth.** If Function 1 and Function 2 disagree, the schema wins. Nobody
should have to read the other side's code to integrate.

Function 1 is a pure function — **instruction string in, mandate JSON out**:

```python
from mandate_compiler import compile_mandate

compile_mandate(
    "Buy one ordinary grocery item for CHF 20 or less from a shop I use "
    "regularly. Ask me when uncertain."
).to_viseca_dict()
```

```json
{
  "hard_rules": [
    {
      "field": "authorization.billing_amount_chf",
      "operator": "<=",
      "value": 20.0,
      "currency": "CHF",
      "scope": "purchase"
    }
  ],
  "uncertainty_policy": "ask",
  "guidance": ["One ordinary grocery item", "A shop I use regularly"],
  "open_questions": [
    "You limited purchases to shops you use regularly, but 'regularly' isn't defined. What should qualify a shop as regular — a minimum number of past purchases (say 3 or more), or any shop used within a recent window (say the last 90 days)?"
  ]
}
```

Exactly four keys, always. `additionalProperties: false`.

- **`hard_rules`** — mechanically checkable. Evaluate every one.
- **`uncertainty_policy`** — `ask` | `decline` | `approve`, for when nothing else resolves the case.
- **`guidance`** — informs judgment. **Never a gate on its own.**
- **`open_questions`** — what the customer hasn't answered yet. Surface, don't guess.

⚠️ **Three things that will bite you** — the full list with reasoning is in
[`/contracts/README.md`](contracts/README.md):

1. `authorization.*` fields come off the event; `rolling.*` fields you aggregate
   from your **own ledger** over `period_days`. Not interchangeable.
2. `value` is denominated in `currency`, which **is not necessarily CHF** despite
   the field name. Convert via `fx_rates`, and **fail closed** if you can't.
3. `<` and `<=` both occur and are not interchangeable.

## Run Function 1 (works today, no API key)

```bash
cd mandate-compiler
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt && pip install -e .
pytest                          # 88 tests, fully offline
python scripts/run_fixtures.py  # 7 example instructions, compiled
```

Needs Python 3.10+. **No API key required** — there's an offline deterministic
backend that is both the no-key dev path and the fallback the challenge brief
asks for. Add `ANTHROPIC_API_KEY` to `.env` later to switch on the model backend.

`python scripts/run_fixtures.py --json` gives machine-readable output, which is
the easiest thing to build the harness and the UI against before Function 2 exists.

## Run the others

Not built yet. See [`/decision-engine`](decision-engine) and
[`/demo-harness`](demo-harness) — both READMEs have a getting-started section, a
test list, and the design decisions already made.

## Ground rules

- **Never commit a key.** `.env` is gitignored; `.env.example` is the template.
  The team key arrives on event day — it goes in `.env`, nowhere else.
- **Regenerate the contract, don't hand-edit it.**
  `cd mandate-compiler && python scripts/export_schema.py`, then commit.
- **Branch for anything non-trivial.** Three people, 15 hours, one `main` — a
  force-push over someone's work costs more than a branch does.

## Event day

1. Clone `START-Hack/viseca-2026` for the fixtures and the real event schema.
2. **Reconcile `mandate-compiler`'s field vocabulary against
   `data/schemas/authorization_event.schema.json`.** Only
   `authorization.billing_amount_chf` is confirmed today; the rest are flagged
   provisional in code. Do this first — every provisional field is a rule that
   might not be evaluable.
3. Team key into `.env`; `GET /healthz`, then `POST /v1/bootstrap`. Read
   deadlines and window values from bootstrap at runtime, don't hardcode them.
