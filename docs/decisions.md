# Decisions

Two things this pipeline deliberately does *not* do. Both were evaluated rather
than assumed, and both are recorded here because the reasoning is the useful part
— anyone can wire up the alternative.

---

## 1. Extraction is not agentic

**Decision:** the extractor is a three-layer cascade ending in a single-turn,
schema-constrained model call. It is not an agent loop.

### What was tried

A locked-down agent with two in-process tools — `fetch_page` and
`lookup_enrichment` — restricted to the local mock services by a host allowlist,
with no filesystem or shell access. It was given a company listing URL and asked
to produce the same record the cascade produces, with permission to go looking
for fields the page did not state.

### What happened

One company, one run:

```
→ fetch_page(/company/northwind-analytics)
→ lookup_enrichment(northwindanalytics.com)
→ fetch_page(/)                            wandered
→ fetch_page(/software)                    404
→ fetch_page(/category/software)           404
→ fetch_page(/company/harbor-point-labs)   a different company

6 tool calls · $2.56 · employee_count still null · region "Texas" not "TX"
```

Four problems, in order of severity:

1. **$2.56 for one company.** The cascade costs a fraction of a cent per page.
   At any real directory size this is ruinous.
2. **It read an unrelated company's page.** In a pipeline that then performs
   entity resolution, an extractor that bleeds data across records is a
   correctness hazard, not merely waste.
3. **Three of six calls were 404s**, guessing URL patterns that do not exist.
4. **Schema drift** — `"Texas"` where the mapping wants the keyword `TX`.

### Why the cascade wins

Extraction is high-volume and well-specified. Every page wants the same ten
fields, and the page either states them or does not. That is precisely the shape
a deterministic parser handles for free and an agent loop handles expensively,
because the loop's value is deciding *what to do next* — and here there is
nothing to decide.

`scripts/eval_extraction.py` puts a number on it: the deterministic layers
recover **232 of 234 recoverable fields with zero incorrect values**. The model
is needed for two, and both are genuinely ambiguous from prose alone.

### Limits of this evidence

This was a **single spike against the mock site, not a controlled benchmark.**
The agent was written for the experiment, not tuned; a better prompt, a tool that
refused off-topic URLs, or a cheaper model would all improve it. A fair
head-to-head would need repeated runs against real markup and a labelled set, and
would cost money to produce.

It does not need to be tighter to support the decision. The gap is roughly two
orders of magnitude on cost, and the failure mode — reading the wrong company —
is a correctness problem rather than a tuning one.

### When this would change

If extraction needed to *navigate* — following pagination whose shape is unknown,
logging in, deciding which of several candidate pages describes the company —
then choosing the next action is the actual work, and a loop earns its cost.

---

## 2. Pydantic AI is not used

**Decision:** model calls go directly through the Anthropic SDK
(`AsyncAnthropic`, `messages.create`) in `extraction/agent.py` and
`resolution/adjudicator.py`.

Pydantic and `pydantic-settings` are used heavily — for domain models, API
schemas and settings, and via `temporalio.contrib.pydantic` so models cross the
workflow boundary as models. The agent framework is a separate package and is
absent deliberately.

### Why

**There is no agent loop to frame.** Both call sites are single-turn structured
calls. Pydantic AI's value is the loop: tool orchestration, multi-step reasoning,
dependency injection. None of that applies, and decision 1 above is the measured
argument against introducing it.

**Validated structured output is already there.** The extractor passes
`output_config.format` with a JSON schema; the adjudicator uses strict tool
calling with `tool_choice`. Responses are schema-valid by construction. That is
the framework's headline feature, already held.

**Three provider-specific behaviours are load-bearing**, and an abstraction layer
complicates each:

| behaviour | why it matters |
|---|---|
| `cache_control` placement in the `system` block | the cached prefix must stay byte-identical across pages; a test pins it, and when it breaks there is no error, just a larger bill |
| `cache_read_input_tokens` on the response | `/metrics/summary` bills cached tokens at the cache rate — a ~5× difference in reported cost |
| `output_config.effort` | a deliberate cost/latency knob on the extraction call |

### What is given up

Model-agnosticism. Swapping providers means editing two call sites rather than
one configuration value. That is a real cost and an acceptable one for a pipeline
that has no requirement to switch.

Also given up: Logfire integration, and typed tool definitions generated from
signatures rather than written out. The adjudicator's tool schema is hand-written
and would be shorter under the framework.

### When this would change

Model-agnosticism becoming a requirement, or a component appearing that genuinely
needs a multi-tool agent loop. At that point the loop is the reason to adopt it,
not the structured output.
