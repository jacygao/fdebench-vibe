# Architecture

## Current scope

One asynchronous FastAPI service implements mission-signal triage, schema-driven
document extraction and real HTTP workflow orchestration. All four requested
routes exist locally; deployment and live model evaluation are separate gates.

| Route | Implementation | Responsibility |
|---|---|---|
| `GET /health` | Implemented | Liveness and model-configuration state |
| `POST /triage` | Implemented | Typed signal classification and routing |
| `POST /extract` | Implemented | Vision extraction with caller-supplied field schema |
| `POST /orchestrate` | Implemented | Planned, guarded, journaled HTTP workflow execution |
| `GET /ready` | Implemented, additional route | Model credentials configured and Redis reachable |

`/health` does not prove model availability. `/ready` checks Redis but does not
make a paid model request or verify the credentials with the provider.

## Request flow

```text
Client / load balancer
        |
        v
Request context and body-size/content-type checks
        |
        v
FastAPI route -> Pydantic signal validation
        |
        v
Triage engine -> shared Redis decision lookup
        |                     |
        | cache miss          | cache hit
        v                     v
Capacity admission       Validated decision
and Redis lease
        |
        v
Async HTTPS model client + stable system policy
        |
        v
Structured-output validation and domain invariants
        |
        v
First committed decision saved in Redis
        |
        v
Typed response + model, request, usage and cache headers
```

## Module boundaries

| Module | Responsibility |
|---|---|
| `triage/api.py` | Lifecycle, routes, error responses, headers and request logging |
| `triage/models.py` | Signal/response contracts and exact domain enums |
| `triage/engine.py` | Cache coordination, bounded concurrency and routing invariants |
| `triage/llm.py` | Structured completions, pooled async HTTP, retries and token usage |
| `triage/system_prompt.txt` | Semantic classification and safety policy |
| `triage/store.py` | Shared immutable decisions and owner-safe expiring leases |
| `triage/settings.py` | Environment configuration and provider URL validation |
| `triage/middleware.py` | JSON content-type and 64 KiB request-body limits |
| `triage/observability.py` | Allowlisted structured operational logs |
| `triage/errors.py` | Stable domain error codes |
| `triage/extraction/models.py` | Typed extraction envelope and dynamic JSON record |
| `triage/extraction/images.py` | Base64, image verification, orientation and encoding |
| `triage/extraction/schemas.py` | Bounded local-only schema compilation and benchmark null compatibility |
| `triage/extraction/vision.py` | Vision message construction using the shared model client |
| `triage/extraction/system_prompt.txt` | Stable factual extraction/injection-boundary policy |
| `triage/extraction/normalization.py` | Field normalization, null isolation and final validation |
| `triage/extraction/engine.py` | Extraction deadlines, capacity, shared results and stage ordering |
| `triage/json_values.py` | Strict typed JSON parsing shared across dynamic tasks |
| `triage/orchestration/models.py` | Opaque workflow envelopes, typed steps and non-executable plan language |
| `triage/orchestration/planner.py` | Stable policy and bounded model-based next-batch proposals |
| `triage/orchestration/system_prompt.txt` | Goal/constraint interpretation and observation-only tool data |
| `triage/orchestration/guards.py` | Types, actual dependencies, limits, entity matching and final audit obligations |
| `triage/orchestration/dispatch.py` | Pinned, exact-endpoint POSTs, bounded bodies and safe internal retries |
| `triage/orchestration/journal.py` | Permanent task claims, immutable policy and sensitive write-ahead step evidence |
| `triage/orchestration/engine.py` | Bounded observe/plan/execute loop and honest terminal snapshots |

Extraction follows admission before body buffering, typed validation, schema
compilation, shared result lookup, image decoding/verification on a cache miss,
distributed lease, one vision call, per-field normalization, final schema
validation and immutable commit. CPU-heavy stages use worker threads. Safe
native encodings are preserved; other supported images are oriented/converted.
No image URL or schema URL is fetched.

Extraction uses JSON-object output rather than provider-strict JSON Schema
because arbitrary client schemas exceed that provider subset. Exact field-schema
validation against the effective schema is local and mandatory. `document_id` is application-owned metadata
outside the field schema unless explicitly included as one of its properties.
The user-approved compatibility policy permits null for named fields and nested
containers even when a caller supplies non-nullable types; constraints on
non-null values remain enforced, and `not`, `if`, `contains` and
`propertyNames` subschemas are not widened. Omitted, null or blank schemas use an open
object schema to retain visible document facts. Unknown request-envelope metadata
is ignored rather than becoming model input. Schema safety limits still apply.

Orchestration follows bounded admission, typed input and URL/DNS validation,
permanent task claim, model proposal, policy/parameter/dependency validation,
write-ahead intent, real HTTP calls, result persistence and another planning
round. Independent calls within a batch run concurrently. A dependent batch
requires already observed successful results. Each HTTP attempt gets its own
trace entry; the model cannot directly create executed-step records.

English constraints compile once into a typed guard language with no code
evaluation. Guards support caps, parameter/result comparisons, prerequisites
matched to the current entity, distinct successful checks and final obligations
for every source action. Per-action audits must start after the corresponding
action finished. Policies cannot be replaced in later rounds. This is mechanical
enforcement of a model-produced policy, not a proof of natural-language semantics.
Unsupported rules stop rather than receive a vacuous guard.

## State and consistency

Service instances share durable Redis. Decision keys hash the complete normalized
signal and model/policy/schema fingerprint, not just the ticket ID. The first
committed response remains authoritative while that record is retained. A Redis
lease prevents duplicate concurrent classifications; its release is conditional
on ownership, so an expired worker cannot remove a successor's lease.

There is no silent stateless fallback when Redis is unavailable. Raw signals are
not stored, but decisions include ticket IDs and action text and may contain
sensitive facts. Extraction results may contain health and financial records;
images and raw completions are not persisted. Storage retention, access controls and backups are operational
responsibilities. Store loss, eviction, retention deletion or a changed policy/model
can result in a new decision.

Workflow state deliberately differs from classification caching. A permanent
claim is keyed by the exact task ID, independent of model/prompt changes.
Conflicting inputs get 409. Identical duplicates return the stored snapshot,
including an unfinished one after a crash, without resuming side effects.
Intent is persisted before a POST and the observed result afterwards. An
interrupted write is unknown, not automatically retried. There is no distributed
transaction with the remote system and therefore no unconditional exactly-once
guarantee. Claim loss/deletion permits execution again and requires reconciliation.

Full workflow parameters, results and compiled policies are sensitive journal
data in Redis and in the caller's trace response. Operational logging emits only
task/tool/step identifiers, parameter names, outcome, duration and usage. This
separates evidence retention from ordinary log sinks, but does not remove the need
for authenticated ingress, Redis ACLs/TLS and controlled retention.

## Reliability and deployment

The default model-attempt timeout is eight seconds, total triage budget 20 seconds,
and per-process model concurrency 16. Busy work fails promptly with 503. Transient
provider failures retry within the remaining budget and honor `Retry-After`.
Invalid provider output or ownership conflicts produce explicit 502 responses.
P1 decisions always escalate.

Extraction defaults to a 15-second attempt budget, a 45-second total budget and
four active requests per instance. The middleware rejects excess work before
large bodies are buffered; the engine also bounds direct work admission. Each
worker may decode at most a 10 MiB, 20-million-pixel image. Total HTTP body size is
16 MiB. Leases and cache namespaces are independent per task, and extraction
fingerprints include the caller schema and full document input.

Orchestration defaults to 55 seconds including a two-second finalization reserve,
12 planning rounds, 64 tool HTTP attempts, eight-second planner attempts,
five-second tool attempts and eight active workflows. Tool response bodies are
bounded at 256 KiB, request bodies at 256 KiB and the next planning round is
refused once the journal exceeds 2 MiB. A final batch can grow beyond that threshold.
Capacity failures do not imply workflow replay is safe.

Tool requests use a separate credential-free client with environment proxies and
redirects disabled. Exact operator-approved origins are resolved before inference;
public destinations cannot resolve to private addresses. Connections pin a safe
IP while keeping the original HTTP Host, path/query and TLS SNI. Separate pools
per original origin prevent cross-host TLS reuse on a shared IP. Explicit private
origins support local benchmark mocks without broadly permitting private egress.
The request cannot expand these allowlists.

429 cooldowns survive later planning rounds. 5xx/transport errors retry only for
operator-configured safe tools. Uncertain writes and repeated identical mutations
are rejected, while successful safe retries retain both attempts in the trace.
Unexpected tool data is fed back as untrusted observations for authorized fallback
planning, never interpreted as executable code.

Docker Compose describes a non-root API container and persistent Redis. The API
must sit behind an authenticated, rate-limited TLS ingress with request deadlines.
Model traffic uses verified HTTPS without redirects. Attachment filenames and
embedded URLs are never fetched. Secrets come from environment configuration.

Container deployment, real Redis recovery, live model accuracy and latency are
not yet verified. See [evals.md](evals.md) and [methodology.md](methodology.md).
