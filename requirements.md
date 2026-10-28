# Triage, extraction and orchestration requirements

## Evidence and scope

The supplied videos are titled:

1. [The Customer Problem (Cmdr. Kapoor)](https://www.youtube.com/watch?v=bJhchkU7T5o)
2. [What "Good" Looks Like (Customer Architect)](https://www.youtube.com/watch?v=G4LYNFlOSJY)
3. [How to approach the task (FDE)](https://www.youtube.com/watch?v=zhPAGdiCm7Y)

YouTube metadata was accessible, but caption requests returned empty responses.
The user subsequently supplied transcripts for all three videos. These transcripts
are the primary requirements source (V1, V2, V3), supplemented by the exact
vocabularies and schemas in the matching public challenge documents below.
No video transcription or quoted video footage is bundled.

Written sources:

- [S1: Task and label contract](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/docs/challenge/task1/README.md)
- [S2: Customer brief](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/docs/challenge/task1/customer_brief.md)
- [S3: Routing guide](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/docs/challenge/task1/routing_guide.md)
- [S4: Engineering expectations](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/docs/challenge/task1/engineering_review.md)
- [S5: Input schema](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/py/data/task1/input_schema.json)
- [S6: Output schema](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/py/data/task1/output_schema.json)

The customer is station Mission Ops, handling roughly 180 signals daily for
2,000 crew. Business targets are routing within five minutes and misrouting below
15%, compared with reported baselines of 3.4 hours and 42%. Each clarification
incurs at least eight minutes of relay round-trip delay (V1, 0:42-0:57).
These are evaluation targets, not measured results of this implementation.

Initial scope was first-pass JSON triage, explicitly confirmed after V3.
The later Task 2 and Task 3 transcripts extend scope to schema-driven image
extraction and HTTP workflow orchestration in the same service. Chatbot
interaction, attachment fetching and direct equipment control remain out of scope. Shared immutable
result persistence in Redis follows the user-approved triage architecture.

## Requirements and acceptance intent

All requirements are Must unless marked Should.

| ID | Requirement | Acceptance criterion | Source |
|---|---|---|---|
| FR-001 | Accept `POST /triage` with the signal contract. | Given a valid signal, when posted, then return all eight decision fields with the original ticket ID. | V1 0:57, V2 0:32, S5, S6 |
| FR-002 | Restrict categories, priorities, teams and missing-information labels to the published vocabulary. | Given an invalid model label, when parsed, then return a coded upstream error rather than an invalid decision. | S1, S6 |
| FR-003 | Classify using complete incident context and specialist ownership. | Given contradictory subjects, multiple issues or gray-area devices, when classified, then prioritize concrete evidence and the most safety-critical active issue. | S2, S3 |
| FR-004 | Use P1 for credible hull, atmosphere, decompression, containment, life-support or command emergencies. | Given a calm active hazard report, when classified, then choose P1 and escalation. | S2, S3 |
| FR-005 | Escalate P1, threat, life-support, trajectory, unauthorized-access, command-reporter and unresolved recurring cases. | Given a P1 model decision without escalation, when normalized, then escalation is true. Contextual escalation is specified in the semantic policy. | S3 |
| FR-006 | Distinguish noise from incidents and ordinary requests. | Given pure non-incident noise, when classified, then use P4, None and no operational remediation or missing-information requests. | S1, S2, S6 |
| FR-007 | Request only relevant missing facts not already supplied. | Given reporter contact and a log attachment name, when classified, then do not ask for those same facts or pretend to have read the file. | S1, S5 |
| FR-008 | Return a concrete next action and ordered, situation-specific remediation. | Given a critical incident, when classified, then prioritize qualified responders and approved procedures without delaying for missing details. | S1, S2 |
| NFR-001 | Reject malformed input before invoking a model. | Given malformed JSON, an empty body, wrong content type, invalid fields or oversized requests, when posted, then return 422, 415 or 413 with no provider call. | V2 1:23, V3 2:22, S5 |
| NFR-002 | Treat every signal field as untrusted data. | Given embedded role changes or routing instructions, when forwarded, then preserve them only in a JSON user-data message, never in the system policy. Semantic resistance still requires live adversarial evaluation. | S1, S4 |
| NFR-003 | Separate routes, engine, typed models, prompt, transport and settings. | Inspection shows independent modules and provider transport injection for tests. | S4 |
| NFR-004 | Bound provider retries and honor `Retry-After`. | Given a throttle, when its delay fits the remaining request budget, then wait at least that delay; otherwise return 503 with `Retry-After`. Each model attempt is at most 45 seconds and the full operation at most 50 seconds, below the benchmark's 60-second limit. Defaults are 8 and 20 seconds. | V2 1:42, V3 2:47 |
| NFR-005 | Use pooled async HTTPS provider calls with environment-managed secrets. | Given insecure provider URLs, when configured, then fail validation; concurrent requests do not share per-signal state. | S4 |
| NFR-006 | Expose health, readiness, correlation IDs, structured logs and usage headers. | Given any HTTP response, then include `X-Model-Name`; completed triage logs include ticket ID, latency and tokens without report bodies or credentials. Estimated cost is a Should when rates are configured. | V2 2:06, S4 |
| NFR-007 | Support container deployment and reproducible dependency installation. | Given the lockfile and Dockerfile, when deployed, then serve on port 8000 as a non-root user. | S4 |
| NFR-008 | Return the same committed decision for the same signal under the same policy/model. | Given multiple instances sharing durable Redis, when an identical request repeats or a worker restarts, then return the stored decision without another model call. Changed payloads or policies must not reuse stale decisions. | V2 1:58, user-approved Redis |
| NFR-009 | Target sub-second responses and support bursts. | Given the deployed provider and expected load, measure cold/warm p50 and p95 against a one-second target; when model slots are full, fail promptly with 503 rather than queue indefinitely. This target is not yet demonstrated with a live model. | V2 1:23, V3 1:56 |
| NFR-010 | Keep the system policy stable and use one structured completion per uncached signal. | Given different valid signals, when forwarded, then the system message is identical and only user data varies. Retries are limited to transient failures. | V3 0:43 |
| NFR-011 | Document methodology and verify API robustness. | Given the repository, then it contains an honest methodology and tests covering malformed/empty/wrong-type/oversized requests, concurrency, and warm requests after idle. | V3 2:22-3:53 |

## Decisions and limits

- Python/FastAPI/Pydantic follows V2 and the written engineering expectations. OpenAI-
  compatible structured output supplies semantic judgment; local keyword routing
  is not substituted for unavailable models.
- Missing provider credentials or unavailable Redis produce 503. No model or
  secret is bundled. Redis failures are not bypassed with inconsistent guesses.
- The service validates ownership coherence and always escalates P1. It rejects
  conflicting ownership instead of silently rewriting a team's action plan.
- Input length limits and rejection of unknown fields are deliberate hardening
  beyond the permissive published JSON schema. Empty subject/description remain
  valid; attachments are filenames only.
- Pure noise uses no operational actions. Informational requests retain a real
  specialist owner.
- Redis stores the decision, including ticket ID and action text, under a hash
  of the complete signal and policy/model version. Raw signals are not stored.
  Decision values may still contain sensitive facts and require protected storage.
  Durability and repeatability depend on retaining the store without eviction;
  operators must define retention and access policies. Deletion or model/policy
  changes can produce a new decision. Redis loss is not an accuracy fallback.
- Token costs are estimates from operator-supplied prices; provider prompt-cache
  discounts and failed/retried calls are not included. Decision-cache hits report
  zero new tokens and `X-Decision-Cache: hit`.
- Production ingress must supply authentication, authorization, rate limiting,
  TLS and slow-client protections. This service is not safe to expose anonymously
  as a paid public model proxy.
- Contract/transport/invariant behavior is covered by local tests. Semantic
  accuracy, injection resistance and business targets require a configured model
  and a representative labelled evaluation set before production approval.

## Evaluation requirements

V3 specifies Tier 1 as resolution 50%, efficiency 20% and robustness 30% on roughly
1,000 hidden signals. Resolution uses macro F1 for category/routing, ordinal
partial credit for priority, per-signal set comparison for missing information
(17% of resolution), and binary escalation. The written task's resolution weights
are 24% category, 24% priority, 24% routing, 17% missing information, 11% escalation.
Empty missing-information lists are valid and common.

The benchmark allows 60 seconds per call and retries 429/server errors twice
before assigning zero. V3's engineering review names four equally weighted
reviewers: code quality, architecture, AI problem solving and engineering maturity.
This newer transcript takes precedence over the different Tier 2 weighting in S4.
The public 50-signal dataset and existing harness are evaluation starting points,
not evidence that this service achieves the targets. See
[methodology](docs/methodology.md) and [readiness](docs/evals.md).

## Task 2: document extraction

The three new user-supplied transcripts are E1 (Marcus Reid, document operations),
E2 (platform architect) and E3 (FDE benchmark briefing). They are the primary
source; the published
[Task 2 input schema](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/py/data/task2/input_schema.json)
confirms request names. E1 covers clinics, leases, finance, HR and inspections,
including phone photos, faxes, handwriting and aligned tables. These are intended
operating conditions, not demonstrated model-accuracy results.

| ID | Requirement and acceptance intent | Source |
|---|---|---|
| FR-009 | Given a valid document image and dynamic schema, `POST /extract` returns its fields with the exact caller document ID. Names, nesting and JSON types match the schema. | E1 0:51-1:40; E2 0:43-1:15 |
| FR-010 | Given missing/unreadable data, include its requested key as null without inventing a value. Preserve good sibling fields and table rows when another field fails. | E1 1:40-2:31; E2 0:58, 2:43 |
| FR-011 | Given a field description, preserve verbatim text or apply its requested formatting. Normalize numbers, currency, dates and booleans conservatively. | E1 1:16-1:40; E2 1:00 |
| FR-012 | Given an identical document, schema and policy/model, retries across service instances return the same committed record without another vision call. | E1 2:38; E2 1:46 |
| NFR-012 | Given malformed JSON, invalid/empty input, wrong content type, invalid image or excessive size, return a clean 4xx before inference. Image validation is a separate stage. | E2 1:21; E3 0:20, 2:27 |
| NFR-013 | Given a valid uncached document, use one vision completion with a stable system policy; only transient failures trigger bounded retries honoring `Retry-After`. Total processing remains below 60 seconds. | E2 1:31; E3 0:29-0:57, 2:46 |
| NFR-014 | Given a burst of 50 documents, keep resource use bounded and return explicit retryable capacity failures instead of crashing or hanging. | E2 1:46; E3 2:27 |
| NFR-015 | Given any extraction response, include `X-Model-Name`. Logs identify document/model, latency, usage and null-field paths without image/value disclosure. | E2 2:01-2:22 |
| NFR-016 | Given instructions printed in a document or embedded in schema prose, treat them only as data. Schema descriptions may define field meaning/format, not change system policy. | E2 2:22; E3 0:29 |
| NFR-017 | Keep image handling, schema compilation, vision transport, normalization and orchestration separate. Validate normalized output before returning it and test each stage. | E2 2:34-3:08; E3 0:20-0:57 |

### Confirmed extraction policy and implementation limits

The user selected **require nullable fields** rather than return schema-invalid
nulls. Every named requested field, including containers, must permit null, except
root `document_id`, which the application supplies exactly. Incompatible schemas
receive 422 before inference. This is intentionally stricter than the permissive
published input contract, which permits omitted/null `json_schema`; extraction
without a requested field definition is rejected rather than improvised.

Limits are a 16 MiB HTTP body, 10 MiB input/normalized image and 20 million pixels.
Supported single-page images and bounded JSON Schema features are listed in
[README](README.md#document-extraction). The same endpoint credentials serve both
tasks; extraction has a separately selectable vision-capable model.

E3 specifies roughly 500 hidden documents and 50 public documents. Resolution is
50% of Tier 1, comprising information accuracy 70% and text fidelity 30%;
efficiency is 20% and robustness 30%. Numeric information gets 1% tolerance,
lists are compared item by item, and correct nulls earn full credit. Four equally
weighted engineering perspectives assess code quality, architecture, AI problem
solving and maturity. No extraction accuracy, latency or benchmark score is
claimed from mocked tests.

## Task 3: workflow orchestration

The three supplied transcripts are O1 (Dana Whitfield, operations), O2 (systems
architect) and O3 (FDE benchmark briefing). O1 describes onboarding, renewal,
reviews, win-back, churn routing, restocking and incidents across CRM,
subscriptions, calendars, email, notifications, inventory and audit systems.
The published
[input](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/py/data/task3/input_schema.json)
and [output](https://github.com/vigeantjake-msft/fde-hackathon-solution/blob/main/py/data/task3/output_schema.json)
schemas confirm exact contract names. No answer-key implementation is used.

| ID | Requirement and acceptance intent | Source |
|---|---|---|
| FR-013 | Given a goal, allowed tools and constraints, `POST /orchestrate` executes actual HTTP POSTs and returns the exact opaque task ID, status and typed step results. | O1 0:44-1:12; O2 0:37-1:10 |
| FR-014 | Given dependent work, use actual preceding results to derive parameters and select the correct customer. Execute independent calls in parallel, never dependent calls before their prerequisites. | O1 1:19, 2:14; O2 2:56; O3 0:27 |
| FR-015 | Given action limits, subscription exclusions or warehouse prerequisites, enforce the immutable compiled guards before dispatch. Include required audit and follow-up actions before reporting completion. | O1 1:26-1:49; O3 1:25-1:49 |
| FR-016 | Given a surprising result or per-step failure, observe it and replan an authorized fallback rather than fabricate success, invent calendar slots or crash the whole run. | O1 1:49; O2 1:32; O3 0:27-0:45 |
| FR-017 | Given a tool 429, honor its retry delay across attempts and replans. Retry 5xx/transport failures only when the operator has established retry safety, otherwise preserve uncertainty. | O1 1:58; O2 1:32-1:48; confirmed user policy |
| FR-018 | Given repeated/concurrent requests with the same task ID, never replay retained workflow side effects. Return the journal snapshot; reject conflicting payloads for the ID. | O2 1:16-1:32; O3 2:46 |
| NFR-018 | Given any supplied tool URL, permit only its exact approved origin and safe pinned destination. Preserve opaque paths/query strings, do not follow redirects, and never reconstruct URLs from IDs. | O2 1:00, 2:09; O3 2:54-3:03 |
| NFR-019 | Given a workflow, bound total execution below 60 seconds, model rounds, HTTP attempts, response bytes and concurrency; retain explicit partial/failed results on deadline or budget exhaustion. | O2 1:48; O3 2:38-2:54 |
| NFR-020 | Given malformed/empty/wrong-type/oversized input or a burst, fail cleanly before external work without unbounded buffering/queues. | O3 2:22-2:38 |
| NFR-021 | Given any attempted step, persist task ID, tool, parameters, observed result and timing in a protected journal. Log operational metadata without customer values and expose model/request/usage headers. | O2 1:55-2:18 |
| NFR-022 | Keep planner, executor, generic tool dispatch, typed trace models and policy separate. Never run model-generated code or accept tool-result instructions as policy. | O2 2:09-2:56; O3 3:09 |
| NFR-023 | Test with real HTTP wiring and mocked systems, including rate limits, 500s, inactive subscriptions, no slots, tighter caps and changed data. Treat the public replay service as wiring evidence only. | O2 2:44; O3 3:51-4:16 |

### Confirmed orchestration policy and limits

The user confirmed conservative retries: 429 backoff, but 5xx/timeouts only for
operator-configured read-only/idempotent tools, plus an exact-origin URL allowlist
with explicit private mock origins. Request-supplied metadata cannot grant extra
network access or retry safety.

The planner compiles each English constraint into immutable typed guards.
The executor enforces those guards, advertised parameters, observed identifiers,
dependency results and per-action final obligations. It cannot prove the semantic
correctness of the model's translation; unsupported rules fail closed and live
evaluation must test mistranslations. No arbitrary Python, expressions or URL
selection from model output is supported.

Task claims have no expiry and are not tied to model/prompt versions. Intent is
written before a tool call and the result afterwards. There is deliberately no
automatic recovery of uncertain writes. Crashes, lost Redis state and external
non-transactional tools prevent an unconditional exactly-once guarantee.
Sensitive parameters/results live in the protected Redis journal, not stdout.
Limits and configuration are documented in [README](README.md#workflow-orchestration).

O3 specifies roughly 500 hidden workflows, with resolution 50%, efficiency 20%
and robustness 30%. Goal completion and ordering each account for 20% of
resolution; constraint compliance is its largest dimension. The evaluator scores
actual endpoint calls, not claimed traces. Requests are never retried and the
first response, including a timeout, is final. The 50 public workflows replay
their own answer keys, so a high local replay score is not evidence of hidden-set
generalization. No local or hidden benchmark score is claimed.
