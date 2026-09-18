# Mandate Compiler — Function 1

Viseca "Agent on a Leash", Swiss {ai} Weeks 2026 ([START-Hack/viseca-2026](https://github.com/START-Hack/viseca-2026)).

Plain-English spending instruction in, structured mandate out.

```python
from mandate_compiler import compile_mandate

m = compile_mandate(
    "Buy one ordinary grocery item for CHF 20 or less from a shop I use "
    "regularly. Ask me when uncertain."
)
m.to_viseca_dict()
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

## Setup

```bash
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt && pip install -e .
cp .env.example .env
pytest
```

No API key needed — the test suite and every fixture run fully offline.

## The contract

`compile_mandate(instruction: str) -> Mandate`. That is the whole public surface.
It is **pure**: the instruction string is the only input, a `Mandate` the only
output. It reads no files, touches no ledger, and makes no Viseca API calls —
`/healthz` and `/v1/bootstrap` belong to Function 2's runtime, not to mandate
compilation.

`Mandate.to_viseca_dict()` emits exactly the four contract fields. Everything
else on the object (`clauses`, `source_instruction`, `meta`) is provenance we
add for explainability and is deliberately kept out of the canonical payload.

The JSON Schema lives at [`/contracts/mandate.schema.json`](../contracts/mandate.schema.json)
(regenerate with `python scripts/export_schema.py`). **Function 2 should validate
against that file, not import these Pydantic models** — it keeps the two
functions independently testable and lets a non-Python consumer check the same
contract. See [`/contracts/README.md`](../contracts/README.md) for the
integration notes.

### Two field namespaces

This is the part Function 2 most needs to agree with:

| Namespace | Read from | Rule scope |
|---|---|---|
| `authorization.*` | the incoming event, directly | `purchase` |
| `rolling.*` | Function 2's own ledger, over `period_days` | `period` |

Mixing them is rejected at compile time. A `rolling.*` rule is an instruction to
Function 2 to aggregate over the trailing window, **summing only strictly-earlier
rows, per card**.

### Currency is not decoration

A money rule's `value` is denominated in its own `currency`, which is **not
necessarily CHF**, even though the field is CHF-denominated. Function 2 must
convert through `fx_rates` before comparing, and must **fail closed (decline)**
if it cannot. A cap that can't be compared is not a cap that was satisfied. The
compiler emits a warning on every non-CHF rule so this can't be missed.

## How it decides

Four buckets, and the split is the point — it's what makes the system's limits
legible to the customer instead of hidden:

- **`hard_rules`** — only when there's an explicit comparator, an explicit value,
  and (for money) an explicit currency, against a field that actually exists.
- **`guidance`** — shapes judgment, no comparable field. Describes the *kind* of
  purchase ("an ordinary grocery item").
- **`open_questions`** — asserts a threshold or a relationship over history that
  the instruction never defines ("regularly", "a trusted shop", a number with no
  currency). **We ask instead of guessing.**
- **`uncertainty_policy`** — `ask` | `decline` | `approve`, for when nothing else
  resolves a case. Defaults to `ask` when unstated, and says so in
  `open_questions` rather than defaulting silently.

The line between guidance and an open question: a word describing a **kind** of
thing is guidance (an agent can weigh it per purchase from item data); a word
asserting a **quantified relationship over history** is an open question,
because enforcing it needs a threshold nobody supplied. That's why "ordinary
grocery item" and "a shop I use regularly" land in different buckets despite
both being fuzzy. Configurable via `MANDATE_UNRESOLVABLE_TO`.

## Two backends

| | `deterministic` | `anthropic` |
|---|---|---|
| Needs a key | no | yes |
| Speed | ~0.1–2 ms | one API round trip |
| Coverage | numeric/comparable patterns | open-ended phrasing |
| Status | **tested, 88 tests green** | **network path unverified** |

`MANDATE_BACKEND=auto` (the default) uses the model when a key is present and
falls back to deterministic otherwise — including if the API call *fails*, which
is the deterministic fallback the brief asks for.

Both backends pass through the same assembly, the same field-vocabulary gate and
the same schema validation. A rule the model invents for a field that doesn't
exist is demoted to guidance plus an open question, never enforced.

## Verified vs. not

Worth being precise about, since it affects what you can demo:

- **Verified.** The deterministic backend, the models and invariants, schema
  validation, and the LLM backend's *mapping* layer (via a fake client) — 88
  tests, all passing offline.
- **Not verified.** The actual Anthropic network call. There was no API key on
  the build machine, so `messages.parse(...)` has never been sent. It's written
  from the SDK's documented structured-output surface. Run
  `python scripts/smoke_llm.py` once a key exists, before relying on it.
- **Assumed.** Only `authorization.billing_amount_chf` is confirmed from Viseca's
  worked example. Every other field in `models.FIELDS` is our best guess and is
  flagged `confirmed=False`; `Mandate.provisional_fields_used` reports when a
  compiled mandate leans on one.

## Try it

```bash
python scripts/run_fixtures.py          # all 7 test instructions, readable
python scripts/run_fixtures.py --json   # machine-readable, for diffing runs
python scripts/smoke.py                 # just the worked example
```

## Event-day checklist

1. Clone `START-Hack/viseca-2026`; reconcile `models.FIELDS` against
   `data/schemas/authorization_event.schema.json`. Flip `confirmed=True` on what
   matches, fix what doesn't. **This is the first thing to do** — every
   provisional field is a rule that might not be evaluable.
2. Put the team key in `.env`, run `python scripts/smoke_llm.py`.
3. Re-run `pytest` and `scripts/run_fixtures.py` with `MANDATE_BACKEND=anthropic`
   to compare the two backends on the same fixtures.

## Open questions for the team

1. **Is `guidance` a list or a free-text string?** We emit `list[str]` to keep
   clause provenance; `Mandate.guidance_text` joins it. One-line change if Viseca
   wants a plain string.
2. **Should "buy *one* item" be a hard rule?** Arguably `authorization.item_count
   <= 1`. Viseca's own worked example doesn't list it, so we treat it as guidance.
   Revisit once we know whether item count is on the event.
3. **Precedence between a deny-type hard rule and a permissive
   `uncertainty_policy`.** Function 2's call, but it should be decided explicitly
   rather than falling out of array order.
