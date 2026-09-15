"""Rendition role lookup preserves first() semantics without discarding prefetch."""
import uuid

from testit import helpers as th

TESTIT_TIER = "framework"


@th.django_unit_test()
def test_rendition_lookup_prefetch_and_fallback(opts):
    from django.db import connection
    from django.db.models import Prefetch
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.fileman.models import File, FileManager, FileRendition

    manager = FileManager.objects.create(
        name=f"rendition-prefetch-{uuid.uuid4().hex}",
        backend_type="file", backend_url="file://", is_default=False)
    try:
        original = File.objects.create(
            file_manager=manager, filename="prefetch.png", storage_file_path="prefetch.png")
        first = FileRendition.objects.create(
            original_file=original, filename="first.png", storage_path="first.png",
            role="thumbnail", content_type="image/png", category="image")
        last = FileRendition.objects.create(
            original_file=original, filename="last.png", storage_path="last.png",
            role="thumbnail", content_type="image/png", category="image")
        FileRendition.objects.create(
            original_file=original, filename="preview.png", storage_path="preview.png",
            role="preview", content_type="image/png", category="image")

        with CaptureQueriesContext(connection) as queries:
            found = original.get_rendition_by_role("thumbnail")
        assert found.pk == first.pk, "fallback selects the lowest primary key"
        assert len(queries) == 1, "unprefetched lookup performs one query"

        cached = File.objects.prefetch_related("file_renditions").get(pk=original.pk)
        with CaptureQueriesContext(connection) as queries:
            found = cached.get_rendition_by_role("thumbnail")
            missing = cached.get_rendition_by_role("missing")
        assert found.pk == first.pk, "default prefetch preserves fallback ordering"
        assert missing is None, "missing cached role stays absent"
        assert len(queries) == 0, "prefetched role lookups issue no queries"

        ordered = File.objects.prefetch_related(Prefetch(
            "file_renditions", queryset=FileRendition.objects.order_by("-pk"))).get(pk=original.pk)
        with CaptureQueriesContext(connection) as queries:
            found = ordered.get_rendition_by_role("thumbnail")
        assert found.pk == last.pk, "explicit prefetch ordering is preserved"
        assert len(queries) == 0, "ordered prefetch stays query-free"

        with CaptureQueriesContext(connection) as queries:
            missing = original.get_rendition_by_role("missing")
        assert missing is None, "missing unprefetched role stays absent"
        assert len(queries) == 1, "missing unprefetched role uses its normal query"
    finally:
        manager.delete()
