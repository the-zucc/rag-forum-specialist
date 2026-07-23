# Knowledge Processor

A batch job that reads whole forum threads — most popular first — and distills
each one into a handful of **durable knowledge pieces**, which it writes into a
new OpenSearch index called `knowledge`.

Where ingestion stores *posts* (one document per message, good for retrieval)
and the RAG agent answers *one question at a time*, the knowledge processor
works at the level of a *thread as a whole*. It reads a thread the way an
archivist would — start to finish — and writes down the small number of facts
worth keeping, detached from the back-and-forth they were buried in. The output
is a compact, high-signal knowledge base built from the forum's most-discussed
subjects.

```text
                  OpenSearch: forum-posts  (one doc per post)
                              |
                              v
   1. rank threads by popularity   ── terms aggregation on thread_id
                              |
                              v
   2. take the window [offset+1, offset+top_n], drop already-processed threads
                              |                        |
                              |                         `── OpenSearch: knowledge-processor-status
                              v
   3. reconstruct each remaining thread   ── all posts, chronological
                              |
                              v
   4. distill knowledge pieces   ── Ollama reads the whole thread
                              |
                              v
   5. embed + index each piece, mark the thread processed
                              |                        |
                              v                         `── OpenSearch: knowledge-processor-status
                  OpenSearch: knowledge  (new index)
```

It is intentionally **standalone**: a job you run (or schedule) after
ingestion has posts to work with. It does not touch the RAG agent, its
`forum-learnings` index, or the live ingestion watcher.

Processing is **idempotent at the thread level**: every thread's id is
checked against a `knowledge-processor-status` index before it is
reconstructed, and skipped if already marked processed there (see
[The `knowledge-processor-status` Index](#the-knowledge-processor-status-index)).
That's what makes it safe to restart the job, run `--loop` forever, or
re-sweep the same ranking window without redoing (and re-billing, on a
metered LLM) work already done.

By default it does one pass and exits, but with `--loop` it runs forever,
sweeping the popularity ranking window by window (see
[Offset Windows and Endless Mode](#offset-windows-and-endless-mode)) so it
keeps mining threads beyond the top 10 and keeps up as ingestion adds posts.

## Inputs and Outputs

- **Input:** the `forum-posts` index in OpenSearch (already populated by
  [`ingestion/`](../ingestion/README.md)). Every post carries a `thread_id`,
  `thread_title`, `thread_url`, `created_at_timestamp`, `body_text`, and
  `author` — everything the processor needs to rank, reconstruct, and cite.
- **Output:** a new `knowledge` index. One document per knowledge piece, each
  embedded for semantic search and stamped with the thread and posts it came
  from.
- **Input/output:** a `knowledge-processor-status` index — one document per
  thread, read to decide whether to skip it and written once its processing
  reaches a terminal outcome. See
  [The `knowledge-processor-status` Index](#the-knowledge-processor-status-index).

Reading from OpenSearch (rather than the raw `threads/<id>/posts.json` files)
keeps the processor aligned with the canonical store the rest of the system
searches, and lets step 1 rank threads with a single aggregation instead of
scanning every file.

## What "Popular" Means

The scraped data does **not** include view counts, so popularity is measured by
**engagement**: the **number of posts in the thread** (its reply count). A
thread people kept coming back to is, by construction, one the community found
worth discussing.

- **Primary signal — post count.** A single `terms` aggregation on `thread_id`
  over `forum-posts`, ordered by document count descending, yields the ranking
  directly. In the current corpus the top band runs from ~79 posts down to ~39
  for the tenth thread, so the "top 10" is clear and well-separated.
- **Likes are a poor secondary.** Most threads have zero aggregate likes, so
  they can't order the list; they're recorded on each knowledge piece as
  context but do not drive selection.

`--top-n` (default `10`) controls the size of the window processed per pass;
`--offset` shifts which slice of the ranking that window covers (see below).

## Processing Stages

### 1. Rank threads by popularity

Aggregate `forum-posts` by `thread_id`, ordered by post count, and take the
window of threads ranked `offset+1` through `offset+top_n` (`offset` defaults
to `0`, so by default this is the top `N`). The aggregation returns each
thread's post count for free; the title/URL are read from any one post in the
bucket. Threads below a `--min-posts` floor (default `5`) are skipped — a
three-post thread rarely contains a durable, generalizable fact, and
processing it wastes an LLM call.

### 2. Drop already-processed threads

Before any thread in the window is reconstructed, all of its thread ids are
looked up in `knowledge-processor-status` with a single `_mget`. A thread
with a document there whose `processed` field is `true` is skipped — logged
and left out of steps 3 onward entirely, no LLM or embedding calls spent on
it. This is the whole idempotency mechanism: it's what lets `--loop` sweep
back over the same ranking window on every cycle for free once the corpus
stops growing, and what lets a one-off run be re-invoked safely after a
crash or a config change without redoing already-mined threads.

### 3. Reconstruct the thread

For each selected `thread_id`, fetch all of its posts and order them by
`created_at_timestamp` ascending. This is the "reconstructed thread": the
original question, the replies, the corrections, and — crucially — the poster's
own report of what actually worked. Reading it in order is what lets the
processor tell a confirmed fix from a discarded guess.

Very long threads are capped at a character budget (`--thread-char-budget`) so a
single 79-post thread can't overflow the model's context window; when trimming
is needed, the opener and the most-liked posts are kept preferentially.

### 4. Distill knowledge pieces

The whole reconstructed thread is handed to the local LLM (Ollama, model set by
`--llm-model`) with an extraction prompt. The model returns a short list of
**knowledge pieces** — not a summary of the conversation, but standalone facts
about the *subject* the thread is about. Each piece must:

- **Stand on its own.** Readable without the thread — "The XR-200's water-pump
  seal can only be replaced from the clutch-cover side, and needs a sizing tool
  or it breaks on installation," not "As Jess said above, you need the tool."
- **Be durable.** A specification, a mechanism, a known failure mode, or a fix
  that a poster confirmed worked — the kind of thing still true next year, not
  "has anyone got one for sale."
- **Cite its posts.** Each piece names the post id(s) it was drawn from, so the
  index entry can link back to primary sources.

The prompt asks for a fixed, easily-parsed shape (one piece per line, with its
supporting post ids) and instructs the model to emit *nothing* for a thread that
is pure chatter with no durable takeaway — an empty result is a valid, expected
outcome, not a failure.

A typical popular thread yields on the order of 1–5 pieces.

### 5. Embed and index into `knowledge`

Each piece is embedded with the same model ingestion uses (`nomic-embed-text`,
768 dimensions) and written to the `knowledge` index. The document id is a hash
of the normalized piece text, so re-running the processor **updates** an
existing piece rather than duplicating it (see [Re-runs](#re-runs)).

Once every piece for a thread is written (or the model returned `NONE`, or
the thread had no readable posts), the thread is marked processed in
`knowledge-processor-status` — see
[The `knowledge-processor-status` Index](#the-knowledge-processor-status-index)
for exactly when a thread does and doesn't get marked.

## The `knowledge` Index

Created idempotently from an index template on startup (`PUT
_index_template/knowledge`) — the same convention
[`opensearch-bootstrap/`](../opensearch-bootstrap/README.md) uses — so mapping
changes roll out on the next run and searches never hit a missing index.

One document per knowledge piece:

| Field | Type | Purpose |
|---|---|---|
| `statement` | `text` | The self-contained knowledge piece. |
| `vector_field` | `knn_vector` (768, hnsw/l2/faiss) | Embedded `statement`, for semantic search. |
| `subject` | `text` + `keyword` | What the piece is about — seeded from the thread title, optionally refined by the model. |
| `thread_id` | `keyword` | Source thread. |
| `thread_title` | `text` + `keyword` | Source thread title. |
| `thread_url` | `keyword` | Link to the thread. |
| `source_posts` | `nested` (`post_id`, `post_url`, `author`) | The specific posts the piece was distilled from. |
| `thread_post_count` | `integer` | The thread's popularity signal at processing time. |
| `thread_like_count` | `integer` | Aggregate likes on the thread (context only). |
| `popularity_rank` | `integer` | 1..N, this thread's rank in the run. |
| `model` | `keyword` | LLM that produced the piece (provenance). |
| `created_at` | `date` | When the piece was written. |

The `knn_vector` mapping mirrors `forum-posts` so the same embedding model and
distance space apply, and the `knowledge` index is immediately usable by any
hybrid (`knn` + `multi_match`) search.

## The `knowledge-processor-status` Index

Created idempotently from an index template on startup (`PUT
_index_template/knowledge-processor-status`), the same convention the
`knowledge` index uses.

One document per thread, `_id` set to the `thread_id`:

| Field | Type | Purpose |
|---|---|---|
| `processed` | `boolean` | `true` once the thread has reached a terminal outcome. Presence + `true` is what step 2 checks. |
| `processed_at` | `date` | When it was marked. |
| `thread_title` / `thread_url` | `keyword` | For inspecting the index without joining back to `forum-posts`. |
| `post_count` / `like_count` | `integer` | The thread's popularity signal at the time it was marked. |
| `popularity_rank` | `integer` | Its rank in the run that marked it. |
| `pieces_written` | `integer` | How many knowledge pieces this thread produced (`0` for "no durable knowledge" or "no readable posts"). Absent for threads marked via `--mark-all-processed`, since it's unknown how many pieces they'd have produced. |

A thread is marked **only** on a terminal outcome — pieces written, "no
durable knowledge" (`NONE` from the model), or no readable posts to
distill — never on a transient failure (Ollama unreachable, an indexing
error). That distinction is what makes a crash mid-run safe to just restart:
whatever was marked stays skipped, whatever wasn't gets retried.

### Bootstrapping with `--mark-all-processed`

`--mark-all-processed` ranks the *entire* corpus (no `--offset`/`--top-n`
windowing, just `--min-posts`) and writes a `processed: true` document for
every thread, without reconstructing, distilling, or embedding anything. It
exits once done — it never combines with `--loop` or a normal pass in the
same invocation.

This exists for the day this tracking was introduced: a `knowledge` index
already populated by prior runs, with no record of which threads produced
it. Running `--mark-all-processed` once seeds `knowledge-processor-status`
to match that existing state, so the next normal run doesn't re-distill
(and re-bill, on a metered LLM) everything from scratch. Any thread scraped
*after* that point — or any thread you deliberately want reprocessed (see
below) — is unaffected and gets picked up normally.

```bash
make knowledge MARK_ALL_PROCESSED=1
```

### Forcing a thread to be reprocessed

There's no `--replace` for this index (deleting it would be self-defeating).
To force one thread back through distillation, delete its status document
directly:

```bash
curl -X DELETE "$ES_URL/knowledge-processor-status/_doc/<thread_id>"
```

It will be picked up the next time its rank falls inside a processed window.

## Re-runs

The job is safe to run repeatedly:

- **Ranking** re-reads `forum-posts`, so as ingestion grows the corpus the
  top-10 naturally shifts and newly-popular threads get processed.
- **Thread-level skip** (see above) means a thread already marked processed
  costs one `_mget` lookup and nothing else — no LLM or embedding calls —
  no matter how many times its ranking window comes back around, which is
  what makes `--loop`'s endless wrap-around sweep cheap in steady state.
- **Indexing** upserts by a hash of the normalized piece text. A fact
  re-derived on a later run (e.g. after a status document was deleted to
  force a reprocess) overwrites its own document (refreshing the provenance
  and popularity fields) instead of creating a near-duplicate.
- Because ids are content-derived, a *reworded* fact creates a new document; a
  `--replace` mode (delete the `knowledge` index first) is offered for a clean
  rebuild when prompt or model changes make old pieces stale. `--replace`
  only touches the `knowledge` index, though — every thread is still marked
  processed, so nothing gets re-distilled unless `knowledge-processor-status`
  is also cleared (delete the index) before the next run.

## Offset Windows and Endless Mode

Left alone, a job that always processes "the top `N`" never gets past the
same `N` threads — thread #11 never gets mined no matter how many times it
runs. `--offset` fixes that by letting a pass target any slice of the
ranking, not just the top:

- **`--offset` + `--top-n`** together select the window ranked `offset+1`
  through `offset+top_n`. `rank_threads` asks OpenSearch for the top
  `offset+top_n` threads (a `terms` aggregation has no native offset) and
  slices off the first `offset` in Python, so `--offset 10 --top-n 10`
  processes ranks 11–20, `--offset 20 --top-n 10` ranks 21–30, and so on.
- A single pass still processes one window and exits — `--offset` alone is
  useful for a one-off backfill of a specific slice.

**`--loop`** turns this into a job that never exits, sweeping the whole
ranking window by window:

1. Wait for `forum-posts` to stop growing (`wait_for_posts`, the same
   stabilization check used on startup) — this is what makes it safe to run
   continuously alongside the live ingestion watcher, rather than racing a
   thread whose posts are still arriving.
2. Rank and process the window at the current offset (starting at
   `--offset`, default `0`).
3. Advance the offset by `--top-n` for the next cycle — `0, 10, 20, …` for
   the default window size of 10.
4. When a window comes back shorter than `--top-n` (the tail of the ranked
   list), the *next* cycle wraps back to offset `0` instead of stepping past
   the end, so the sweep restarts from the top once it has covered every
   eligible thread — picking up newly-popular threads and freshly-scraped
   posts as it goes.
5. Sleep `--poll-interval` seconds (default `1800` = 30 minutes; a couple of
   minutes to a couple of hours is the intended range) before the next cycle.

Because every thread is skipped once marked processed (see
[The `knowledge-processor-status` Index](#the-knowledge-processor-status-index)),
sweeping back over already-mined threads is not just harmless but cheap — a
window of already-processed threads costs one `_mget` and a handful of log
lines, no LLM or embedding calls, until the sweep reaches a thread that
hasn't been marked yet (newly scraped, or newly promoted into the ranking by
fresh posts).

`--offset` and `--loop` are independent: `--offset` alone changes which
window a single pass covers; `--loop` alone starts the endless sweep at
`--offset 0`.

## Configuration

Environment variables, with CLI flags overriding, matching the rest of the
repo's conventions:

| Setting | Default | Meaning |
|---|---|---|
| `ES_URL` / `--opensearch-url` | `http://localhost:9200` | OpenSearch base URL. |
| `SOURCE_INDEX` / `--source-index` | `forum-posts` | Index to read posts from. |
| `KNOWLEDGE_INDEX` / `--knowledge-index` | `knowledge` | Index to write pieces to. |
| `STATUS_INDEX` / `--status-index` | `knowledge-processor-status` | Index tracking which threads have already been processed. |
| `OLLAMA_URL` / `--ollama-url` | `http://localhost:11434` | Ollama base URL. |
| `LLM_MODEL` / `--llm-model` | `${LLM_MODEL}` | Distillation model. |
| `EMBED_MODEL` / `--embed-model` | `nomic-embed-text` | Embedding model (768-dim). |
| `--top-n` | `10` | Window size: how many ranked threads to process per pass. |
| `--offset` | `0` | Skip this many top-ranked threads before the window (ranks `offset+1`..`offset+top-n`). |
| `--min-posts` | `5` | Skip threads smaller than this. |
| `--thread-char-budget` | `12000` | Trim threads longer than this before distillation. |
| `--replace` | off | Delete and recreate `knowledge` before writing. |
| `--mark-all-processed` | off | Mark every currently-ranked thread processed and exit, without distilling anything (see [Bootstrapping](#bootstrapping-with---mark-all-processed)). |
| `--loop` | off | Run forever, sweeping the ranking window by window (see [Offset Windows and Endless Mode](#offset-windows-and-endless-mode)). |
| `--poll-interval` | `1800` | Seconds to sleep between cycles when `--loop` is set. |

## Running It

- **`Makefile` target** — `make knowledge`, mirroring `make ingest`/`make ask`:
  a one-off local run against a live OpenSearch + Ollama. `make knowledge
  LOOP=1` sweeps forever instead; `make knowledge MARK_ALL_PROCESSED=1` runs
  the bootstrap mode.
- **Compose service** — `knowledge-processor` runs `--loop` with `restart:
  unless-stopped`, continuously sweeping the ranking window by window
  alongside the live ingestion watcher; it never exits on its own.

```bash
# after `make up` has ingested some posts, and Ollama has the models pulled:
make knowledge
```

## Edge Cases

- **Empty distillation.** A popular but low-substance thread (off-topic, an
  argument, a for-sale post) legitimately produces zero pieces. The processor
  logs it and moves on.
- **Fewer than N eligible threads.** With a small corpus the run simply
  processes whatever clears `--min-posts`.
- **OpenSearch/Ollama unavailable.** The job waits for the cluster to be
  reachable (same as ingestion) and fails loudly if the LLM or embedder can't be
  reached, rather than writing partial pieces.
- **A thread's posts changed since it was marked processed.** Unlike
  `knowledge`'s content-hash upsert, the status index does **not**
  auto-detect this — a processed thread is skipped regardless of new replies
  added to it since. Delete its status document (see
  [Forcing a thread to be reprocessed](#forcing-a-thread-to-be-reprocessed))
  to have it picked up again.
- **A transient failure mid-thread.** If Ollama errors out during
  distillation, the thread is left unmarked and simply gets retried the next
  time its rank falls inside a processed window — see
  [The `knowledge-processor-status` Index](#the-knowledge-processor-status-index).
  A failure indexing one *piece* (`index_piece` raising) is logged and the
  thread is still marked processed once the rest of its pieces are written.
- **`--offset` past the end of the ranked list.** A single pass (`--loop` not
  set) with an out-of-range `--offset` logs a warning and does nothing — it
  does not clamp or wrap on its own. In `--loop` mode, an empty window at a
  nonzero offset is expected (the sweep just reached the tail) and wraps back
  to offset `0` on the next cycle.
- **Corpus smaller than one window.** If the corpus never has more than
  `--top-n` eligible threads, every `--loop` cycle re-ranks the same window
  at offset `0` but skips every thread already marked processed — harmless
  and cheap (see [Re-runs](#re-runs)), just not productive until more
  threads clear `--min-posts` or existing ones get new replies and are
  manually unmarked.
- **`--mark-all-processed` leaves gaps in `knowledge`.** Threads bootstrapped
  this way are marked processed without ever being distilled, so they may
  have no corresponding documents in `knowledge` (if they predate this
  tracking and were never actually processed) or stale ones (if they were
  processed under an older prompt/model). Both are expected for a bootstrap
  seed; reprocess specific threads as needed (see
  [Forcing a thread to be reprocessed](#forcing-a-thread-to-be-reprocessed)).
