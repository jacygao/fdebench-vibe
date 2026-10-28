# FDEBench Operations API

Async `POST /triage`, `POST /extract` and `POST /orchestrate` APIs for all three tasks, implemented
from the supplied video transcripts and published input contracts. See [requirements.md](requirements.md)
and [docs/methodology.md](docs/methodology.md).

## Readiness

The requested documentation artifacts are present:
[architecture](docs/architecture.md), [methodology](docs/methodology.md), and
[evaluations](docs/evals.md).

All four requested endpoints are implemented locally: `GET /health`,
`POST /triage`, `POST /extract` and `POST /orchestrate`. This does not establish
deployment readiness or model accuracy. See [evaluation and readiness status](docs/evals.md).

## Run

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Redis 7+.
All service instances must share the same Redis store.

```sh
uv sync --frozen
cp .env.example .env
# Set TRIAGE_LLM_API_KEY in .env.
# Point TRIAGE_REDIS_URL at your Redis instance.
uv run uvicorn triage.api:app --host 127.0.0.1 --port 8000 --no-access-log
```

The configured provider must support OpenAI-compatible chat completions with
strict JSON-schema output for triage, image input and JSON-object output for
extraction, JSON-object planning and token usage. Triage/extraction default to
`gpt-4.1-mini`; orchestration defaults to the more capable `gpt-4.1`.
provider access and pricing are the operator's responsibility. Azure deployments
can use their full HTTPS chat-completions URL and `TRIAGE_LLM_AUTH=api-key`.
Provider redirects are not followed and TLS certificates are verified.

Interactive API documentation: `http://127.0.0.1:8000/docs`.
`GET /health` is liveness; `GET /ready` returns 503 until credentials are set and
Redis is reachable. Neither endpoint verifies remote model availability.

```sh
curl -sS http://127.0.0.1:8000/triage \
  -H 'Content-Type: application/json' \
  -d '{
    "ticket_id": "SIG-4829",
    "subject": "Pressure reading",
    "description": "Deck 4 pressure is falling. Possible hull breach.",
    "reporter": {
      "name": "Mira Chen",
      "email": "mira.chen@cdss.space",
      "department": "Habitat Operations"
    },
    "created_at": "2026-10-28T03:00:00Z",
    "channel": "bridge_terminal",
    "attachments": ["deck4-pressure.csv"]
  }'
```

The response contains `ticket_id`, `category`, `priority`, `assigned_team`,
`needs_escalation`, `missing_information`, `next_best_action` and
`remediation_steps`. Labels are exact challenge values; enum definitions are in
`triage/models.py`. Attachments are filenames, never fetched. Raw signals are
sent to the configured model provider but are not stored locally. Redis persists
decisions, including ticket IDs and action text that may contain sensitive details.

Every response carries `X-Model-Name` and `X-Request-ID`. Successful responses include
`X-Decision-Cache`, `X-LLM-Input-Tokens`, `X-LLM-Output-Tokens` and
`X-LLM-Total-Tokens`. Configure both per-million prices to also return
`X-LLM-Estimated-Cost-USD`. Usage refers to the successful completion, not the
entire provider bill. Failed requests may still incur provider charges.
Cached decisions report zero new tokens and estimated cost.
Uncached cost estimates use the rates for `TRIAGE_LLM_MODEL`. An independently
selected extraction model has no cost estimate unless it is the same model.

Errors use `{"error": {"code": "...", "message": "..."}}`: 413 for a triage body above
64 KiB or extraction body above 16 MiB, 415 for incorrect content type,
422 for invalid input, 503 for missing
configuration, unavailable infrastructure or capacity, 504 for timeouts, and 502
for rejected/invalid/inconsistent model output. Only transient transport failures,
429s and server errors are retried, at most three attempts. `Retry-After` seconds
and HTTP dates are honored. If waiting would exceed the overall deadline, the
service returns 503 immediately with that delay instead.

Uncached triage/extraction requests use one model completion, except bounded retries. Sampling is
fixed and Redis preserves the first committed decision for each complete input
and policy/model fingerprint. A distributed lease prevents duplicate concurrent
classifications. Busy duplicates get 503 with `Retry-After: 1`; retries return the
same shared decision. Configure durable, non-evicting Redis and protect its
contents. Retention expiry, store loss or policy/model changes can cause a new
decision. There is no silent classification fallback.

## Document extraction

`POST /extract` accepts `document_id`, `content` (raw base64 image bytes),
`content_format: "image_base64"`, and `json_schema` (a JSON string or schema object).
The schema is required; its root must be an object. The response is a **flat
object** containing the schema's fields plus the exact caller `document_id`, not
a `fields` envelope. When the schema declares `document_id`, that property is
validated too; otherwise it is envelope metadata outside the field schema.

```python
import base64
from pathlib import Path

import httpx

schema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["patient_name", "date_of_birth", "balance"],
    "properties": {
        "patient_name": {"type": ["string", "null"], "description": "As it appears"},
        "date_of_birth": {"type": ["string", "null"], "format": "date"},
        "balance": {"type": ["number", "null"]},
    },
}
response = httpx.post(
    "http://127.0.0.1:8000/extract",
    json={
        "document_id": "DOC-001",
        "content": base64.b64encode(Path("document.png").read_bytes()).decode("ascii"),
        "content_format": "image_base64",
        "json_schema": schema,
    },
    timeout=60,
)
response.raise_for_status()
print(response.json())
```

Named fields, including nested containers, must permit null. This user-selected
policy reconciles strict validation with null for missing/unreadable facts.
Incompatible schemas get 422 before inference. Omitted fields are included as
null, never fabricated, and one invalid field does not blank its valid siblings.
Schema-wide contradictions that cannot be repaired without breaking the schema
produce a coded 502, not a purportedly valid record.

Supported images are single-page PNG, JPEG, WEBP, TIFF and GIF. Limits are 10 MiB
decoded and normalized image bytes and 20 million pixels. Native safe encodings
are retained; EXIF orientation is applied and transparency flattened onto white
when conversion is needed. Multi-page/animated files and PDFs are rejected with
422. Image decoding and schema/normalization work run off the event loop.

Schemas use draft-07, 2019-09 or 2020-12 and may contain local, acyclic JSON Pointer
references. Schema IDs, remote/dynamic/recursive references and legacy
`dependencies` are rejected. Limits are depth 24, 10,000 JSON nodes/expanded schema
visits, 100,000 characters for a schema string, and 20,000 per schema text value.
Schema objects also have a 100,000-character serialized limit; combined schema
text, including repeated text after reference expansion, is capped at 100,000.
Patterns are limited to 256 characters, no groups/alternation, and at most one
variable repetition anchored at the start; fixed repetitions are capped at
10,000. These restrictions prevent schema validation from becoming a network or
unbounded regular-expression execution surface.

The vision call uses a stable policy and JSON-object mode; arbitrary client
schemas are validated locally rather than restricted to the provider's strict
schema subset. Normalization preserves verbatim text and table order, converts
unambiguous numeric/currency/checkbox/date values, and uses null instead of
guessing ambiguous numeric dates. Do not expect post-processing to correct facts
the model read incorrectly.

`TRIAGE_EXTRACT_MODEL` selects the vision model on the shared configured endpoint.
Its default attempt budget is 15 seconds and total processing budget 45 seconds.
The default per-instance capacity is four extraction requests, admitted **before
body buffering**; excess work returns 503 with `Retry-After: 1`. Tune replicas and
capacity against provider quotas and measured memory/latency for 50-document
bursts; the defaults do not promise 50 simultaneous successful vision calls.

Extraction shares immutable Redis results and distributed leases. Cache keys
include the full image, document ID, schema and policy/model fingerprint. Images
and raw completions are not persisted; extracted records **are** and may include
health or financial information. Protect Redis, define retention and access
policies, and account for provider data handling before processing real records.
Operational logs include document ID, model, latency, usage and JSON Pointer
paths of null fields, not document images or extracted values.

## Workflow orchestration

`POST /orchestrate` accepts an opaque `task_id`, a `goal`, `available_tools`, and
optional `constraints` (a list of rules). Each tool has a name, description, exact
HTTP endpoint and parameter definitions (`name`, `type`, optional `required` and
`description`). Parameter types support string/str, int/integer, number/float,
bool/boolean, object and array/list. Unknown fields, duplicate names and excessive
request sizes are rejected. Harness `mock_service_url` metadata is accepted but
never fetched or used to rebuild tool URLs.

```sh
curl -sS http://127.0.0.1:8000/orchestrate \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "opaque-workflow-id-001",
    "goal": "Look up Example Corp in CRM and return what was found.",
    "available_tools": [{
      "name": "crm_search",
      "description": "Search CRM accounts by filter criteria",
      "endpoint": "https://tools.fdebench.dev/crm/search",
      "parameters": [
        {"name": "filter", "type": "string", "required": true}
      ]
    }],
    "constraints": []
  }'
```

The sample requires a functioning tool service at the supplied endpoint; the
benchmark harness provides scenario-specific URLs instead. Use those URLs
unchanged. No task/scenario ID or tool path is inferred.

The response echoes `task_id` and contains `status` (`completed`, `partial` or
`failed`), `steps_executed`, `reason`, and `finished`. Each typed step contains the
actual parameters, parsed result, HTTP status, success, attempt, timing and outcome.
Failed HTTP attempts are separate entries, not hidden behind a successful retry.
An interrupted request is explicitly `pending`/`unknown`, not a claimed success.
The model cannot fabricate trace entries or dispatch tools itself.

Ordinary planner/tool failures after a task is claimed return HTTP 200 with an
explicit `failed`/`partial` status and the retained trace. Input, capacity and
journal infrastructure failures use coded 4xx/5xx errors instead. Do not treat
HTTP 200 alone as goal completion. Token headers aggregate successful planning
rounds; orchestration does not return a cost estimate.

A stable planner proposes the next independent batch after observing real
results. The executor validates tool/parameter types, successful dependencies,
action caps, matching-customer prerequisites, result conditions, and final
per-action audit obligations. Mutation identifiers must have appeared in successful
tool data or explicitly in the trusted goal/constraints. Constraint policies are
compiled once, persisted and immutable. These checks enforce the **compiled
policy**, not proof that a model correctly interpreted every English rule.
Unsupported interpretations fail closed; live variations remain essential.

Tool egress uses an operator-controlled **exact origin allowlist**, HTTPS, safe
DNS resolution pinned to the connection, original Host/TLS SNI, separate origin
connection pools, and no redirects or environment proxies. Private/HTTP mock
services require their actual origin in **both** `TRIAGE_TOOL_ALLOWED_ORIGINS`
and `TRIAGE_TOOL_PRIVATE_ORIGINS`, for example `["http://127.0.0.1:9001"]`.
Do not grant untrusted callers network-wide tool access. Protect the ingress and
bind configured read-only tool names to trustworthy tool definitions.

The confirmed retry policy retries 429s, honoring `Retry-After`, and retries
5xx/transport errors only for operator-configured read-only or idempotent names.
New tool names need no dispatch code but are conservatively non-retryable by
default. Writes with uncertain outcomes are not replayed, even by a later plan.
The default budget is 55 seconds total with two seconds reserved for finalization,
eight seconds per model attempt, five per tool attempt, two attempts, 12 planning
rounds, 64 HTTP attempts and eight active workflows per instance. Independent
calls run concurrently; dependencies require a later observed-result round.
The body limit is 256 KiB and each tool response is bounded at 256 KiB. Planning
stops above a 2 MiB journal budget, which a final batch can exceed.
Capacity overflow is an explicit 503, not queued work or permission to replay.

Redis permanently claims each exact task ID **before any tool execution** and
persists intent before every POST, then its observed result. A repeated identical
request returns the stored snapshot without new actions, even after a model
change; reusing the ID with different input returns 409. An active/crashed
worker's snapshot has `finished: false`. Claims never expire or automatically
resume. A crash between a remote action and its journal update cannot prove the
remote outcome: an operator must reconcile it. This is a conservative no-replay
boundary, not transactional exactly-once execution. Never use a new task ID to
blindly retry an uncertain workflow.

The restricted Redis journal contains full customer parameters, results and
compiled policies; operational stdout contains task/tool/step names, parameter
names, outcomes, timing and usage, **not parameter/result values**. Tool audit
entries are still executed separately when required by the workflow. Protect
Redis with access controls, encryption in transit and durable no-eviction storage;
define reconciliation and retention before processing real customers. Deleting
or losing a claim can allow side effects again.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy triage
```

Tests replace the provider HTTP transport, not the production engine. They
exercise the API, validation, structured-output transport, retries and invariants
without sending signal data to an external service. They do not measure semantic
classification quality. Evaluate a representative labelled set with your chosen
model before relying on routing accuracy or adversarial robustness.

```sh
docker compose up --build
```

Put an authenticated, rate-limited TLS ingress in front of deployment. Keep API
keys in a secret manager, restrict provider egress, and configure ingress request
deadlines and concurrency limits. Use Redis authentication and `rediss://` outside
the private local Compose network. Operational logs contain ticket/request IDs,
timings, status and usage, not signal bodies, emails or provider credentials.

The default per-model-attempt timeout is eight seconds, overall triage deadline
20 seconds and per-process model concurrency 16. These are safety bounds, not
latency claims. Sub-second cold model responses and routing accuracy require live
measurement; cache hits avoid model latency.
