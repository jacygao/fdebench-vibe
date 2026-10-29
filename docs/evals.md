# Evaluation and readiness

## Checklist status

| Required item | Status |
|---|---|
| `README.md` | Present |
| `docs/architecture.md` | Present |
| `docs/methodology.md` | Present |
| `docs/evals.md` | Present |
| `GET /health` | Implemented |
| `POST /triage` | Implemented |
| `POST /extract` | Implemented |
| `POST /orchestrate` | Implemented |

These items describe implementation readiness, not production approval.
All requested routes and documentation are implemented. The service is
**not production-approved** until the deployment/model checks below are completed.

## Recorded local evidence

The combined triage/extraction/orchestration suite passed **209 automated tests**
on Python 3.12.14. Ruff lint and formatting checks, strict mypy
over 25 source files and the dependency lockfile check also passed.

The suite executes the application with mocked provider HTTP transport and an
in-process Redis-compatible store supporting Lua scripts. Eight test cases start
real local HTTP socket servers. A parameterized cold-burst regression sends
50 simultaneous identical requests to each task endpoint, requires 50 valid
HTTP 200 responses and checks repeatability and no duplicate provider/tool work.
Triage descriptions and workflow goals contain zero-width, right-to-left and
emoji characters. The separate triage burst test sends eight concurrent
requests through two model slots, corrects schema, ownership or ticket-correlation
failures within one shared correction budget, and checks
stable caching and aggregate usage without overload rejection.
The orchestration socket test makes actual HTTP
tool POSTs through production DNS-pinned dispatch, not a mocked tool transport;
the model and Redis remain replaced. This is not a live-provider or
production-storage evaluation.

Coverage includes input contracts; malformed/empty/wrong-content-type/oversized
and chunked requests; structured provider output; refusals; invalid labels;
retry timing; total deadlines; ticket correlation; P1 escalation; ownership;
shared decisions across application instances; duplicate-work leases; queued
bursts; queue expiry without provider work or capacity leaks; one-shot decision
correction, strict persistent-failure rejection and correction deadlines;
body-receiving deadlines; duplicate waiter expiry without releasing the owner's lease;
late workflow admission without a permanent claim, followed by a successful same-ID retry;
storage outages; connection recovery after idle; headers; and log privacy.
Adversarial tests establish instruction/data separation, not successful semantic
resistance to injection by a real model.

Extraction coverage includes schema strings/objects, optional-schema preflight
requests, ordinary non-nullable fields with per-field null compatibility,
local references, nested tables and per-field failures, typed normalization,
all-null blank-page responses, supported formats, angled raster documents,
transparency, EXIF orientation, pixel/byte limits, multi-page rejection, provider
throttles and hard timeouts, shared duplicate leases/cache reuse, and a 50-request
burst with bounded queued success. An admission regression verifies that queue
waiting consumes the original extraction deadline and releases capacity on expiry.
The socket tests exercise both task endpoints
and malformed-schema rejection. Log checks verify document/model/latency/usage/
null-path metadata without image payloads or credentials.

Orchestration coverage includes exact task ID/opaque URL preservation, deriving
customer IDs from actual results, write-ahead intent before POST, parallel
warehouse checks, distinct-result prerequisites, subscription exclusions,
matching-entity guards, per-customer post-action audit requirements (including
rejection of concurrent audits started before an action completed), caps,
wrong/unknown parameters, invented identifiers, immutable policy coverage,
fallback when no calendar slots exist, 429 cooldowns, conservative 500/timeout
retries, redirects, malformed/oversized responses, partial sibling failures,
deadline snapshots, task conflicts, worker-crash claims, duplicate retrieval
after model/origin configuration changes,
storage failure before a side effect and queued workflows with bounded admission.
Log checks inspect actual formatted output without parameter/result values.
These are synthetic variations, not a score on the public replay dataset.

Reproduce the deterministic checks from the repository root:

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy triage
uv lock --check
```

## Live task evaluation

Synthetic live provider smoke checks have exercised triage, vision extraction
and planning with real HTTP calls to an isolated synthetic tool. Public HTTPS
liveness, readiness and task-route contract probes also succeeded. The evaluator
subsequently reported extraction preflight HTTP 422; the contract regression was
reproduced publicly before changing optional-schema and null-field handling.
The corrected public endpoint returned HTTP 200 for omitted/null schemas and
ordinary schemas in both string and object encodings, using live `gpt-4.1-mini`
vision calls. Responses preserved exact document IDs, visible invoice facts and
null missing fields; repeats returned identical cached records. A synthetic
invalid request confirmed field/type diagnostics without logging its value.

A subsequent evaluator run stopped during triage. Logs showed 40 capacity
rejections (503) and 12 invalid-model-response failures (502), not model
timeouts. After replacing immediate capacity rejection with deadline-bounded
queueing and adding one strict validation-correction attempt, a public burst of
50 fresh synthetic signals using the real provider and Redis returned 50 HTTP
200 responses in 6.84 seconds overall; the slowest request took 6.81 seconds.
All 50 repeats returned identical cached decisions with zero new model tokens.
No invalid decisions occurred during that burst; correction behavior is covered
by the provider-mocked regressions above.

The subsequently supplied resilience contract requires at least 48 valid
responses to a 50-request burst. A cold burst of 50 **identical** triage requests
reproduced a different failure: one HTTP 200 and 49 in-progress-lease HTTP 503s.
Real-socket regressions reproduced rejection on all three endpoints before the
shared-result waiting and deadline-bounded admission fixes.

After those fixes and a follow-up self-review, public HTTPS checks using live
models and Redis on 2026-10-29 UTC recorded:

| Endpoint | Cold identical burst with all response fields | Slowest response |
|---|---|---|
| `/triage` | 50/50 HTTP 200 | 2.14 seconds |
| `/extract` | 50/50 HTTP 200 | 1.56 seconds |
| `/orchestrate` | 50/50 HTTP 200 | 5.32 seconds |

Each burst started one producer; 49 requests reused cached results or durable
workflow snapshots. Malformed JSON and empty
objects returned 422, wrong content types returned 415, and a 1 MiB unknown field
returned 413 for triage/orchestration and a valid cached 200 for extraction.
Unicode triage descriptions and workflow goals were accepted. Three repetitions
were identical, and all task endpoints returned valid 200 responses after five
seconds idle.

Additional cold bursts changed every request ID to require distinct work rather
than reuse a shared result:

| Endpoint and setting | Cold unique burst | Slowest response |
|---|---|---|
| Public `/triage` | 50/50 valid HTTP 200, no cache hits | 6.45 seconds |
| Public `/extract` | 50/50 valid HTTP 200, no cache hits | 12.83 seconds |
| Isolated `/orchestrate` with a controlled HTTP tool | 50/50 completed, no cache hits | 21.26 seconds |

The self-review also reproduced HTTP 502 `MODEL_ROUTING_CONFLICT` on the
unchanged public `SIG-2954` request. Schema validation received a correction
attempt, but engine ownership/ticket checks ran afterward and bypassed it.
Both checks now participate in the same one-shot correction, original deadline
and usage accounting. Persistent failures still return explicit 502s without
caching or rewriting teams. The formerly failing request returned a valid
HTTP 200 in 3.51 seconds after the fix. Real-socket regressions reproduced
ownership and ticket failures before the fix and now pass.

The validation gate found another missed edge case: a delayed request could
permanently claim a workflow after its execution budget had elapsed, consuming
the time reserved for finalization. A pre-claim deadline check now rejects that
unstarted work with 504. The regression verifies no planner/tool work or journal
claim and a successful same-ID retry. The affected public and controlled-tool
workflow probes were repeated after applying that guard to the running API.

The public workflow numbers establish the supplied HTTP/response-field contract, not
successful completion: the isolated read-only synthetic tool request returned
404 because the evaluator service uses opaque registered scenario IDs. Its
failure remained explicit in the final trace, with one attempted tool call and
no replay. A prior synthetic plan was also rejected by the existing guards.
To verify completion separately, an isolated instance used the real model and
Redis with one exactly authorized, controlled local HTTP tool; production
destination authorization was unchanged. Its cold identical burst produced
50 valid answers and one completed workflow with exactly one actual tool call.
Its cold unique burst completed all 50 workflows with exactly 50 additional
tool calls. Successful completion against the deployed evaluator service
remains unverified without its valid tool definitions.
These checks do not establish that ngrok exhausted a quota
or that the original hosted scoring failure has been resolved.

No successful public/hidden benchmark score or labelled live-model evaluation
is claimed. Neither synthetic checks nor mocked contract tests establish
classification quality or the customer's under-15% misrouting target.

After configuring a vision/text-capable provider as required by each implemented
task and durable Redis, run the existing
[FDEBench evaluation harness](https://github.com/vigeantjake-msft/fde-hackathon-solution/tree/main/py/apps/eval).
Its published Python workspace supports `make eval-triage`, targeting a service
on port 8000. Obtain the public data and tooling under their source licensing;
they are not bundled in this repository. Run official Task 2 fixtures unchanged
and report schema rejections separately. Ordinary non-nullable field schemas
and missing/null schemas are accepted under the user-approved compatibility
policy; null handling occurs inside the service, not by modifying test fixtures.
Never present results on modified fixtures as official benchmark scores.
The published dataset is about 71 MB; it was not downloaded or evaluated here.
For Task 3, authorize the mock service's actual origin explicitly in both tool
origin settings when it is local/private, and use the exact endpoints supplied
by the harness. `mock_service_url` is never used to reconstruct URLs.

For triage, resolution weights are category 24%, priority 24%, routing 24%,
missing-information 17% and escalation 11%. Category and routing use macro F1;
priority gets ordinal partial credit; missing information is compared as a set
per signal; escalation uses binary F1. Empty missing-information lists are valid.
Use the benchmark harness for its exact metric implementation.

Record model and policy version, dataset revision, per-dimension scores, malformed
output/failure rates, tokens, estimated cost, and cold/warm p50/p95 latency.
Include noisy threads, contradictory context, ambiguous ownership, injection
attempts, concurrency and requests after idle. The 50 public signals are a small
sample; small score differences are not strong evidence.

For extraction, record field-by-field information accuracy (70% of resolution)
and text fidelity (30%). Numeric information gets 1% tolerance; lists are aligned
item by item. Correct nulls earn full marks; guesses do not. Include nested
schemas, multiple aligned tables, blank pages, poor scans, phone photos,
handwriting and injection text in the live evaluation. Report input-schema
rejections separately from missing/incorrect model values.

For orchestration, score actual tool calls, parameter values, ordering,
constraint compliance and final system state. Goal completion and ordering
each represent 20% of resolution; constraint compliance is the largest
dimension. The public 50-workflow service replays its own answer keys, so a
near-perfect score would establish wiring, not generalization. Evaluate separate
data/constraint/failure variations and semantic policy translations with the
live planner. Do not equate mocked plans or injection-message separation with
successful real-model judgment.

## Deployment and release gates

| Check | Evidence still needed |
|---|---|
| Container image and Compose startup | Docker unavailable in the current WSL environment |
| Real Redis durability and recovery | Automated tests use an in-process substitute |
| Live model correctness and injection resistance | Configured provider and labelled evaluation |
| Sub-second cold-response aspiration | Representative live latency measurements |
| Customer routing targets | Representative operational evaluation |
| Production ingress | Authentication, authorization, TLS, rate limits and deadlines |
| CI | No remote CI result is asserted |
| Workflow recovery and side-effect durability | Real tool/Redis crash reconciliation; no automatic replay |
| English constraint interpretation | Live variations and policy/goal correctness review |

The benchmark allows 60 seconds per call. Tasks 1/2 retry 429/server errors twice;
Task 3 is **never retried** and its first response is final. Default service
budgets are 20 seconds for triage, 45 for extraction and 55 for orchestration.
Full benchmark performance combines resolution 50%, efficiency 20% and robustness
30%; completing route names alone does not demonstrate readiness.
