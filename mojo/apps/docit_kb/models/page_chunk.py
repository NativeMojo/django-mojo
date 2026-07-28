from django.db import models
from pgvector.django import VectorField, HnswIndex
from mojo.models import MojoModel

# The column dimension is fixed by migration 0001. Changing it requires a new
# migration plus a full re-embed; mojo.apps.docit_kb.services.knowledge guards
# against an EMBEDDINGS_DIM mismatch rather than writing corrupt vectors.
EMBEDDING_DIM = 1024


class PageChunk(models.Model, MojoModel):
    """
    A heading-scoped slice of a docit Page with its embedding vector.

    Rows are written only by mojo.apps.docit_kb.services.knowledge — the
    embed pipeline chunks page markdown, diffs by content hash, and fills
    embeddings when a provider is available. Never exposed via REST.
    """

    page = models.ForeignKey(
        'docit.Page',
        on_delete=models.CASCADE,
        related_name='chunks',
        help_text="Page this chunk was sliced from"
    )
    position = models.IntegerField(
        default=0,
        db_index=True,
        help_text="Order of the chunk within its page"
    )
    heading = models.CharField(
        max_length=300,
        blank=True,
        default="",
        help_text="Heading breadcrumb, e.g. 'Install > Quickstart'"
    )
    content = models.TextField(
        help_text="Raw markdown slice, including its heading line"
    )
    content_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="sha256 of heading + content; unchanged chunks are never re-embedded"
    )
    embedding = VectorField(
        dimensions=EMBEDDING_DIM,
        null=True,
        blank=True,
        help_text="Vector embedding; null until a provider fills it"
    )
    embed_model = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Model id that produced the embedding"
    )

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['page', 'position']
        indexes = [
            HnswIndex(
                name='docit_kb_chunk_emb_hnsw',
                fields=['embedding'],
                opclasses=['vector_cosine_ops'],
            ),
        ]

    class RestMeta:
        VIEW_PERMS = ['manage_docit', 'docs']
        SAVE_PERMS = ['manage_docit', 'docs']
        # Chunks are derived data written only by the embed pipeline — keep
        # the assistant's generic model tools away from them entirely.
        DENY_AI = True
        GRAPHS = {
            'default': {
                "fields": [
                    'id', 'page', 'position', 'heading', 'content_hash',
                    'embed_model', 'created', 'modified'
                ],
            },
        }

    def __str__(self):
        return f"chunk {self.position} of page {self.page_id}"
