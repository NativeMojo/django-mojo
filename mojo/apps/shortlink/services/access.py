"""Who may put a File behind a public shortlink."""
from mojo import errors as merrors


def _denied():
    # One answer for every failure, so a caller cannot tell a missing file id
    # from one they may not share.
    return merrors.PermissionDeniedException(
        "Permission denied", 403, 403,
        branch="shortlink.file_unavailable", model_name="File")


def shareable_file(request, raw_id):
    """Return the active File `raw_id` names when the caller may share it.

    The check is File's SAVE_PERMS (manage_files, files or owner), the same
    permission fileman's own `share` action needs: a shortlink is a public share
    that can outlive the creator's access. Every failure raises the same 403.
    """
    from mojo.apps.fileman.models import File

    if isinstance(raw_id, bool):
        raise _denied()
    if isinstance(raw_id, int):
        pk = raw_id
    elif isinstance(raw_id, str) and raw_id.strip().isascii() and raw_id.strip().isdigit():
        pk = int(raw_id.strip())
    else:
        raise _denied()
    if pk <= 0:
        raise _denied()

    # rest_check_permission ignores is_active, so filter here.
    file = File.objects.filter(pk=pk, is_active=True).first()
    if file is None:
        raise _denied()

    # The check rebinds request.group to the file's group; restore it so the
    # link is stored in the caller's group, not the file's.
    original_group = getattr(request, "group", None)
    try:
        allowed = File.rest_check_permission(request, "SAVE_PERMS", file)
    finally:
        request.group = original_group
    if not allowed:
        raise _denied()
    return file
