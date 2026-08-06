# Query Control Write Intent Boundary Design

## Goal

Restore a clear intent boundary in the current `d310ce3` codebase without reintroducing a separate router. `QueryResponder` remains the single conversation coordinator, but its protocol is split into `query`, `control`, `write`, and `irrelevant`.

## Scope

Modify only:

- `src/prompts.py`
- `src/query_responder.py`
- `src/server.py`
- `README.md`

Add only:

- `tests/test_query_responder_intents.py`
- `tests/test_server_intent_flow.py`

Do not change task decomposition, S1-S8 ordering, criteria evaluation, anomaly advice, status update APIs, approval APIs, persistence layout, frontend structure, model loading, config directory structure, or third-party dependencies.

## Intent Model

`query` is read-only. It answers task facts, status, subtask progress, criteria, anomaly, pending action impact, and whether prior changes are reflected.

`control` changes process flow only. Valid actions are `rollback`, `retry`, and `force_complete`.

`write` writes data or state facts. Valid actions are `change_parameter` and `override_field`.

`irrelevant` is the non-business fallback for unrelated or unsafe-to-classify messages.

`force_complete` remains `control` because its user meaning is flow advancement. `override_field` remains `write` because its user meaning is writing an observed fact.

## Classification

Classification returns:

```json
{
  "intent": "query | control | write | irrelevant",
  "target_task_id": null,
  "query_topics": [],
  "action": null,
  "needs_clarification": false,
  "confidence": 0.95,
  "reason": "short reason"
}
```

The prompt must remove `adjust_criterion_tolerance`, add query topics, and include contrast examples where action words in question form stay `query`, such as "S2 can retry?" and "what happens if we retry S2?".

Code normalization validates the protocol after LLM output:

- `query` cannot carry an action.
- `control` can only carry control actions.
- `write` can only carry write actions.
- Low-confidence control/write requests do not create pending actions.
- Missing target, field, or value returns clarification.
- A single message cannot create multiple actions. Mixed control/write requests are clarification-only.

## Query Facts

Add `_build_query_facts(task_state, query_topics)`.

Facts are extracted deterministically from `task_state["metadata"]` and runtime task state. `metadata["monitoring_runtime_example"]` is explicitly excluded because it is admission-file example data, not live status.

Metadata facts include identity, time, location, task details, equipment, and conditions. Runtime facts include overall status, current subtask, subtask statuses, retry counts, criteria summaries, current failed criteria, anomaly fields, and pending intervention.

The reply LLM receives compact query facts through `operation_result`; it should not browse the whole raw task JSON to answer factual questions.

## Pending Flow

When a pending intervention exists:

- Explicit confirmation executes the original pending action.
- Explicit cancellation clears it.
- A new `query` is answered normally and the pending action remains.
- A new `control` or `write` is rejected without replacing the pending action.

Responses keep existing external types for frontend compatibility, for example `type: "intervention_pending"` with an added `intent` field.

## Write Safety

`override_field` is allowed only for fields declared by the target subtask's `criteria_ref` in `criteria.yaml` hard or soft entries.

`change_parameter` is allowed only for `timeout_seconds` and `max_retries`, because those are the writable parameters currently reflected by the task flow. Unsupported parameter writes are recognized as write attempts but do not create pending actions.

## Server Orchestration

`src/server.py` branches on `query`, `control`, `write`, and `irrelevant`.

It preserves two-step confirmation for control and write actions. It returns `intent` in query responses, pending responses, executed responses, cancellation responses, and rejection responses.

## Tests

Add coverage for:

- Basic metadata query facts such as water depth, coordinates, robot type, payload, support vessel, time, task type, and priority.
- Task status, subtask status, criteria, anomaly, and pending action queries.
- Question phrasing with action words staying query.
- Retry, rollback, and force-complete as control.
- Parameter changes and field overrides as write.
- Invalid action/intent combinations rejected.
- Missing write values rejected.
- Pending query allowed without clearing pending.
- Pending control/write rejected without replacing pending.
- Confirmation and cancellation execute or clear only the original pending action.
