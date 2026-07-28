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
- **Search** — `knowledge.search(query, book=None, limit=10)` runs two
  legs over published pages of active books: pgvector `CosineDistance`
  (when a provider is available) and Postgres FTS
  (`SearchVector('heading','content')`, websearch syntax), fused with
  reciprocal-rank fusion (k=60). Returns `objict(mode, results)` — mode
  `"hybrid"` or `"fts"`.
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
  down, the save still succeeds and the miss is only logged. Until the
  reconciliation cron ships (board item 544), a page edited during a queue
  outage keeps serving its **pre-edit** chunks in search — including
  content the edit removed. A book `{"reindex": true}` clears it manually.
- Repeat `reindex` calls dedupe per unchanged page via job idempotency
  keys (page id + modified timestamp) — a reindex loop cannot flood the
  queue. The flip side: a permanently-failed embed of an unchanged page
  won't re-run until the page is touched (also item 544's territory).
- `snippet` is untrusted text in every mode — page authors control it;
  never render it as HTML. `pages` mode wraps matches in `**` (markdown),
  not HTML tags.
- Search enforces the same visibility docit's own RestMeta enforces:
  authenticated user + published page + active book. Per-book ACLs
  (`Book.permissions` / `can_user_view`) are not consulted — docit itself
  never wired them into REST. If a consumer later adds
  `check_view_permission` to Book, extend the base queryset in
  `knowledge.search` (single choke point) to match.
- The test suite pins the pipeline, hybrid ranking, visibility filters,
  and REST contract in `tests/test_docit/knowledge.py`; the test Postgres
  gets the extension via `bin/create_testproject`.
