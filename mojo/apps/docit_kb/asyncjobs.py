"""
Async job handlers for the docit knowledge base.

Handlers are plain functions taking a Job model instance (mojo.apps.jobs
convention — addressed by dotted path, no registration).
"""
from mojo.helpers import logit

logger = logit.get_logger(__name__, "docit_kb.log")


def embed_page(job):
    """Re-chunk and re-embed one docit page. Payload: {"page_id": N}."""
    from mojo.apps.docit.models import Page
    from mojo.apps.docit_kb.services import knowledge

    page_id = (job.payload or {}).get("page_id")
    if not page_id:
        logger.error(f"embed_page job {job.pk} missing page_id")
        return
    page = Page.objects.filter(pk=page_id).first()
    if page is None:
        logger.info(f"embed_page: page {page_id} no longer exists; nothing to do")
        return
    knowledge.embed_page_now(page)


def reconcile_embeddings(job):
    """Sweep for pages whose chunks fell behind and queue embed jobs. No payload."""
    from mojo.apps.docit_kb.services import knowledge

    result = knowledge.reconcile_stale_pages()
    logger.info(f"reconcile_embeddings job {job.pk} {result}")
    return result
