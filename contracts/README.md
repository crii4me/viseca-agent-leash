# Interface contract

**The one file that matters: [`mandate.schema.json`](mandate.schema.json).**

That is the single source of truth for the JSON flowing from `/mandate-compiler`
(Function 1) into `/decision-engine` (Function 2). If the two sides disagree,
the schema wins. Nobody should need to read the other side's source to integrate.

It is **generated**, not hand-written:

```bash
cd mandate-compiler && python scripts/export_schema.py
```

Regenerate and commit after any change to the mandate shape. Don't hand-edit it —
the next regeneration will silently overwrite your edit.

## The shape

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
  "open_questions": ["...'regularly' isn't defined. What should qualify...?"]
}
```

Exactly four top-level keys. `additionalProperties: false` — anything extra is a
validation failure, not a field to ignore.

| Key | Type | Meaning for Function 2 |
|---|---|---|
| `hard_rules` | array | Mechanically checkable. Evaluate every one. |
| `uncertainty_policy` | `ask` \| `decline` \| `approve` | What to do when nothing else resolves the case. |
| `guidance` | array of strings | Free-text context that should inform judgment. Never a gate on its own. |
| `open_questions` | array of strings | Things the customer hasn't answered yet. Surface these; don't silently resolve them. |

## Four things that will bite you

These are the parts where a reasonable-looking implementation is wrong.

### 1. Two field namespaces, and they are not interchangeable

| Prefix | Read from | `scope` |
|---|---|---|
| `authorization.*` | the incoming event, directly | `purchase` |
| `rolling.*` | **your own ledger**, aggregated over `period_days` | `period` |

A `rolling.*` rule is an instruction to aggregate over the trailing window,
**summing only strictly-earlier rows, per card**. Function 1 rejects a mismatched
pair at compile time, so if you receive one, it is coherent — but you still have
to actually implement the windowing.

`period_days` is present if and only if `scope` is `period`.

### 2. `value` is in `currency`, which is not necessarily CHF

The field is named `billing_amount_chf`, but a rule can carry
`"value": 45.0, "currency": "EUR"`. That means *45 euros*, not 45 francs.

**Convert through `fx_rates` before comparing.** If conversion isn't possible,
**fail closed — decline.** A cap that can't be compared is not a cap that was
satisfied. Silently skipping a currency-mismatched rule is the single easiest way
to approve a purchase nobody authorised.

### 3. Operators are exact

`<` and `<=` both appear and are not interchangeable — "under CHF 25" compiles to
`<`, "no more than CHF 25" compiles to `<=`. Full set: `<=`, `<`, `>=`, `>`,
`==`, `!=`, `in`, `not_in`. The `in` / `not_in` operators take an array value;
everything else takes a scalar.

### 4. `guidance` and `open_questions` are not rules

They must never, on their own, approve or decline anything. A non-empty
`open_questions` means the customer was asked something and hasn't answered —
that is a reason to lean on `uncertainty_policy`, not a reason to invent a
threshold.

## Validating

Python:

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("contracts/mandate.schema.json"))
Draft202012Validator(schema).validate(mandate)
```

Any language with a JSON Schema (draft 2020-12) library works — that's the point
of publishing the file rather than a Python module.

## Still open

Decisions we should make together rather than discover at hour 12:

1. **Precedence between a deny-type hard rule and a permissive
   `uncertainty_policy`.** Decide explicitly; don't let array order decide it.
2. **Is `guidance` a list or one free-text blob?** Currently `list[str]` to keep
   clause provenance. One-line change on Function 1's side if you want a string.
3. **Field vocabulary is partly provisional.** Only
   `authorization.billing_amount_chf` is confirmed from Viseca's worked example.
   The rest are our best guess and must be reconciled against
   `data/schemas/authorization_event.schema.json` on event day. See
   `mandate-compiler/src/mandate_compiler/models.py` → `FIELDS`.
