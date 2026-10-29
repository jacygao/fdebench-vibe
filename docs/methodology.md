# Triage, extraction and orchestration methodology

## Evidence and scope

The repository was initially empty. YouTube metadata established that the three
links concern Task 1; caption retrieval returned empty responses, so it could not
supply reliable requirements. Matching public challenge briefs supplied the
initial exact contracts. The user then supplied all three transcripts. These
overrode the written briefs where they were more specific, notably retry timing,
model headers, logging, repeatability, latency and engineering-review weighting.
The user initially approved Redis and limited implementation to triage. The next
three supplied transcripts extended scope to document extraction. The final
three supplied transcripts extended scope to stateful HTTP workflow execution.

## Approach

The API validates input before external calls. A single structured completion
per uncached signal performs semantic classification rather than keyword routing.
The stable system policy records team ownership, ambiguous-device examples,
priority rules, escalation, missing-information relevance and safe next actions.
Signal fields are serialized only as untrusted user-message JSON. Attachment
filenames and embedded URLs are not retrieved.

`gpt-4.1-mini` is a configurable starting point for short structured decisions:
a compact model is appropriate for the latency/cost objective. It has not been
shown to outperform other models here. Temperature zero and a fixed seed reduce
sampling variability but do not guarantee deterministic inference. A shared
Redis store therefore makes the first committed decision authoritative for a
complete signal and policy/model fingerprint. Redis stores decisions, not raw
reports; decision text can still contain sensitive information.

The engine checks ticket correlation and ownership coherence and enforces P1
escalation. It rejects invalid labels, contradictory decisions and malformed
provider output. It does not silently rewrite category/team disagreements,
because an action plan for the original team could become unsafe.

Redis leases coordinate duplicate work across instances. Per-process model
capacity is bounded with minimal queueing. Transient failures retry within a
20-second default total budget; provider `Retry-After` wins over local backoff.
Long retry delays are returned as 503 instead of sleeping beyond the deadline.
Unavailable storage fails explicitly rather than weakening repeatability.

## Extraction approach

The implementation adds stages rather than an unmeasured multi-model pipeline:
typed request, bounded schema compilation, verified image input, one vision
completion, conservative normalization, schema validation and immutable storage.
`gpt-4.1-mini` is a configurable vision-capable starting point, not a measured
winner. It shares endpoint credentials and retry transport with triage while
having independent model selection, deadlines and capacity.

Unreadable fields must be null, but ordinary non-nullable schemas forbid that.
The initial user-selected strict policy rejected such schemas. After an
evaluation preflight failed with HTTP 422, public HTTP reproductions and the
published request contract also established that omitted/null schemas were being
rejected even though the evaluator permits them. The user approved replacing the
strict policy with benchmark compatibility: named fields and containers permit
null in an effective schema while non-null value constraints remain enforced.
Missing, null or blank schemas request visible document facts using an open
object schema; blank pages return only the application-owned document ID.
Missing requested keys become null, valid siblings survive a bad scalar, and
table order/duplicate rows are retained. Contradictory schema-wide constraints
may still require a coded failure.

Provider JSON mode handles arbitrary client schemas; the complete client schema
is passed as user data and validated locally. A fixed extraction policy defines
fact-only transcription, missing versus unchecked boxes, aligned rows and
instruction/data separation. Schema descriptions can specify field meaning or
format but cannot override policy. Strict provider JSON-schema mode was not
reused because it accepts a narrower schema subset than this dynamic contract.

Image handling verifies real bytes and bounds dimensions before decoding.
Preserving safe native JPEG/PNG/WEBP avoids inflating phone photos into oversized
PNGs. Conversion applies EXIF orientation and composites transparency on white
instead of hiding black text on a black background. No OCR engine, deskew model,
image-resizing heuristic or second-pass model has been added without evaluation.

Normalization preserves text by default and changes only recognized typed values.
It handles currency grouping/negative parentheses, integers, explicit checkbox
values and ISO/English/unambiguous numeric dates. Ambiguous numeric dates become
null rather than choosing an unstated locale. Pattern constraints are restricted
to a simple bounded profile; remote and recursive schema references are rejected
to avoid network access and unbounded validation. These are documented API limits,
not claims of supporting every JSON Schema construct.

Extraction reuses Redis first-commit semantics with a separate task/policy
namespace. A changed schema cannot reuse a stale record. Capacity admission occurs
before buffering large requests; overflow gets 503 with a retry delay. Default
capacity is a safety bound, not a claim that one instance can complete 50 simultaneous
vision calls. Real replicas, provider quotas and memory must be sized from measurements.

## Orchestration approach

The final brief requires executing, not narrating. A capable `gpt-4.1` planner
proposes one independent batch at a time; ordinary asynchronous Python dispatches
the actual supplied HTTP endpoints, records the observed responses and replans.
The model choice is configurable and has not won a measured comparison here.
There is no heavyweight agent framework, model-generated Python, fabricated tool
trace, answer-key lookup or task-ID parsing. A cheaper second model has not been
added without evidence that it improves the latency/accuracy tradeoff.

Each English constraint must receive an executable typed policy in the first
plan. The policy is persisted and cannot be changed later. The executor checks
parameter names/types, actual completed dependencies, action caps across attempts,
entity-matched prerequisites, result facts and per-action final obligations.
An audit for one customer cannot satisfy another customer's email obligation;
an audit before the action finished cannot count as its follow-up. Duplicate
reads cannot stand in for distinct warehouse checks. Mutation identifiers must
be supplied explicitly or observed in successful tool results.

These are deterministic checks on the **compiled policy**. A model can still
mistranslate a natural-language rule or choose the wrong customer from ambiguous
search results. The implementation is not a formal verifier of arbitrary
English. Unsupported interpretations fail closed; representative live
variations and careful review of constraint translations remain release gates.
Tool-result instructions stay in the observation message, never in the policy.

The user confirmed conservative retry and URL policies. 429s are treated as
rejections and honor cooldowns across attempts/replans; writes are not retried on
5xx/timeouts unless the operator has explicitly established idempotence.
Configured read-only names may retry. The trusted invoker/operator must ensure
those names really designate safe tools. Unknown names remain usable without
new dispatch code but default to non-retryable. A repeated mutation or uncertain
write cannot be silently reissued by the planner.

Endpoints remain opaque. Only exact approved origins are accepted; public DNS
answers must be globally routable and are pinned to the connection. The original
Host, TLS SNI and raw path/query are retained. Origin-isolated connection pools
prevent sharing a TLS connection between different hostnames on the same IP.
Redirects, provider credentials on tools and environment proxies are excluded.
Local harness origins require explicit private-origin configuration rather than
a blanket SSRF bypass.

Stateful workflows cannot use classification's expiring lease. Redis permanently
claims the exact task ID, irrespective of model changes, then stores intent
before every tool POST and the observed result afterwards. Duplicates receive
the snapshot, not another execution. Worker crashes never release claims for
automatic recovery. This deliberately sacrifices automatic liveness for
conservative no-replay behavior. A crash between remote action and journal update
leaves uncertainty; exactly-once behavior would require remote idempotency or a
transaction protocol. No such guarantee is claimed.

Full parameters/results belong to the access-controlled Redis journal and caller
trace, while ordinary stdout contains only operational metadata. Business audit
tools are still called when required; an application log is not substituted.
The 55-second total budget reserves two seconds for finalization and bounds
rounds, attempts, parallel work and response sizes. There is no workflow-level
retry. Partial results are explicit and a safe retry may resolve a read's
transport uncertainty without erasing its failed attempt.

## What did not work or is not proven

- Direct YouTube caption retrieval did not work; the supplied transcripts resolved
  that evidence gap. No claim is made to have watched the videos.
- A whitespace-only next-action response initially passed validation. An
  endpoint-level regression test reproduced it; action-text validation now rejects it.
- The Redis async scripting API's shared sync/async annotations did not pass strict
  typing. Registering an asynchronous script preserves atomic, owner-safe lease
  release without unsafe type casts.
- A malformed extraction `$schema` value initially returned 500. A real HTTP
  socket test reproduced it; schema validation now returns a clean 422.
- Evaluation requests reached envelope validation but not the extraction route.
  Existing logs did not retain validation-field diagnostics, and ngrok inspection
  was disabled, so the exact historical payload was not recoverable. Omitted/null
  schema HTTP reproductions exposed a contract mismatch. Validation failures now
  log recognized field names and error types without rejected document values.
- The initial reference expansion could lose a referenced scalar type when
  sibling metadata/composition was present. A focused normalization regression
  now covers that case, including preserving an already-valid string union value.
- Initial orchestration API tests reproduced 500 responses for redirects and
  malformed tool JSON. Strict JSON parser failures now produce explicit failed
  or uncertain step records, not an uncaught parser exception.
- Tool retries alone did not prevent a later plan from bypassing a long 429
  delay. The dispatcher now checks persisted cooldowns on every attempt/replan.
  Mutation replay prevention is independent of model compliance.
- Docker Desktop is not integrated into this WSL environment, so an image build
  could not be verified here. Python 3.12, the image's runtime family, was exercised
  independently.
- Provider credentials were subsequently configured and synthetic live triage,
  vision extraction and planner/HTTP-tool smoke checks ran successfully.
  These do not establish labelled accuracy, safety judgment, prompt-injection
  resistance or representative cold-response latency. No benchmark scores are claimed.
- Synthetic blank/angled documents, table-like content, transparent pixels and
  orientation tags exercise the image and contract stages only. Mocked vision
  answers cannot establish OCR accuracy on real faxes, handwriting or statements.
- Redis in automated tests is an in-process compatible implementation, including
  Lua scripts. Real Redis deployment, failover durability and protected production
  ingress still require deployment verification.

## Verification and release evaluation

See [evals.md](evals.md) for recorded evidence, commands and remaining readiness gaps.

Local tests cover typed contracts, malformed/empty/wrong-content-type/oversized
requests, chunked input, model errors/refusals, timeouts, throttles, Retry-After,
P1 escalation, injected data separation, shared cache behavior, concurrent bursts,
duplicate signals across instances and an actual HTTP socket server. Static checks
use Ruff and strict mypy.

Before production approval:

1. Configure the chosen model and durable Redis, then run the public 50-signal
   evaluation with gold labels using the existing
   [FDEBench harness](https://github.com/vigeantjake-msft/fde-hackathon-solution/tree/main/py/apps/eval).
   Its published `make eval-triage` target calls a service on port 8000. Respect
   the source repository's licensing when obtaining its data and tooling.
2. Record resolution by dimension, including rare-label macro F1, priority partial
   credit, per-signal missing-information set F1 and escalation F1. Do not tune
   against hidden labels or mistake a two-point change on 50 samples for certainty.
3. Measure cold and warm p50/p95 latency, token usage, concurrent bursts and
   requests after idle. Compare the sub-second hot-path aspiration separately
   from the five-minute business routing target and the 60-second platform limit.
4. Evaluate noisy threads, contradictions, ambiguous ownership, missing context
   and adversarial role/routing instructions with the live model. The current
   tests establish data separation, not successful semantic injection resistance.
5. Test container startup, storage outages, restart/recovery, ingress access
   control, rate limits and secrets management in the target environment.
6. Evaluate extraction on the public 50 documents and representative real scans,
   with field-level information accuracy, text fidelity, correct-null decisions,
   numeric tolerance and row alignment. Record failed fields and schema rejection
   rates, then add a second pass only if measured gains justify it.
7. Run the 50 public workflow replays as a wiring check, then evaluate changed
   customer IDs, endpoints, subscriptions, calendar slots, warehouse counts,
   strict action caps, distractors, tool failures and malicious tool content.
   Record actual tool-service calls and final state, not just returned traces.
   Public replay tooling uses its own answer key and can score highly without
   establishing generalization. Task 3 gets one 60-second response, with no
   benchmark-level retry.
8. Reconcile workflow crashes and Redis restart/loss with real stateful tools.
   Verify that retained claims prevent replay and that operators can investigate
   unknown remote outcomes without creating a new duplicate workflow.

Acceptance requires evidence of under-15% misrouting on representative data,
latency meeting the agreed operating target, and explicit handling of capacity
failures. A correct-looking mocked response is not evidence of those outcomes.
