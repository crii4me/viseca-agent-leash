# Demo harness

> **Status: offline replay working.** Owner: Omar.

The no-key path: replay the challenge data pack through Function 1 + Function 2
and see every decision with its reason.

## Run

```bash
cd demo-harness
pip install -r ../decision-engine/requirements.txt -e ../decision-engine
pip install pytest requests
python replay.py                       # all 5 scenarios, FULL engine, bundled sample-data/
python replay.py --scenario SCEN0002   # one scenario
python replay.py --json                # machine-readable, for a UI
python replay.py --data-dir /path/to/viseca-2026/data   # a full data pack
python replay.py --no-risk             # hard rules ONLY -- for diffing the layers
```

**The full engine is the default.** Risk composition used to be opt-in behind
`--risk`, so a plain `python replay.py` quietly ran only the hard-rules half:
SCEN0004's duplicate order and lookalike seller both slipped through, and the
output looked like a finished system. `--risk` is still accepted and does
nothing; `--no-risk` is the explicit opt-out and prints a warning.

Expected totals over the full 45-purchase pack (`--data-dir` at the real pack):

| Mode | approve / decline / step_up |
|---|---|
| default (full engine) | **24 / 12 / 9** |
| `--no-risk` | 36 / 9 / 0 ← half the engine |

Twelve of those approvals flip to decline or step_up once risk composition runs.
If you see 36/9/0, you are looking at the partial engine.

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
