# Demo harness

> **Status: offline replay working.** Owner: Omar.

The no-key path: replay the challenge data pack through Function 1 + Function 2
and see every decision with its reason.

## Run

```bash
cd demo-harness
pip install -e ../mandate-compiler -e ../decision-engine
pip install pytest requests
python replay.py                       # all 5 scenarios, bundled sample-data/
python replay.py --scenario SCEN0002   # one scenario
python replay.py --json                # machine-readable, for a UI
python replay.py --data-dir /path/to/viseca-2026/data   # a full data pack
```

## What it does, per purchase

```
cardholder instruction
   → Function 1 (compile_mandate)        the mandate
   → build a live-shaped event (build_events.py, from the CSV pack)
   → Function 2 (decide), in replay_order, per card
   → approve / decline / step_up  + the reason
```

State is kept the way a correct live worker must (technical_details.md step 8):
only final approvals enter the ledger (a step_up doesn't, until a human
resolves it), repeated `authorization_id`s aren't decided or counted twice, and
the simulated purchase time drives the rolling windows.

## Files

- `replay.py` — the runner.
- `build_events.py` — turns the CSV pack into schema-valid events (a copy of
  the tested builder from the offline edge-case kit).
- `sample-data/` — the public challenge data pack, trimmed to just the CSVs the
  replay reads (no `authorization_history.csv`), so this runs with zero setup.

## Reading the output

The money caps are enforced and correct. Where a scenario's real intent
(specialist retailer, returnable, "shops I've used before", no unrequested
add-ons) isn't a money cap, the current engine still approves it — that's the
boundary of the hard-rules layer, not a bug. See
[`/decision-engine`](../decision-engine) → "What is NOT enforced yet".

## The demo itself

Not built. The narrative to show: instruction → compiled mandate (with its open
questions) → an approve, a decline, a step_up → the customer answering an open
question and a later decision changing. `replay.py --json` is the data source
to build that UI against.
