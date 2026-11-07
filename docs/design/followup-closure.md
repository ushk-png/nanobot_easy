# Scoped follow-up closure

This change is limited to the 60→45 minute plan, 45 versus 50 conflict→choice,
and correct versus changed fixed-item values. It does not expand the natural-language parser.

Two product changes in `context_selection.py`:

- A kept condition citing the current user event is validated against that event and current text.
  It is not rejected merely because the prior-condition excerpt does not contain it, and is not
  misclassified as evidence carried from an earlier turn. Foreign sources, unsupported spans and
  task-reference mismatches keep their existing checks.
- A non-nested same-unit item sum above the total contradicts both exact-composition and upper-bound
  readings. It is a conflict even when the relation kind is unclear. A shortfall or nested scope stays
  unverified. No new phrase regex or distance-limit exception was added.

The original 90-second trial records retain their original task state, requests, model arguments,
validation outcomes and provenance hashes in `tests/agent/fixtures/handoff90/`. The replay runs map only
newly allocated IDs. An additional frozen-source test uses the saved user/assistant events and first
completed task without ID remapping. Original responses are not changed into successful fixtures.

`reference_kept_condition_unoffered` was a product provenance error: the model's source was the actual
current user message. The model also sent partial quantity relations and duplicate text, which remain
model-output limitations; the code still grounds supported quantities in user originals.
`insufficient_info` above the total was a product arithmetic-status error. Blocking the follow-up solely
because that internal label was not `conflict` was a separate harness constraint.

The independent test oracle grades answer allocations/options/task linkage separately from product
relations/answer findings. A valid question can authorize a follow-up despite a bad internal label;
that label still receives a failing internal grade. An unreadable answer stays unverified. The oracle
is for these fixed scenarios only and is not a general natural-language judge.

Existing t61 T16b/T16c internal expectations change from insufficient/never-conflict to conflict for
flat 50>45. Both continue refusing completion. T16c still requires the unsupported model kind to remain
`unclear`; its arithmetic conflict is independent of that kind. Saved trial outcomes are unchanged.

State-transition tests cover create/change/rechange, conflict/choice/rechange/choice, other-task
isolation, current values, fixed values and old-verification invalidation. Constructed proposals and
answers are explicitly mocks. Historical completion ledger entries may remain as history but cannot
complete a changed instruction version. These tests do not measure model interpretation success.
A constructed phrase using '바꾸고' exposed the existing external-target floor ambiguity; it is outside
the fixed wording set and remains a follow-up issue rather than another parser exception.

## Narrow numeric rendering assessment

The existing computed `expected_values` or passing relation results can supply item/value rows
without extracting numbers from prose. However, the current delivery contract has only a free-text
`answer`; it does not identify a complete numeric-only deliverable or which nonnumeric goals and
constraints must be retained. Replacing that answer with a list would silently drop requested detail.
This repair therefore does not change delivery or add a new model-output schema. A future explicit
numeric-output contract could use those values; the source-to-requirement checks must still apply.
Free-text answer extraction and its documented limitations remain.

## New real-model experiment (not executed here)

Run exactly two independent instances of the same two-session bundle, with fresh workspaces and no
repeat-until-success. Freeze this commit, the operating config fingerprint and the pinned 90-second
trial harness. Per instance: 8 sends/360 seconds. Aggregate: at most 16 sends and 750 seconds including
process startup. Repairs and provider resends count. The shared cost ledger uses xAI's documented
`cost_in_usd_ticks` (10^10 ticks/USD), stops further sends at $0.50 observed cost, and stops on unknown
cost. A final in-flight request can cross that threshold: this is not a guaranteed billing hard cap.
An execution approval must explicitly accept that policy or establish an account-side hard cap first.
The previous timeout, cleanup and unsafe-completion guards are retained. No operation is deployed.
