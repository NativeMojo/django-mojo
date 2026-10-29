"""Who may put a File behind a public shortlink."""
from mojo import errors as merrors


def _denied(branch="shortlink.file_unavailable", model_name="File"):
    # One answer for every failure, so a caller cannot tell a missing file id
    # from one they may not share, or a group refusal from a file refusal.
    return merrors.PermissionDeniedException(
        "Permission denied", 403, 403, branch=branch, model_name=model_name)


def require_group_access(request):
    """Refuse a link stored in a group the caller does not belong to.

    The dispatcher binds any active ?group= to request.group, and checks only
    confined credentials against it. A person must be a member (directly or
    through a parent), or hold manage_shortlinks globally. No group: allowed.
    """
    from mojo.helpers.request import restricted_identity, identity_allows_group

    group = getattr(request, "group", None)
    if group is None:
        return
    if restricted_identity(request) is not None:
        if identity_allows_group(request, group):
            return
        raise _denied("shortlink.group_not_member", None)
    user = getattr(request, "user", None)
    if group.get_member_for_user(user, check_parents=True, is_active=True) is not None:
        return
    if user is not None and user.has_permission("manage_shortlinks"):
        return
    raise _denied("shortlink.group_not_member", None)


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
        # int() refuses more than sys.get_int_max_str_digits() digits.
        try:
            pk = int(raw_id.strip())
        except ValueError:
            raise _denied()
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
