# RAG Agent

An agent that answers a user's question from the `knowledge` index built by the
[knowledge processor](../knowledge-processing/knowledge-processor.md) — and
that **grows that index as a side effect of answering**.

Where the knowledge processor reads threads *popularity-first* and distills
whatever durable facts they contain, the RAG agent works *query-first*: it
searches the existing knowledge pieces, follows them back to their source
threads, re-reads those threads with its attention directed at the user's
question, and writes any newly distilled pieces back into the same index.
Every question asked makes the knowledge base a little more complete in the
neighborhoods people actually ask about.

## Pipeline

Two loops, run in sequence:

```text
user query
    |
    v
1. keyword distillation ──> 2. knowledge search ──> 3. sufficiency check
         ^                                                 |
         └────────── gap remains: revised keywords ────────┤
                        (loop A: until enough              |
                         knowledge coverage)               | enough coverage
                                                           v
4. thread reconstruction ──> 5. query-directed distillation ──> 6. cross-thread check
         ^                        (pieces written back                  |
         |                         to `knowledge`)                      |
         └───────── more source threads worth reading ─────────────────┤
                        (loop B: until enough                          |
                         cross-thread knowledge)                       v
                                                                  7. answer
```

- **Loop A — knowledge fetching.** Distill the query into keywords, search the
  `knowledge` index, and evaluate whether the retained pieces cover the
  question. If not, distill *new* keywords informed by what was found and
  search again.
- **Loop B — thread mining.** The retained pieces carry `thread_id`s. Each
  referenced thread is reconstructed in full from `forum-posts` and re-read by
  the LLM with the user's query in focus, producing new standalone knowledge
  pieces that are indexed back into `knowledge`. If reading one thread surfaces
  pointers to others (or the coverage check still sees gaps), more threads are
  pulled in — until the agent judges it has enough *cross-thread* knowledge to
  answer.

## Stages

### 1. Keyword distillation

An LLM node summarizes the search intent into keywords. Its input is the RAG
state: the **user query**, the **knowledge pieces retained so far** (initially
empty), and the **keywords already tried**. On the first pass it simply
extracts the query's key terms; on later passes it aims keywords at what is
*still missing*, not at what has already been retrieved. Re-emitting an
already-tried keyword set is treated as "nothing left to try" and ends loop A.

### 2. Knowledge search

A search node (no LLM) queries the `knowledge` index. Keywords are searched
**one by one, then in combination**, each wrapped in wildcards —
`*<keyword>*`, then `*<keyword>* *<other_keyword>*` — against the `statement`
and `subject` fields. Individual-keyword queries cast a wide net; combined
queries surface the pieces where the concepts intersect, which are usually the
most on-point. Because the index also carries a `vector_field`, a `knn` clause
on the embedded query is added alongside the wildcard clauses (the same
hybrid `bool.should` shape ingestion search uses), so a piece worded
differently from the query can still be found.

Hits are **accumulated and deduplicated across iterations** (keyed by the
piece's content-hash document id) into the retained-knowledge set.

### 3. Sufficiency check

An LLM node reads the query and the retained pieces and decides:

- **Direct hit** — a retained piece *already answers the question*. Loop A
  ends immediately; no further fetching. Loop B still runs, but only over that
  piece's own thread(s): the full context is reconstructed and distilled
  against the query, both to sharpen the answer (the piece is a compressed
  fact; the thread holds the caveats and confirmations around it) and to bank
  whatever else the thread says about the question.
- **Enough coverage** — the pieces collectively frame the answer. Proceed to
  loop B over all retained pieces' threads.
- **Gap remains** — name the gap and return to stage 1 for revised keywords.

A `--max-fetch-rounds` budget caps loop A; on exhaustion the agent proceeds
with whatever it has rather than spinning.

### 4. Thread reconstruction

For each distinct `thread_id` among the retained pieces, fetch all of its
posts from `forum-posts` and order them by `created_at_timestamp` — the same
reconstruction the knowledge processor performs, and for the same reason: a
knowledge piece is a conclusion, and only the thread shows the question it
answered, the corrections along the way, and whether the fix was confirmed.
Long threads are trimmed to a char budget, keeping the opener and the posts
cited by the retained pieces (`source_posts`) preferentially.

### 5. Query-directed distillation

An LLM node reads each reconstructed thread with a prompt modeled on the
knowledge processor's extraction prompt (`build_distill_prompt`), with one
addition: the **user's query is included as the area of interest**, steering
the model's attention toward passages that bear on it. The output contract is
identical — pieces must **stand on their own**, be **durable**, and **cite
their post ids** — so the pieces are valid `knowledge` documents, not
answer fragments. Zero pieces from a thread is a valid outcome.

Each new piece is embedded and **upserted into `knowledge`** using the same
content-hash id scheme as the processor, so re-derived facts refresh rather
than duplicate. The `model` field records provenance; rag-derived pieces are
otherwise indistinguishable from processor-derived ones, and future runs (and
future loop-A searches *within this run*) recall them like any other.

### 6. Cross-thread check

An LLM node evaluates the accumulated picture: do the distilled pieces, taken
across threads, close the question — or did reading the threads surface leads
(a referenced thread, a named part or procedure, a contradiction between
threads) that warrant reconstructing more? If more is warranted, the new
thread ids (from freshly retrieved pieces, or from a targeted re-run of loop A
with lead-derived keywords) feed back into stage 4. A `--max-thread-rounds`
budget caps loop B.

### 7. Answer

The answerer composes the final response from the retained knowledge pieces
and the reconstructed threads, citing thread URLs and source posts. It is
instructed to say plainly when the sources do not contain the answer, rather
than fall back on prior knowledge — an honest "the forum doesn't cover this"
is a valid answer.

## Graph Layout

Five nodes, two conditional edges — implementable directly in LangGraph:

| Node | Kind | Role |
|---|---|---|
| `planner` | LLM | Stages 1 + 3: emit keywords, or judge sufficiency / direct hit. |
| `search` | tool | Stage 2: hybrid wildcard + knn query over `knowledge`; dedup into state. |
| `reconstruct` | tool | Stage 4: rebuild threads from `forum-posts`. |
| `distill` | LLM | Stage 5: query-directed extraction; embed + upsert pieces. |
| `answer` | LLM | Stage 7: final answer from pieces + threads. |

Conditional edges: `planner → search` (gap) vs `planner → reconstruct`
(sufficient / direct hit / budget spent), and `distill → reconstruct` (more
threads, stage 6) vs `distill → answer` (enough / budget spent). The state
carries the query, tried keywords, retained pieces, reconstructed threads,
and round counters.

## Configuration

Environment variables with CLI flags overriding, matching the repo's
conventions:

| Setting | Default | Meaning |
|---|---|---|
| `ES_URL` / `--opensearch-url` | `http://localhost:9200` | OpenSearch base URL. |
| `KNOWLEDGE_INDEX` / `--knowledge-index` | `knowledge` | Index searched and written back to. |
| `SOURCE_INDEX` / `--source-index` | `forum-posts` | Index threads are reconstructed from. |
| `OLLAMA_URL` / `--ollama-url` | `http://localhost:11434` | Ollama base URL. |
| `LLM_MODEL` / `--llm-model` | `${LLM_MODEL}` | Planner / distiller / answerer model. |
| `EMBED_MODEL` / `--embed-model` | `nomic-embed-text` | Embedding model (768-dim, same as the index). |
| `--max-fetch-rounds` | `3` | Loop A budget (keyword → search rounds). |
| `--max-thread-rounds` | `2` | Loop B budget (reconstruction → distillation rounds). |
| `--thread-char-budget` | `12000` | Trim reconstructed threads to this size. |

## Edge Cases

- **Empty or thin `knowledge` index.** Loop A finds nothing; the agent says so
  and suggests running the knowledge processor (`make knowledge`) — it does
  not fall back to searching raw posts, which is the old agent's job.
- **Wildcard over-match.** A short keyword (`oil`) can match half the index;
  the combined-keyword queries and the dedup cap keep the retained set focused,
  and the planner is told to prefer specific, multi-word keywords.
- **Repeated keywords / stalled loops.** A re-emitted keyword set ends loop A;
  round budgets bound both loops; on exhaustion the agent answers from what it
  has.
- **Distillation yields nothing new.** A thread already fully mined by the
  processor may produce only pieces whose content-hash ids already exist — the
  upserts are no-ops and the run proceeds normally.
- **Sources don't contain the answer.** The answerer states it plainly, with
  the closest threads linked so the user can judge for themselves.

## Serving It (`--serve`)

Everything above describes one call to `ask_rag` — the graph itself doesn't
know whether it was invoked from argv or from an HTTP request. `--serve`
(implemented in `server.py`) puts a minimal OpenAI-compatible API in front of
it (`POST /v1/chat/completions`, `GET /v1/models`, `GET /healthz`; stdlib
`http.server`, no new dependency) so any OpenAI-client-compatible tool can
treat the agent as a chat backend instead of one CLI question at a time. The
last `user` message in a request is the question passed to `ask_rag`;
requests are serialized behind a lock, since they all contend for the same
local Ollama instance the research loop is already using. See
[README.md](README.md#serving-an-openai-compatible-api) for usage and the
`rag-serve` service in the repo's `docker-compose.yml` for the always-on
deployment.
