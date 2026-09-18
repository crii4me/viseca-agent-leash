# Demo harness

> **Status: not started.** This folder is a stub. Owner: _TBD_.

The offline test harness, and whatever we show the judges.

## Two jobs

### 1. Offline replay harness

Read a JSON event → validate it against Viseca's
`authorization_event.schema.json` → hand it to
[`/decision-engine`](../decision-engine) → print `decision` + `reasons`.

**No API key required for any of this**, which is the point — it's what lets
Function 2 be developed and tested before event day. Replay
`data/purchase_attempts.csv` in `replay_order` against
`data/authorization_history.csv`.

This is the fastest way to catch the rolling-window bugs, because you can replay
the same sequence deterministically and diff the decisions.

### 2. The demo

Whatever makes the story legible in a few minutes. The narrative the challenge is
actually judging is **how the customer retained control**, so the thing worth
showing is the full round trip:

> plain-English instruction → compiled mandate (with its open questions) →
> a purchase that's approved → one that's declined → one that triggers a step-up
> → and the customer answering an open question, which changes a later decision.

The `open_questions` array is the most demo-able part of Function 1: it's visible
proof the system asks rather than guesses. Worth putting on screen.

## Getting started

Function 1 works **today** and needs no key, so you can build against real output
immediately:

```bash
cd ../mandate-compiler
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt && pip install -e .
python scripts/run_fixtures.py --json
```

That prints compiled mandates for 7 instructions as JSON — enough to build the UI
and the harness against before the decision engine exists.

To compile an arbitrary instruction:

```python
from mandate_compiler import compile_mandate
compile_mandate("Buy one ordinary grocery item for CHF 20 or less.").to_viseca_dict()
```

See [`/contracts/README.md`](../contracts/README.md) for the exact output shape.

## Language

Open. If this ends up a web frontend, note the repo `.gitignore` already covers
`node_modules/` and `.env*`.
