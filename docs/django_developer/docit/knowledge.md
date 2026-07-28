# DocIt Knowledge Base (mojo.apps.docit_kb)

Optional companion app that turns DocIt into a searchable knowledge base:
pages are sliced into heading-scoped chunks, embedded with AWS Bedrock
(Titan Text Embeddings V2 by default), and searched with a hybrid of
pgvector cosine similarity and Postgres full-text rank. The same retrieval
service powers `/api/docit/search` and the assistant's `search_docs` tool.

DocIt itself works without this app — search then falls back to page-level
Postgres full-text search with no extra schema or dependencies.

## Enabling

1. Install the extra: `pip install django-mojo[kb]` (adds the `pgvector`
   Python package).
2. Ensure the `vector` extension is available in your PostgreSQL
   (preinstalled on RDS/Aurora/Supabase; locally `brew install pgvector`).
   Migration `docit_kb.0001` runs `CREATE EXTENSION IF NOT EXISTS vector` —
   if your app role may not create extensions, create it manually first.
3. Add the app to `INSTALLED_APPS`, after `mojo.apps.docit`:

```python
INSTALLED_APPS = [
    ...
    "mojo.apps.docit",
    "mojo.apps.docit_kb",
    ...
]
```

4. Run `migrate`, then backfill existing content per book:
   `POST /api/docit/book/<id>` with `{"reindex": true}` (or call
   `knowledge.reindex_book(book)` from a shell).

## Settings

| Setting | Default | Notes |
|---|---|---|
| `EMBEDDINGS_PROVIDER` | `"bedrock"` | `"bedrock"` or `"mock"` (deterministic, for tests/offline dev) |
| `EMBEDDINGS_DIM` | `1024` | Must match the `PageChunk` vector column. Changing it requires a migration plus a full re-embed; on a mismatch the pipeline skips vectors and logs an error rather than writing corrupt data. |
| `BEDROCK_EMBED_MODEL` | `"amazon.titan-embed-text-v2:0"` | |
| `BEDROCK_REGION` | `AWS_REGION` | |
| `DOCIT_KB_RECONCILE_ENABLED` | `True` | Kill switch for the reconciliation cron dispatcher. False = no sweep is queued (the job itself still runs if published by hand). |
| `DOCIT_KB_RECONCILE_LIMIT` | `200` | Maximum pages one sweep may queue, across all three arms. |
| `DOCIT_KB_RECONCILE_LOOKBACK_HOURS` | `168` | How far back the stale arm looks. Bounds the cost of the sweep; the never-chunked arm ignores it. |

Credentials come from the shared `AWS_KEY` / `AWS_SECRET` settings. Without
credentials the pipeline still chunks pages (embeddings stay null) and
search degrades to full-text only — nothing errors.

## Architecture

- **`PageChunk`** (`mojo/apps/docit_kb/models/page_chunk.py`) — one row per
  heading section of a page: position, heading breadcrumb
  (`"Install > Quickstart"`), raw markdown slice, sha256 content hash,
  `VectorField(1024)` (HNSW-indexed), and the model id that embedded it.
- **Chunker** (`services/chunker.py`) — splits on `#`–`###` headings
  (fenced code blocks ignored), hard-splits sections over ~4000 chars at
  paragraph boundaries. Deterministic.
- **Pipeline** (`services/knowledge.py`) — `embed_page_now(page)` diffs
  chunks against existing rows **by content hash**: unchanged chunks keep
  their rows and embeddings, only new content is embedded, stale rows are
  deleted. Idempotent.
- **Async wiring** — `Page.save()` publishes a
  `mojo.apps.docit_kb.asyncjobs.embed_page` job (jobs app, default
  channel); a failed publish never breaks the save. `Book` gains a
  `reindex` POST action that queues one job per page.
- **Reconciliation** (`cronjobs.py` → `asyncjobs.reconcile_embeddings` →
  `knowledge.reconcile_stale_pages`) — because the embed publish is
  fire-and-forget, a queue outage silently drops re-embeds. A `*/15` cron
  dispatcher queues one sweep job; the sweep queues a normal embed job per
  affected page. Three arms, capped together at
  `DOCIT_KB_RECONCILE_LIMIT` and ordered newest-edit-first:
  1. **stale** — the page has chunks whose watermark is behind
     `page.modified`, within `DOCIT_KB_RECONCILE_LOOKBACK_HOURS`.
  2. **never chunked** — no chunk rows at all. No lookback (so adopting the
     app backfills itself), and blank pages are skipped here — they produce
     zero chunks by design and would otherwise be queued forever.
  3. **null embeddings** — chunks exist without vectors, i.e. an embed that
     ran while the provider was down. Skipped entirely when no provider is
     available, since null vectors are the steady state of an FTS-only
     install.

  Pages saved in the last 10 minutes are left to their own embed job, pages
  with a pending/running embed job are skipped, and each page is queued at
  most once an hour (job idempotency key `docit_kb.recon:<page>:<bucket>`).
  **Operator prerequisite:** the deployment's cron runner must call
  `mojo.helpers.cron.load_app_cron()` and then `run_now()` on a schedule —
  nothing sweeps if cron is not wired up.
- **Search** — `knowledge.search(query, book=None, limit=10, groups=None)`
  runs two legs over published pages of active books: pgvector
  `CosineDistance` (when a provider is available) and Postgres FTS
  (`SearchVector('heading','content')`, websearch syntax), fused with
  reciprocal-rank fusion (k=60). `groups` confines results to those tenants
  (`None` is unrestricted — internal/system callers only); see
  [docit/README.md](README.md#knowledge-base-search) for
  `visible_groups()`, which request-facing callers must pass. Returns
  `objict(mode, results)` — mode `"hybrid"` or `"fts"`.
- **Fallback dispatch** — `mojo/apps/docit/services/search.py::search_any`
  routes to the KB when the app is installed, else to page-level FTS
  (`search_pages`, mode `"pages"`). The REST endpoint and assistant tool
  both go through it.

## Embeddings helper

`mojo/helpers/embeddings.py` is provider-agnostic and reusable outside
docit:

```python
from mojo.helpers import embeddings

vectors = embeddings.embed_texts(["first doc", "second doc"])
vector = embeddings.embed_query("search terms")
embeddings.is_available()   # a usable provider is configured
```

Providers: `bedrock` (Titan V2 — one invoke per text; batches loop) and
`mock` (hash-derived deterministic unit vectors — no network, used by the
test suite via `EMBEDDINGS_PROVIDER = "mock"`).

## Notes

- Vector search returns nearest neighbors without a relevance threshold —
  a query matching nothing textually still returns weak neighbors. RRF
  fusion keeps textual matches on top when they exist.
- Unpublishing a page or deactivating a book hides its chunks from search
  immediately (query-time filters); the rows remain and reappear on
  republish. Deleting a page cascades to its chunks.
- The embed publish on `Page.save` is fire-and-forget: if the job queue is
  down, the save still succeeds and the miss is only logged. A page edited
  during a queue outage keeps serving its **pre-edit** chunks in search —
  including content the edit removed — until the reconciliation sweep
  notices. With the cron wired up the exposure window is about one cron
  period plus job latency (~15–20 minutes); without it, forever, and a
  book `{"reindex": true}` is the manual clear.
- Repeat `reindex` calls dedupe per unchanged page via job idempotency
  keys (page id + modified timestamp) — a reindex loop cannot flood the
  queue. The flip side is that a permanently-failed embed of an unchanged
  page won't re-run on its own; the sweep's null-embedding arm re-queues it
  within the hour whenever an embeddings provider is configured.
- `PageChunk.modified` is a **pipeline verification watermark**, not a
  last-content-change timestamp: `embed_page_now` overwrites it with the
  page's `modified` value as of the run that built the chunks, so it reads
  as "certified against page version X". That is what makes
  `page.modified > MAX(chunk.modified)` an exact staleness test, and it is
  why a save landing mid-embed leaves the page flagged instead of being
  declared current. Anything reading these rows for freshness (dashboards,
  exports) must read it that way.
- The sweep re-runs the **existing** embed pipeline, which diffs by content
  hash — it cannot notice that the embedding *model* changed, since the
  content is identical. A book `{"reindex": true}` (after clearing the
  chunks, if the vectors must be rebuilt) remains the tool for embedding
  model or dimension changes.
- `snippet` is untrusted text in every mode — page authors control it;
  never render it as HTML. `pages` mode wraps matches in `**` (markdown),
  not HTML tags.
- Search enforces the same visibility docit's own RestMeta enforces:
  published page + active book, confined to the caller's own tenants via the
  `groups` argument (a global `view_docit`/`manage_docit`/`docs` holder is
  unrestricted; an `ApiKey` is never unrestricted; an anonymous caller gets
  nothing — see [docit/README.md](README.md#knowledge-base-search)).
  `Book.permissions` remains an unused free-text field — no code reads it.
  If a consumer later wires it into a view hook, extend the base queryset in
  `knowledge.search` (single choke point) to match.
- The test suite pins the pipeline, hybrid ranking, visibility filters,
  REST contract, and every reconciliation arm (including the watermark
  semantics and the blank-page guard) in `tests/test_docit/knowledge.py`;
  the test Postgres gets the extension via `bin/create_testproject`.
