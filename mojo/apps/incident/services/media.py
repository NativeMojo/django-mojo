"""Incident record attachment policy.

File relation parsing and VIEW authorization belong to ``fileman.File``.  This
module adds only the incident-domain checks that depend on the parent record.
"""

from mojo import errors as me


MEDIA_UNAVAILABLE = "Media must reference an active, completed File in the record scope"


def _record_parent(record, request, parent_model):
    """Return the saved parent used to scope one REST-supplied attachment."""
    parent_id = getattr(record, "parent_id", None)
    if parent_id:
        return parent_model.objects.only("id", "group_id").get(pk=parent_id)

    supplied_parent = request.DATA.get("parent")
    if not isinstance(supplied_parent, (int, str)):
        raise me.ValueException(MEDIA_UNAVAILABLE)
    try:
        supplied_parent = int(supplied_parent)
    except (TypeError, ValueError):
        raise me.ValueException(MEDIA_UNAVAILABLE)
    if supplied_parent <= 0:
        raise me.ValueException(MEDIA_UNAVAILABLE)
    try:
        return parent_model.objects.only("id", "group_id").get(pk=supplied_parent)
    except parent_model.DoesNotExist:
        raise me.ValueException(MEDIA_UNAVAILABLE)


def validate_record_media(record, candidate, request, parent_model):
    """Validate lifecycle and exact group scope for record ``media``.

    ``File.resolve_rest_related_candidate`` has already resolved the candidate
    exactly once and enforced File VIEW permission before calling this seam.
    """
    try:
        parent = _record_parent(record, request, parent_model)
        manager = candidate.file_manager
    except parent_model.DoesNotExist:
        raise me.ValueException(MEDIA_UNAVAILABLE)

    parent_group_id = parent.group_id
    if (
            not candidate.is_active
            or candidate.upload_status != candidate.COMPLETED
            or not manager.is_active
            or candidate.group_id != parent_group_id
            or manager.group_id != parent_group_id):
        raise me.ValueException(MEDIA_UNAVAILABLE)
