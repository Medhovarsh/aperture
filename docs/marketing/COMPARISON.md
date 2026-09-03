# Where Aperture sits

Written to be fair rather than flattering. If a reader can tell which tool the
author prefers from the table alone, the table is doing marketing instead of
helping someone choose, and a security engineer will notice.

## The stack, top to bottom

```
                 ┌──────────────────────────────────────────┐
  Prompt in      │  Input guardrails                        │  Guardrails AI, NeMo
                 │  jailbreak / PII / topic filters         │  LLM Guard, Lakera
                 └──────────────────────────────────────────┘
                 ┌──────────────────────────────────────────┐
  Retrieval      │  ► APERTURE ◄                            │  ← thin here
  and action     │  who may see this, for what purpose,     │
                 │  what may it do, and what did it do      │
                 └──────────────────────────────────────────┘
                 ┌──────────────────────────────────────────┐
  Response out   │  Output guardrails                       │  Guardrails AI, NeMo
                 │  hallucination / PII / format checks      │  LLM Guard, Vectara
                 └──────────────────────────────────────────┘
                 ┌──────────────────────────────────────────┐
  Afterwards     │  Observability and evaluation            │  LangSmith, Langfuse
                 │  traces, evals, scores                   │  Arize, Braintrust
                 └──────────────────────────────────────────┘
```

Aperture composes with every layer above and below. It is not an alternative to
an input filter, and pretending otherwise would be an easy claim to disprove.

## Feature comparison

| | Aperture | Prompt/response guardrails | RAG frameworks | LLM observability | Vector DB filters |
|---|---|---|---|---|---|
| Filters prompt or response content | — | ✅ | — | — | — |
| Per-identity access to sources | ✅ | — | ~ (DIY) | — | ~ (metadata) |
| Purpose-bound access | ✅ | — | — | — | — |
| Tells the agent what was withheld | ✅ | — | — | — | — |
| Field-level redaction | ✅ | ~ (output PII) | — | — | — |
| Freshness SLA per source | ✅ | — | — | — | — |
| Governs actions, not just text | ✅ | — | — | — | — |
| Measures blast radius before acting | ✅ | — | — | — | — |
| Human approval gate | ✅ | — | — | — | — |
| Rollback of executed actions | ✅ | — | — | — | — |
| Spend and rate budgets | ✅ | — | — | — | — |
| Tamper-evident audit chain | ✅ | — | — | ~ (traces) | — |
| Externally anchored audit | ✅ | — | — | — | — |
| Runs with no external service | ✅ | ~ | ✅ | — | ~ |

`~` means partially, or only if you build it yourself.

## Honest read on each neighbour

**Guardrails AI, NVIDIA NeMo Guardrails, LLM Guard.** Mature, well-adopted, and
solving a different problem well. They validate text going in and coming out.
Neither knows who the user is or whether that user may see the chunk that was
retrieved. Use them *and* Aperture.

**Lakera, Prompt Security, and similar.** Focused on injection detection and
runtime prompt threats, commercial and well-resourced. Complementary: Aperture's
answer to injection is structural rather than detective — a poisoned document is
retrieved as ordinary text and changes no authorization decision, because
authorization is computed from the principal and the policy.

**LangChain, LlamaIndex.** Frameworks for building the pipeline. They give you the
hooks to implement governance; they do not implement it. Aperture is what you would
have to write inside those hooks, plus the parts most people skip.

**LangSmith, Langfuse, Arize, Braintrust.** Excellent at showing you what happened.
They are observability, not enforcement — they will faithfully record an agent
reading a document it should never have seen.

**Vector database metadata filters (Pinecone, Weaviate, Qdrant, pgvector).** The
usual DIY answer, and the reason the silent-truncation problem is so widespread. A
filter drops results without telling anyone, so the model answers from a partial
context as if it were complete. Aperture uses these as backends, not competitors.

**Glean, Microsoft Copilot, Vertex AI Search.** Enterprise search with real
permission awareness — genuinely solve the read half, inside their own product.
Aperture is the open, self-hosted, bring-your-own-stack version, and it also
governs actions, which suites generally do not.

**Cedar, OPA/Rego.** Serious policy engines, more expressive than Aperture's rules.
They evaluate a decision you hand them; they do not know what a blast radius is,
route a question to a source, or hold an approval. A large deployment could
plausibly keep Aperture's pipeline and swap the evaluator for one of these — the
`Policy` interface is small on purpose.

## When not to use Aperture

- Single-tenant, single-user, non-sensitive data. Governance overhead with no
  governance benefit.
- You need dataset-level bias analysis or model evaluation. Wrong tool entirely.
- Your agents only read one public corpus. A filter is enough; be honest about it.
- You need a vendor with an enterprise support contract today. This is a
  single-maintainer open source project, and a security review will find that out.
