# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.middleware.mojo import ANONYMOUS_USER
from mojo.serializers import get_serializer_manager
from mojo.helpers.settings import settings
from mojo import errors as me
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction, models as dm
import json
import objict
import datetime
from mojo.helpers import dates, logit
from mojo.helpers.request import (
    is_request_user, is_key_backed_session, is_override_user_session,
    restricted_identity)
from contextvars import ContextVar


logger = logit.get_logger("debug", "debug.log")
ACTIVE_REQUEST = ContextVar("ACTIVE_REQUEST", default=None)
LOGGING_CLASS = None
MOJO_APP_STATUS_200_ON_ERROR = settings.MOJO_APP_STATUS_200_ON_ERROR
MOJO_REST_LIST_PERM_DENY = settings.get_static("MOJO_REST_LIST_PERM_DENY", True)

# Django ORM date-component lookup suffixes. Values are integers, not dates,
# so normalize_rest_value must skip its datetime-parse branch when it sees one.
_DATE_COMPONENT_LOOKUPS = {
    "year", "iso_year", "month", "day",
    "week", "week_day", "iso_week_day",
    "quarter", "hour", "minute", "second",
}
_DATE_FIELD_TYPES = ("DateTimeField", "DateField")

# Distinguishes "caller passed request=None" from "caller passed no request at
# all". The two must not behave alike: an explicit None is the documented
# fail-closed idiom (see _can_edit_protected_json), while an absent argument
# falls back to the omnipotent system context.
_UNSET = object()


def system_request():
    """A fresh stand-in request for code running outside an HTTP request
    (jobs, crons, management commands, shell).

    Build one PER CALL. The REST machinery writes to whatever request it is
    given — _evaluate_permission rebinds request.group to a row's owning tenant
    on every FK attach (documented in its docstring) — so a shared instance
    carries one call's tenant into the next, where on_rest_save stamps it onto
    the new row. Same hazard for .DATA, which perm hooks also write to.
    """
    request = objict.objict()
    request.user = objict.objict()
    request.user.id = 1
    request.user.display_name = "System"
    request.user.username = "system"
    request.user.email = ""
    request.user.is_authenticated = True
    request.user.has_permission = lambda perm: True
    request.DATA = objict.objict()
    return request


# DEPRECATED — one shared instance, kept so existing imports keep working.
# Call system_request() instead: passing THIS object into a save path more than
# once lets the previous call's tenant reach the next row.
SYSTEM_REQUEST = system_request()


def _resolve_stamp_actor(request):
    """The user to attribute a write to, or None when there is nobody real.

    request.acting_user — the member an ApiKey acts as (account.ApiKey.user) —
    wins when present, in BOTH key modes: attributing a key's writes to a real
    member is the point of the link, and it is independent of whether the key
    also assumes that member's authority.

    Otherwise request.user, but ONLY when it is an actual model instance.
    is_request_user() cannot tell a pseudo-user from a real User: ANONYMOUS_USER
    and the system request's user are both objicts, and objict.__getattr__
    answers EVERY attribute, so hasattr(user, "is_request_user") is True for
    both (see board item 46). Assigning one to a ForeignKey raises
    ValueError: Cannot assign "{...}" ... must be a "User" instance.

    A non-attributable actor leaves the owner field alone — already the
    behavior for an unlinked ApiKey, which is what the per-app shims did by
    hand (see phonehub/rest/sms.py).
    """
    actor = getattr(request, "acting_user", None)
    if actor is None:
        user = getattr(request, "user", None)
        if (user is not None and getattr(user, "is_authenticated", False)
                and is_request_user(request)):
            actor = user
    if not isinstance(actor, dm.Model):
        return None
    return actor


# Process-local set of class names that have already emitted the CAN_SAVE
# deprecation warning. Keeps the log once-per-class-per-process rather than
# repeating on every request.
_DEPRECATED_CAN_SAVE_WARNED = set()


# Columns no model may ever be filtered, searched or sorted on, whatever it
# declares. `mojo_secrets` is the MojoSecrets/KSMSecrets encrypted blob and it
# lives on an ABSTRACT base shared by ~16 concrete models — several of which
# have no list endpoint of their own but are still reachable through a related
# model's filter. Listing it per subclass would be a standing invitation to
# forget one.
_ALWAYS_SENSITIVE_FIELDS = frozenset(["mojo_secrets"])


def _model_sensitive_fields(model):
    """RestMeta.SENSITIVE_FIELDS for `model`, unioned with the baseline.

    Read pattern deliberately matches rest_aggregation._validate_field: a plain
    getattr, tolerant of a model with no RestMeta at all — a related model
    reached by traversal is not necessarily a MojoModel subclass.
    """
    rest_meta = getattr(model, "RestMeta", None)
    declared = getattr(rest_meta, "SENSITIVE_FIELDS", None) if rest_meta else None
    if not declared:
        return _ALWAYS_SENSITIVE_FIELDS
    return _ALWAYS_SENSITIVE_FIELDS.union(declared)


def is_sensitive_filter_path(model, key):
    """True when any segment of an ORM lookup path names a sensitive field.

    Why a WALK and not a check on key.split("__")[0]: the filter path passes
    relation lookups through untouched, so `?user__auth_key__startswith=a`
    reaches User.auth_key from a model that declares nothing. Reverse relations
    are reachable too, because __rest_field_names__ is built from
    _meta.get_fields() whose ForeignObjectRel.name is the related query name —
    so `?api_keys__token_hash__startswith=a` works from Group. Hopping to the
    related model at each segment is what makes the guard hold on both.

    Sensitivity is tested BEFORE resolving the field, which is the fail-closed
    order. A trailing lookup token (startswith / in / isnull / month) is not a
    field, so the previous iteration leaves `current` None and the next one
    returns False before any sensitivity test — a lookup token can therefore
    never collide with a field name to produce a false positive.

    JSON columns terminate the walk as SENSITIVE rather than as unknown:
    `?metadata__client_secret=x` is a JSON-path extraction the ORM applies
    verbatim, and rest_aggregation._validate_field already refuses exactly this
    shape on the aggregation surface. A model storing secrets in a JSON column
    must name the column itself in SENSITIVE_FIELDS.
    """
    current = model
    for part in key.split("__"):
        if current is None:
            return False
        if part in _model_sensitive_fields(current):
            return True
        try:
            field = current._meta.get_field(part)
        except Exception:
            return False
        if field.is_relation:
            current = getattr(field, "related_model", None)
        elif field.get_internal_type() == "JSONField":
            return True
        else:
            current = None
    return False


def _warn_can_save_deprecated(cls_name):
    if cls_name in _DEPRECATED_CAN_SAVE_WARNED:
        return
    _DEPRECATED_CAN_SAVE_WARNED.add(cls_name)
    logit.warn(
        f"RestMeta.CAN_SAVE is deprecated on {cls_name}; rename to CAN_UPDATE. "
        "Both names are honored for one release (CAN_UPDATE wins when both are set)."
    )


class MojoModel:
    """Base model class for REST operations with GraphSerializer integration."""

    @property
    def active_request(self):
        """Returns the active request being processed."""
        return ACTIVE_REQUEST.get()

    @property
    def active_user(self):
        """Returns the active user being processed."""
        req = ACTIVE_REQUEST.get()
        if req:
            return req.user
        return None

    @classmethod
    def get_rest_meta_prop(cls, name, default=None):
        """
        Retrieve a property from the RestMeta class if it exists.

        Args:
            name (str or list): Name of the property to retrieve.
            default: Default value to return if the property does not exist.

        Returns:
            The value of the requested property or the default value.
        """
        if getattr(cls, "RestMeta", None) is None:
            return default
        if isinstance(name, list):
            for n in name:
                res = getattr(cls.RestMeta, n, None)
                if res is not None:
                    return res
            return default
        return getattr(cls.RestMeta, name, default)

    @classmethod
    def get_rest_meta_graph(cls, graph_name):
        graphs = cls.get_rest_meta_prop("GRAPHS", {})
        if isinstance(graph_name, list):
            for n in graph_name:
                res = graphs.get(n, None)
                if res is not None:
                    return res
            return None
        return graphs.get(graph_name, None)

    @classmethod
    def rest_error_response(cls, request, status=500, **kwargs):
        """
        Create a JsonResponse for an error.

        Args:
            request: Django HTTP request object.
            status (int): HTTP status code for the response.
            kwargs: Additional data to include in the response.

        Returns:
            JsonResponse representing the error.
        """
        payload = dict(kwargs)
        payload["is_authenticated"] = False
        if hasattr(request, "user") and request.user is not None:
            payload["is_authenticated"] = request.user.is_authenticated
        payload["status"] = False
        if "code" not in payload:
            payload["code"] = status
        if MOJO_APP_STATUS_200_ON_ERROR:
            status = 200
        return JsonResponse(payload, status=status)

    @classmethod
    def on_rest_request(cls, request, pk=None):
        """
        Handle REST requests dynamically based on HTTP method.

        Args:
            request: Django HTTP request object.
            pk: Primary key of the object, if available.

        Returns:
            JsonResponse representing the result of the request.
        """
        cls.__rest_field_names__ = [f.name for f in cls._meta.get_fields()]
        if pk:
            instance = cls.get_instance_or_404(pk)
            if isinstance(instance, dict):  # If it's a response, return early
                return instance

            if request.method == 'GET':
                return cls.on_rest_handle_get(request, instance)

            elif request.method in ['POST', 'PUT', 'PATCH']:
                return cls.on_rest_handle_save(request, instance)

            elif request.method == 'DELETE':
                return cls.on_rest_handle_delete(request, instance)
        else:
            return cls.on_handle_list_or_create(request)

        return cls.rest_error_response(request, 500, error=f"{cls.__name__} not found")

    @classmethod
    def get_instance_or_404(cls, pk):
        """
        Helper method to get an instance or return a 404 response.

        Args:
            pk: Primary key of the instance to retrieve.

        Returns:
            The requested instance or raises ValueException for 404 error.
        """
        alt_pk_field = cls.get_rest_meta_prop("ALT_PK_FIELD", "uuid")
        obj = None
        if isinstance(pk, int):
            obj = cls.objects.filter(pk=pk).first()
        elif isinstance(pk, str) and pk.isdigit():
            # String that looks like a number - convert to int
            obj = cls.objects.filter(pk=int(pk)).first()
        elif isinstance(pk, str) and cls.has_field(alt_pk_field):
            # Non-numeric string - use alt_pk_field (UUID, slug, etc.)
            obj = cls.objects.filter(**{alt_pk_field: pk}).first()
        else:
            # Fallback: try pk directly (e.g. models with string primary keys)
            obj = cls.objects.filter(pk=pk).first()
        if obj is None:
            raise me.ValueException(f"{cls.__name__} not found", code=404, status=404)
        return obj

    @classmethod
    def get_instance_from_request(cls, request, field_name=None):
        if field_name is None:
            field_name = cls.__name__.lower()
        if field_name not in request.DATA:
            field_name = f"{field_name}_id"
            if field_name not in request.DATA:
                return None
        return cls.objects.filter(pk=request.DATA.get(field_name)).last()

    @classmethod
    def _evaluate_permission(cls, request, permission_keys, instance=None):
        """
        Evaluate the permission for a request without side effects on the
        incident system.

        Returns a tuple ``(allowed, denial)``:
          - allowed: bool — True if the request has the necessary permissions.
          - denial: objict|None — None when allowed; otherwise an objict with
            ``branch``, ``event_type``, ``status`` so callers (e.g. the raiser)
            can build a PermissionDeniedException with full context. The
            ``perms`` and ``permission_keys`` from the call are caller-known
            and are NOT duplicated here — the raiser stitches them in.

        Side effects: when ``instance`` is provided and exposes ``.group``
        and the predicate falls through to the group-permission branch,
        ``request.group`` is set to ``instance.group``. This mirrors the
        legacy behavior used by downstream handlers and is harmless for
        callers that recover from a False return.
        """
        perms = cls.get_rest_meta_prop(permission_keys, [])
        if perms is None or len(perms) == 0:
            return True, None

        # A model is group-scoped when it has a direct `group` FK OR declares a
        # RestMeta.GROUP_FIELD (which may be a related path, e.g.
        # "original_file__group" or "agent__project"). Both forms must gate the
        # group-membership branch below and the instance-group resolution —
        # otherwise a GROUP_FIELD-only model falls through to a flat, tenantless
        # user.has_permission check and leaks every tenant's rows.
        GROUP_FIELD = cls.get_rest_meta_prop("GROUP_FIELD", None)
        is_group_scoped = bool(GROUP_FIELD) or hasattr(cls, "group")

        if "all" not in perms:
            if request.user is None or not request.user.is_authenticated:
                return False, objict.objict(
                    branch="unauthenticated",
                    event_type="unauthenticated",
                    status=401,
                )

        if "authenticated" in perms:
            return True, None

        # An authenticated MACHINE identity (no is_request_user marker — see
        # mojo/helpers/request.py) that did not register itself as
        # request.api_key has no branch below: falling through to the USER
        # branches would authorize its self-claimed has_permission with no
        # group confinement and no inactive-group gating (DM-019/DM-037). Fail
        # closed. ApiKey always sets request.api_key (validate_token), so this
        # is a no-op today; it exists for custom AUTH_BEARER_HANDLERS
        # identities (mojo/middleware/auth.py), which must either set
        # request.api_key or define the is_request_user marker (DM-045).
        if (request.user is not None
                and getattr(request.user, "is_authenticated", False)
                and not is_request_user(request)
                and not getattr(request, "api_key", None)):
            return False, objict.objict(
                branch="non_user_no_api_key",
                event_type="user_permission_denied",
                status=403,
            )

        if instance is not None:
            # For a machine (api_key) identity, an instance owned by an
            # INACTIVE group is denied BEFORE any instance hook can run — the
            # hooks below return directly, so without this gate a hook that
            # grants via bare api_key.has_permission would reopen the
            # self-reversible-suspension class on detail ops (DM-037/DM-045).
            # The api_key branch's is_active gate further down still covers the
            # instance-less re-bind paths. NOTE: a future "parent key may
            # manage an inactive descendant group's rows" carve-out (see
            # planning item apikey-parent-key-inactive-descendant-one-way-door)
            # belongs HERE, not in the hooks.
            if is_group_scoped and restricted_identity(request) is not None:
                # Same resolver as the re-bind below — see _instance_group.
                inst_group = cls._instance_group(instance)
                # Effective activeness (DM-048): an active group under a
                # deactivated ancestor is inactive too.
                if inst_group is not None and not inst_group.is_effectively_active():
                    return False, objict.objict(
                        branch="api_key.group_inactive",
                        event_type="user_permission_denied",
                        status=403,
                    )
            # Classify the operation from the permission keys: a WRITE carries a
            # write perm-key (CREATE/SAVE/DELETE_PERMS); a read carries only
            # VIEW_PERMS. A read prefers the instance's view hook. A write MUST
            # skip it — otherwise a read affordance (e.g. Group.check_view_permission
            # admits ANY active member with a basic-graph downgrade) would
            # authorize the write. A write is gated by check_edit_permission when
            # the instance defines one, else it falls through to the
            # owner/group/flat branches below.
            WRITE_KEYS = ("CREATE_PERMS", "SAVE_PERMS", "DELETE_PERMS")
            if isinstance(permission_keys, str):
                is_write = permission_keys in WRITE_KEYS
            else:
                is_write = any(k in WRITE_KEYS for k in permission_keys)

            if not is_write and hasattr(instance, "check_view_permission"):
                allowed = instance.check_view_permission(perms, request)
                if allowed:
                    return True, None
                return False, objict.objict(
                    branch="instance.check_view_permission",
                    event_type="view_permission_denied",
                    status=403,
                )

            if hasattr(instance, "check_edit_permission"):
                allowed = instance.check_edit_permission(perms, request)
                if allowed:
                    return True, None
                return False, objict.objict(
                    branch="instance.check_edit_permission",
                    event_type="edit_permission_denied",
                    status=403,
                )

            if "owner" in perms:
                owner_field = instance.get_rest_meta_prop("OWNER_FIELD", "user")
                owner = getattr(instance, owner_field, None)
                # A key-backed session never satisfies "owner", even when it
                # carries the owner's identity (ApiKey.override_user). This is
                # the single choke point that keeps API keys away from the
                # owner-scoped models holding a member's CREDENTIALS — Passkey,
                # UserAPIKey, UserTOTP, OAuthConnection, RegisteredDevice — and
                # it covers any such model added later, with nothing to
                # enumerate.
                #
                # Before override_user existed this was accidental: an ApiKey
                # simply lacked the is_request_user marker and fell through to
                # the groupless-deny branch below. Binding a real User to
                # request.user removes that accident, so the intent has to be
                # stated explicitly here.
                #
                # The rule is about REVOCABILITY, not power: a key that can
                # register a passkey or mint a long-lived token as the member
                # turns a credential you can delete into access you cannot.
                if is_request_user(request) and not is_key_backed_session(request):
                    if owner is not None and owner.id == request.user.id:
                        return True, None
            # Bind request.group to the INSTANCE's owning group so the
            # membership check runs against the row's true tenant, not a
            # caller-supplied ?group=. GROUP_FIELD wins (and may be a related
            # path); a direct `.group` attribute is the legacy fallback.
            #
            # UNCONDITIONAL, INCLUDING None (maestro item 953). A row whose
            # tenant path resolves to null belongs to NO tenant, so the correct
            # binding is None: the group branch below is skipped and the check
            # falls through to request.user.has_permission — the caller's GLOBAL
            # permissions only. Re-binding only when non-null (what GROUP_FIELD
            # used to do) left the caller's ?group= standing, and
            # Group.user_has_permission honors GroupMember grants, so any tenant
            # admin could read another tenant's — or the platform's — null-parent
            # rows. The direct-FK branch always assigned unconditionally; this is
            # that same fail-closed behavior, now for both shapes.
            inst_group = cls._instance_group(instance)
            if request.group and inst_group is None and not getattr(
                    request, "_mojo_group_rebind_logged", False):
                request._mojo_group_rebind_logged = True
                logit.warning(
                    "rest.permission",
                    f"{cls.__name__}(pk={getattr(instance, 'pk', None)}) has no owning "
                    f"group; clearing caller-supplied group "
                    f"{getattr(request.group, 'pk', request.group)} before the "
                    f"permission check")
            request.group = inst_group

        if request.group and is_group_scoped:
            identity = restricted_identity(request)
            if identity is not None:
                # TENANT BOUND. The instance re-bind above sets request.group
                # from the ROW's group, which is what stops a caller-supplied
                # ?group= from widening access. For a key that assumes a member
                # (ApiKey.override_user) that is no longer sufficient on its
                # own: the member may belong to other tenants, so a row in one
                # of those would rebind request.group there and then pass the
                # membership check below. The key's own group tree is the
                # boundary in BOTH modes — a key issued for one tenant must
                # never become a cross-tenant credential because of who it acts
                # as. is_group_allowed covers "the key's group or a descendant,
                # and effectively active" for an ApiKey; for a GroupScopedToken
                # it is the signed group alone, no descendants.
                if not identity.is_group_allowed(request.group):
                    return False, objict.objict(
                        branch="api_key.cross_tenant_denied",
                        event_type="user_permission_denied",
                        status=403,
                    )
                if not request.group.is_effectively_active():
                    # The instance re-bind above can repopulate request.group
                    # from a detail instance owned by a now-inactive group; gate
                    # it here so detail/save/delete fail closed too, not just the
                    # list path (validate_token already strips context there).
                    # Effective activeness (DM-048): a deactivated ancestor
                    # darkens the group too. ApiKey-only — user semantics
                    # unchanged. Instance detail ops are ALSO gated pre-hook at
                    # the top of the instance block (DM-045) — keep both: this
                    # one covers non-instance paths where request.group arrives
                    # inactive.
                    return False, objict.objict(
                        branch="api_key.group_inactive",
                        event_type="user_permission_denied",
                        status=403,
                    )
                # A credential that ASSUMES a member falls through to the normal
                # GroupMember check below — that is the whole point of
                # override_user: one place to manage access, and it is how a
                # GroupScopedToken resolves too (its own has_permission is
                # always False). A reference-mode or unlinked key keeps using
                # its own permissions dict.
                if not identity.override_user:
                    allowed = identity.has_permission(perms)
                    if allowed:
                        return True, None
                    return False, objict.objict(
                        branch="api_key.has_permission",
                        event_type="group_member_permission_denied",
                        status=403,
                    )
            # check_user=False for a key session: Group.user_has_permission
            # short-circuits on the member's GLOBAL permission dict, which has
            # no tenant bound. An override key must be authorized by what its
            # member holds IN THIS GROUP — otherwise the key inherits
            # platform-wide grants, survives removal of the GroupMember row,
            # and (since User.has_permission returns True for everything when
            # is_superuser) would silently become a superuser key the moment
            # the linked member is promoted.
            allowed = request.group.user_has_permission(
                request.user, perms, not is_override_user_session(request))
            if allowed:
                return True, None
            return False, objict.objict(
                branch="group.user_has_permission",
                event_type="group_member_permission_denied",
                status=403,
            )
        elif restricted_identity(request) is not None:
            # Reaching this branch means the request is NOT confined to a group
            # (a groupless model, or a null-group instance of a group-FK model).
            # A group-scoped ApiKey must not get unconfined/cross-tenant access:
            # on_rest_list cannot group-filter a groupless model, so a
            # self-claimed permission would read every tenant's rows. Deny by
            # default; a model may opt in via RestMeta.ALLOW_API_KEY_GLOBAL
            # (parallel to the requires_global_perms decorator's allow_api_keys).
            # Group-scoped models take the Branch above (request.group +
            # hasattr(cls,"group")) and are unaffected.
            #
            # A GroupScopedToken is denied HERE, before ALLOW_API_KEY_GLOBAL is
            # even read: that flag is an ApiKey-specific opt-in (a provisioned
            # server credential is the intended caller) and a visitor-grade
            # token must not inherit it. Without this elif covering group
            # tokens at all, one would fall past the branch to the flat
            # request.user.has_permission below and a visitor holding any
            # global grant would read every groupless model.
            if getattr(request, "group_token", None) is not None:
                return False, objict.objict(
                    branch="group_token.groupless_denied",
                    event_type="user_permission_denied",
                    status=403,
                )
            allow_global = cls.get_rest_meta_prop("ALLOW_API_KEY_GLOBAL", False)
            if allow_global and is_group_scoped:
                # Misconfiguration: a group-scoped model must never opt into
                # global api-key access — a key could reach unscoped rows by
                # arriving with no active group context. Fail closed and shout
                # so it's discoverable.
                logit.error(
                    f"ALLOW_API_KEY_GLOBAL=True on group-scoped model {cls.__name__} "
                    "— ignored (fail-closed). Remove the flag or the group FK.")
                allow_global = False
            if not allow_global:
                return False, objict.objict(
                    branch="api_key.groupless_denied",
                    event_type="user_permission_denied",
                    status=403,
                )
            allowed = request.api_key.has_permission(perms)
            if allowed:
                return True, None
            return False, objict.objict(
                branch="api_key.has_permission",
                event_type="user_permission_denied",
                status=403,
            )
        if request.user is None or not request.user.is_authenticated:
            return False, objict.objict(
                branch="unauthenticated",
                event_type="unauthenticated",
                status=401,
            )
        allowed = request.user.has_permission(perms)
        if allowed:
            return True, None
        return False, objict.objict(
            branch="user.has_permission",
            event_type="user_permission_denied",
            status=403,
        )

    @classmethod
    def rest_check_permission(cls, request, permission_keys, instance=None):
        """
        Pure boolean predicate — returns True/False with no event emission.

        Use this when the caller wants to recover gracefully from a denial
        (e.g. fall through to a scoped/empty list, silently skip an FK
        attach). For HTTP handlers that should respond 401/403 on denial,
        use :meth:`rest_check_permission_or_raise` instead so the dispatcher
        emits a single, properly-categorized incident event.

        Args:
            request: Django HTTP request object.
            permission_keys: RestMeta key(s) (e.g. "VIEW_PERMS", or
                ["SAVE_PERMS", "VIEW_PERMS"]) whose perm lists are checked.
            instance: Optional instance for instance-level checks
                (check_view_permission / check_edit_permission, owner match).

        Side effects: see :meth:`_evaluate_permission` for the
        ``request.group`` write that may occur when ``instance`` exposes
        ``.group``.

        Returns:
            True when allowed, False otherwise.
        """
        allowed, _ = cls._evaluate_permission(request, permission_keys, instance)
        return allowed

    @classmethod
    def rest_check_permission_or_raise(cls, request, permission_keys, instance=None):
        """
        Permission predicate that raises on denial.

        Like :meth:`rest_check_permission`, but raises
        ``PermissionDeniedException`` with full metadata
        (``branch``, ``perms``, ``permission_keys``, ``model_name``,
        ``instance``, ``event_type``, ``status``) when the predicate
        returns False. The dispatcher in mojo/decorators/http.py catches
        the exception, emits exactly one incident event, and serializes
        the JSON response.

        Returns True on allow.
        """
        allowed, denial = cls._evaluate_permission(request, permission_keys, instance)
        if allowed:
            return True
        perms = cls.get_rest_meta_prop(permission_keys, [])
        reason = f"Permission denied: {denial.event_type}"
        raise me.PermissionDeniedException(
            reason=reason,
            status=denial.status,
            code=denial.status,
            branch=denial.branch,
            perms=list(perms) if perms else None,
            permission_keys=permission_keys,
            model_name=cls.__name__,
            instance=repr(instance) if instance is not None else None,
            event_type=denial.event_type,
        )

    @classmethod
    def on_rest_handle_get(cls, request, instance):
        """
        Handle GET requests with permission checks.

        Args:
            request: Django HTTP request object.
            instance: The instance to retrieve.

        Returns:
            JsonResponse representing the result of the GET request.
        """
        cls.rest_check_permission_or_raise(request, "VIEW_PERMS", instance)
        return instance.on_rest_get(request)

    @classmethod
    def on_rest_handle_save(cls, request, instance):
        """
        Handle POST and PUT requests on an existing instance with permission checks.

        Gated by the ``CAN_UPDATE`` RestMeta flag (default True). ``CAN_SAVE``
        is accepted as a deprecated alias for one release — a once-per-process
        warning is emitted if only ``CAN_SAVE`` is set. When both are set,
        ``CAN_UPDATE`` wins.

        Args:
            request: Django HTTP request object.
            instance: The instance to save or update.

        Returns:
            JsonResponse representing the result of the save operation.
        """
        if not cls._resolve_can_update():
            raise me.PermissionDeniedException(
                reason=f"UPDATE not allowed: {cls.__name__}",
                model_name=cls.__name__,
                event_type="feature_disabled",
                branch="can_update_false",
            )

        cls.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"], instance)
        return instance.on_rest_save_and_respond(request)

    @classmethod
    def _resolve_can_update(cls):
        """
        Resolve the effective ``CAN_UPDATE`` flag (default True), honoring the
        deprecated ``CAN_SAVE`` alias for one release: if ``CAN_UPDATE`` is unset
        but ``CAN_SAVE`` is set, ``CAN_SAVE`` is used and a once-per-process
        deprecation warning is emitted. Shared by ``on_rest_handle_save`` and the
        per-row batch gate so the alias/default semantics never drift.
        """
        can_update = cls.get_rest_meta_prop("CAN_UPDATE", None)
        if can_update is None:
            can_save = cls.get_rest_meta_prop("CAN_SAVE", None)
            if can_save is not None:
                _warn_can_save_deprecated(cls.__name__)
                can_update = can_save
            else:
                can_update = True
        return can_update

    @classmethod
    def on_rest_handle_delete(cls, request, instance):
        """
        Handle DELETE requests with permission checks.

        Args:
            request: Django HTTP request object.
            instance: The instance to delete.

        Returns:
            JsonResponse representing the result of the delete operation.
        """
        if not cls.get_rest_meta_prop("CAN_DELETE", False):
            raise me.PermissionDeniedException(
                reason=f"DELETE not allowed: {cls.__name__}",
                model_name=cls.__name__,
                event_type="feature_disabled",
                branch="can_delete_false",
            )

        cls.rest_check_permission_or_raise(request, ["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"], instance)
        return instance.on_rest_delete(request)

    @classmethod
    def on_rest_handle_list(cls, request):
        """
        Handle GET requests for listing resources with permission checks.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the list of resources.
        """
        # cls.debug("on_rest_handle_list")
        if cls.rest_check_permission(request, "VIEW_PERMS"):
            return cls.on_rest_list(request)

        # Advanced permission checks if basic check fails
        perms = cls.get_rest_meta_prop("VIEW_PERMS", [])

        # Check for owner permission.
        #
        # The `not is_key_backed_session` clause is the LIST-path twin of the
        # detail-path guard in _evaluate_permission. Both must be present: a key
        # that assumes a member (ApiKey.override_user) puts a real User in
        # request.user, so the marker test alone becomes True and every
        # owner-scoped model opens up — GET /api/user would return the member's
        # own row (email, phone, dob, permissions, is_superuser) to a caller
        # that /api/user/<pk> correctly refuses, and /api/account/api_keys,
        # /passkeys, /oauth_connection would return their credential records.
        # Neither model defines an instance hook and none are group-scoped, so
        # nothing downstream catches it.
        if (perms and "owner" in perms and request.user.is_authenticated
                and is_request_user(request)
                and not is_key_backed_session(request)):
            owner_field = cls.get_rest_meta_prop("OWNER_FIELD", "user")
            if owner_field == "self":
                q = {"pk": request.user.pk}
            else:
                q = {owner_field: request.user}
            return cls.on_rest_list(request, cls.objects.filter(**q))

        # Check if the model is group-scoped (direct `group` FK OR a
        # RestMeta.GROUP_FIELD path) and the user might have group-level
        # permissions on some tenant even without a system-level grant.
        group_field = cls.get_rest_meta_prop("GROUP_FIELD", None)
        if request.user.is_authenticated and (group_field or hasattr(cls, "group")):
            # User doesn't have system-level permissions, but might have group-level permissions.
            #
            # Derive from the KEY for a key-backed session, never from the
            # member it acts as. ApiKey.get_groups_with_permission returns
            # none() for an effectively-inactive chain, which is what makes
            # tenant suspension (DM-037/DM-048) actually suspend. Reading
            # request.user here would return every tenant the member belongs
            # to — and when the key's own group is deactivated,
            # validate_token leaves request.group None, so on_rest_list applies
            # NO group filter and those other tenants' rows would be served.
            # Suspension would become a cross-tenant reader.
            identity = restricted_identity(request) or request.user
            groups_with_perms = identity.get_groups_with_permission(perms)
            if groups_with_perms.exists():
                # Filter queryset to only include objects from groups where user has permission
                q = {f"{group_field or 'group'}__in": groups_with_perms}
                return cls.on_rest_list(request, cls.objects.filter(**q))

        if not request.user.is_authenticated:
            raise me.PermissionDeniedException(
                reason=f"GET permission denied: {cls.__name__}",
                status=401,
                code=401,
                model_name=cls.__name__,
                perms=list(perms) if perms else None,
                permission_keys="VIEW_PERMS",
                branch="list_unauthenticated",
                event_type="unauthenticated",
            )

        if MOJO_REST_LIST_PERM_DENY:
            raise me.PermissionDeniedException(
                reason=f"GET permission denied: {cls.__name__}",
                model_name=cls.__name__,
                perms=list(perms) if perms else None,
                permission_keys="VIEW_PERMS",
                branch="list_perm_deny",
                event_type="user_permission_denied",
            )
        return cls.on_rest_list_response(request, cls.objects.none())

    def update_from_dict(self, dict_data):
        request = ACTIVE_REQUEST.get() or system_request()
        return self.on_rest_save(request, dict_data)

    @classmethod
    def create_from_dict(cls, dict_data, **kwargs):
        # _UNSET, not None: an explicitly passed request=None keeps reaching
        # on_rest_save (and raising) exactly as before. The sentinel also stops
        # the fallback being built on every call that supplies its own request.
        request = kwargs.pop('request', _UNSET)
        if request is _UNSET:
            request = ACTIVE_REQUEST.get() or system_request()
        instance = cls(**kwargs)
        instance.on_rest_save(request, dict_data)
        instance.on_rest_created()
        if cls.get_rest_meta_prop("LOG_CHANGES", False):
            instance.log(kind="model:created", log=f"{request.user.username} created {instance.pk}")
        return instance

    @classmethod
    def create_from_request(cls, request, **kwargs):
        instance = cls(**kwargs)
        instance.on_rest_save(request, request.DATA)
        instance.on_rest_created()
        if cls.get_rest_meta_prop("LOG_CHANGES", False):
            instance.log(kind="model:created", log=f"{request.user.username} created {instance.pk}")
        return instance

    @classmethod
    def on_rest_handle_create(cls, request):
        """
        Handle POST and PUT requests for creating resources with permission checks.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the result of the create operation.
        """
        if not cls.get_rest_meta_prop("CAN_CREATE", True):
            raise me.PermissionDeniedException(
                reason=f"CREATE not allowed: {cls.__name__}",
                model_name=cls.__name__,
                event_type="feature_disabled",
                branch="can_create_false",
            )

        cls.rest_check_permission_or_raise(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"])
        instance = cls.create_from_request(request)
        return instance.on_rest_get(request)

    @classmethod
    def on_handle_list_or_create(cls, request):
        """
        Handle listing (GET without pk) and creating (POST/PUT without pk) operations.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the result of the operation.
        """
        if request.method == 'GET':
            return cls.on_rest_handle_list(request)
        elif request.method in ['POST', 'PUT', 'PATCH']:
            # Batch save mode: if 'batched' list is present and model allows batching
            batched = request.DATA.get("batched")
            if batched and isinstance(batched, list) and cls.get_rest_meta_prop("CAN_BATCH", False):
                return cls.on_rest_handle_batch(request)
            return cls.on_rest_handle_create(request)

    @classmethod
    def on_rest_handle_batch(cls, request):
        """
        Handle batch create/update when request.DATA includes a 'batched' list
        and RestMeta.CAN_BATCH is True.

        Each item in 'batched':
          - If contains 'id' or 'pk', attempts to update that instance via update_from_dict
          - Otherwise creates a new instance via create_from_dict

        Every row is re-checked per instance with the same permission keys as
        the single-instance paths (owner match, group/GROUP_FIELD tenant
        binding, check_view/edit_permission hooks). A denied row is dropped
        with a per-row error entry and a `batch_row_denied` incident — the
        rest of the batch proceeds (rows are written sequentially with no
        transaction, so failing the whole batch could not undo earlier rows).

        Returns a JSON response with serialized results and optional errors.
        """
        if not cls.get_rest_meta_prop("CAN_BATCH", False):
            raise me.PermissionDeniedException(
                reason=f"BATCH not allowed: {cls.__name__}",
                model_name=cls.__name__,
                event_type="feature_disabled",
                branch="can_batch_false",
            )

        cls.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])

        batched = request.DATA.get("batched")
        if not isinstance(batched, list):
            return cls.rest_error_response(request, 400, error="Invalid 'batched' payload: expected a list")

        # Per-verb feature flags are class-level and static — resolve once. Update
        # rows honor CAN_UPDATE (with the CAN_SAVE alias, via the shared resolver);
        # create rows honor CAN_CREATE. A disabled verb drops its rows with the same
        # drop-with-audit convention as the permission checks, so a mixed batch on a
        # single-verb-disabled model still applies the rows for the enabled verb.
        can_update = cls._resolve_can_update()
        can_create = cls.get_rest_meta_prop("CAN_CREATE", True)

        results = []
        errors = []
        original_group = getattr(request, "group", None)
        for idx, item in enumerate(batched):
            # Reset the tenant binding a previous row's permission check may
            # have left on the request (_evaluate_permission sets request.group
            # to the row's owning group as a side effect).
            request.group = original_group
            try:
                if not isinstance(item, dict):
                    raise ValueError("Batch item must be an object")
                pk = item.get("id") or item.get("pk")
                instance = cls.objects.filter(pk=pk).first() if pk else None
                if instance is not None:
                    # Feature gate before the permission gate, mirroring
                    # on_rest_handle_save's ordering.
                    if not can_update:
                        cls._report_batch_row_feature_disabled(
                            request, instance, idx, branch="batch_can_update_false")
                        errors.append({"index": idx, "error": "UPDATE not allowed"})
                        continue
                    # Same keys + instance as on_rest_handle_save. Boolean
                    # check (not _or_raise): a raise here would be swallowed
                    # by the except below and lose the audit event.
                    if not cls.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], instance):
                        cls._report_batch_row_denied(request, instance, idx, branch="batch_update")
                        errors.append({"index": idx, "error": "permission denied"})
                        continue
                    instance.update_from_dict(item)
                else:
                    # Feature gate before the permission gate, mirroring
                    # on_rest_handle_create's ordering.
                    if not can_create:
                        cls._report_batch_row_feature_disabled(
                            request, None, idx, branch="batch_can_create_false")
                        errors.append({"index": idx, "error": "CREATE not allowed"})
                        continue
                    # Same keys as on_rest_handle_create.
                    if not cls.rest_check_permission(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"]):
                        cls._report_batch_row_denied(request, None, idx, branch="batch_create")
                        errors.append({"index": idx, "error": "permission denied"})
                        continue
                    instance = cls.create_from_dict(item, request=request)
                results.append(instance)
            except Exception as e:
                errors.append({"index": idx, "error": str(e)})

        # The last row's check may have left its tenant bound on the request;
        # downstream incident reporting auto-stamps request.group, so restore
        # the caller's group before anything after the loop can observe it.
        request.group = original_group

        # Serialize results using the same serializer manager used elsewhere
        graph = request.DATA.get("graph", "list")
        manager = get_serializer_manager()
        serializer = manager.get_serializer(results, graph=graph, many=True)
        data = serializer.serialize()

        payload = {"items": data, "count": len(results)}
        if errors:
            payload["errors"] = errors
        return cls.return_rest_response(payload)

    @classmethod
    def on_rest_list(cls, request, queryset=None):
        """
        List objects with filtering, sorting, and pagination.

        Args:
            request: Django HTTP request object.
            queryset: Optional initial queryset to use.

        Returns:
            JsonResponse representing the paginated and serialized list of objects.
        """
        if queryset is None:
            queryset = cls.objects.all()
        # for better query perfomance we want raw request GET data
        request.QUERY_PARAMS = request.GET.copy()
        if request.group is not None:
            GROUP_FIELD = cls.get_rest_meta_prop("GROUP_FIELD", None)
            if GROUP_FIELD or hasattr(cls, "group"):
                if "group" in request.QUERY_PARAMS:
                    del request.QUERY_PARAMS["group"]
                if GROUP_FIELD:
                    q = {GROUP_FIELD: request.group}
                else:
                    q = {"group": request.group}
                queryset = queryset.filter(**q)
        queryset = cls.on_rest_list_filter(request, queryset)
        queryset = cls.on_rest_list_date_range_filter(request, queryset)
        mode = request.DATA.get("_mode")
        if mode and mode != "list":
            from mojo.models.rest_aggregation import on_rest_list_aggregate
            return on_rest_list_aggregate(cls, request, queryset)
        queryset = cls.on_rest_list_sort(request, queryset)
        return cls.on_rest_list_response(request, queryset)

    @classmethod
    def on_rest_list_response(cls, request, queryset):
        # Implement pagination
        page_size = request.DATA.get_typed(["size", "limit"], 10, int)
        page_start = request.DATA.get_typed(["start", "offset"], 0, int)
        page_end = page_start+page_size
        paged_queryset = queryset[page_start:page_end]
        graph = request.DATA.get("graph", "list")
        format = request.DATA.get("download_format", "json")
        count = queryset.count()
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        if format != "json":
            format_key = format.split("_")[0]
            serializer = manager.get_format_serializer(format_key)
            formats = cls.get_rest_meta_prop("FORMATS")
            localize = None

            if formats is not None and format in formats:
                fields = formats[format]
            else:
                graph_obj = cls.get_rest_meta_graph(["basic", "default"])
                if not graph_obj or not graph_obj.get("fields"):
                    raise me.ValueException("No valid graph found")
                fields = graph_obj.get("fields")
                # Get localize config from graph if available
                localize = graph_obj.get("localize")

            # Check if localize is defined in FORMATS_LOCALIZE
            formats_localize = cls.get_rest_meta_prop("FORMATS_LOCALIZE")
            if formats_localize and format in formats_localize:
                localize = formats_localize[format]

            # Get timezone from request for localization
            timezone = request.DATA.get("timezone")

            # logger.info(f"Serializing queryset with fields: {fields}")
            return serializer.serialize_queryset(
                queryset,
                fields=fields,
                filename=request.DATA.get("filename", f"{cls.__name__}.csv"),
                localize=localize,
                timezone=timezone
            )
        serializer = manager.get_serializer(paged_queryset, graph=graph, many=True)
        resp = serializer.to_response(request, count=count, start=page_start, size=page_size)
        resp.log_context = {
            "endpoint": "list",
            "model": cls.__name__,
            "page_size": page_size,
            "page_start": page_start,
            "page_end": page_end,
            "graph": graph,
            "count": count
        }
        return resp

    @classmethod
    def _resolve_filter_timezone(cls, request):
        """Pick the timezone for partial-date expansion.

        Precedence: explicit ``timezone`` request param → ``request.group.timezone``
        → UTC. Mirrors how CSV localization at on_rest_list_response resolves it.
        """
        tz = request.DATA.get("timezone") if hasattr(request, "DATA") else None
        if tz:
            return tz
        group = getattr(request, "group", None)
        if group is not None:
            tz = getattr(group, "timezone", None)
            if tz:
                return tz
        return None

    @classmethod
    def on_rest_list_date_range_filter(cls, request, queryset):
        """
        Filter queryset based on a date range provided in the request.

        Accepts both full ISO datetimes (existing behavior) and partial dates
        (``2026``, ``2026-04``, ``2026-04-02``). When a partial date is given,
        ``dr_start`` expands to the start of the period and ``dr_end`` to the
        end, anchored in the request's timezone (``request.DATA["timezone"]``
        → ``request.group.timezone`` → UTC).

        Args:
            request: Django HTTP request object.
            queryset: The queryset to filter.

        Returns:
            The filtered queryset.
        """
        dr_field = request.DATA.get("dr_field", "created")
        dr_start = request.DATA.get("dr_start")
        dr_end = request.DATA.get("dr_end")

        tz = cls._resolve_filter_timezone(request)

        if dr_start:
            parts = dates.parse_partial_date(dr_start)
            if parts is not None:
                try:
                    dr_start_dt, _ = dates.partial_date_to_range(parts, timezone=tz)
                except ValueError as e:
                    raise me.ValueException(f"Invalid dr_start: {e}", code=400, status=400)
            else:
                dr_start_dt = dates.parse_datetime(dr_start)
                if request.group:
                    dr_start_dt = request.group.get_local_time(dr_start_dt)
            queryset = queryset.filter(**{f"{dr_field}__gte": dr_start_dt})

        if dr_end:
            parts = dates.parse_partial_date(dr_end)
            if parts is not None:
                try:
                    _, dr_end_dt = dates.partial_date_to_range(parts, timezone=tz)
                except ValueError as e:
                    raise me.ValueException(f"Invalid dr_end: {e}", code=400, status=400)
            else:
                dr_end_dt = dates.parse_datetime(dr_end)
                if request.group:
                    dr_end_dt = request.group.get_local_time(dr_end_dt)
            queryset = queryset.filter(**{f"{dr_field}__lte": dr_end_dt})
        return queryset

    @classmethod
    def normalize_rest_value(cls, request, field_name, value, lookup=None):
        """Coerce a raw query-param value to the right Python type for filter().

        ``lookup`` is the trailing ``__<suffix>`` token from the filter key.
        When it names a date-component lookup (``year``, ``month``, ``day``,
        ``quarter``, …), the value is coerced to int (or list-of-int for
        ``__in``) instead of being parsed as a datetime — this lets
        ``?created__month=4`` work on a ``DateTimeField`` without the value
        being mangled by ``dates.parse_datetime``.
        """
        if lookup in _DATE_COMPONENT_LOOKUPS:
            try:
                if isinstance(value, list):
                    return [int(v) for v in value]
                return int(value)
            except (TypeError, ValueError):
                raise me.ValueException(
                    f"Invalid value for {field_name}__{lookup}: {value!r}",
                    code=400, status=400,
                )

        field = cls.get_model_field(field_name)
        # Preserve boolean values (e.g., __isnull filters) and do not coerce them to dates
        if isinstance(value, bool):
            return value
        if field.get_internal_type() == "BooleanField":
            if isinstance(value, str):
                value = value.lower() in ("true", "1")
            elif isinstance(value, int):
                value = bool(value)
            else:
                value = bool(value)
        elif value is not None:
            if field.get_internal_type() in ["DateTimeField", "DateField"]:
                if not isinstance(value, (datetime.datetime, datetime.date)):
                    value = dates.parse_datetime(value)
        elif field.get_internal_type() in ["IntegerField", "BigIntegerField"]:
            if isinstance(value, (str, bool)):
                value = int(value)
            elif isinstance(value, bool):
                value = int(value)
        return value

    @classmethod
    def _report_sensitive_probe(cls, request, key):
        """Raise a security event for a dropped sensitive filter.

        The drop itself is silent to the CALLER by design, but it must not be
        silent to the operator: filtering on a secret column is, by definition,
        a credential-enumeration attempt, and it is the highest-signal event on
        the whole list surface. Mirrors the assistant tool layer, which reports
        the same probe class at severity 7.

        Best-effort — a reporting failure must never break a list request. Only
        the KEY is reported, never the value: the key is attacker-supplied but
        reaching here means a segment matched a name the model itself declares.
        """
        try:
            from mojo.apps.incident import reporter
            reporter.report_event(
                f"Sensitive field filter attempt: {key} on {cls.__name__}",
                title="Sensitive field filter blocked",
                category="sensitive_field_probe",
                level=7,
                request=request)
        except Exception:
            logit.exception("failed to report sensitive-field probe")

    @classmethod
    def build_rest_filters(cls, request, params):
        """
        Parse a query-param-shaped mapping into (filters, excludes) dicts.

        Shared by the list-filter path (``request.QUERY_PARAMS``) and the
        aggregation ``_stats`` bundle counter, so a stat bundle's filter
        semantics are identical to the equivalent list query params — same
        operators (__in / __not / __not_in / __isnull), same value coercion,
        same silent skip of reserved and ``_``-prefixed keys. Values may be
        raw strings (query params) or JSON-native (bundles: list / int / bool
        / None).

        Returns:
            (filters, excludes) — two dicts for ``queryset.filter(**filters)``
            / ``queryset.exclude(**excludes)``. Search and application stay
            with the caller.
        """
        reserved_keys = ["start", "size", "download_format", "dr_start", "dr_end", "limit", "offset"]
        filters = {}
        excludes = {}
        if not hasattr(cls, '__rest_field_names__'):
            cls.__rest_field_names__ = [f.name for f in cls._meta.get_fields()]

        for key, value in params.items():
            # Framework-reserved namespace: every `_`-prefixed key is
            # consumed by the aggregation surface (or other framework
            # features). Never confuse with a model field.
            if key.startswith("_"):
                continue
            # Split key to check for foreign key relationships
            if "." in key:
                key = key.replace(".", "__")
            key_parts = key.split('__')
            field_name = key_parts[0]
            if field_name in reserved_keys:
                continue

            # Sensitive columns are not filterable. The COUNT a filter produces
            # is itself the leak: ?auth_key__startswith=a vs =ab recovers the
            # stored value one character at a time from the list envelope's
            # `count` alone, with no need to ever read the field.
            #
            # This sits at the shared parser rather than at on_rest_list_filter
            # because rest_aggregation._count_bundles calls build_rest_filters
            # directly — guarding the wrapper would leave `_stats` open. One
            # placement here covers the plain list path, _mode=count and _stats,
            # and it runs BEFORE the __not/__not_in rewrites below so every
            # operator form of a sensitive key is dropped, not just the bare one.
            #
            # DROP-SILENT, not 400: _count_bundles catches ValueException by
            # design and turns the bundle null, so a raise could not behave
            # identically across the three surfaces. Dropping makes all three
            # act as if the key were never sent, which is also how this parser
            # already treats unknown and reserved keys.
            if is_sensitive_filter_path(cls, key):
                logit.warn(f"dropped sensitive filter on {cls.__name__}: {key}")
                cls._report_sensitive_probe(request, key)
                continue

            # Determine if this is an exclusion filter
            is_exclusion = key.endswith("__not") or key.endswith("__not_in")
            target_dict = excludes if is_exclusion else filters

            # Trailing lookup token (e.g. "month" in "created__month") — used
            # to recognize Django date-component lookups so we don't try to
            # parse the value as a datetime.
            trailing_lookup = key_parts[-1] if len(key_parts) > 1 else None

            if field_name in cls.__rest_field_names__ and cls._meta.get_field(field_name).is_relation:
                # TODO Normalize relation field values
                if key.endswith("__in"):
                    value = value.split(",") if isinstance(value, str) else value
                elif key.endswith("__not_in"):
                    # Convert __not_in to __in for exclude()
                    key = key.replace("__not_in", "__in")
                    value = value.split(",") if isinstance(value, str) else value
                elif key.endswith("__not"):
                    # Remove __not suffix for exclude()
                    key = key.replace("__not", "")
                elif key.endswith("__isnull"):
                    value = str(value).strip().lower() in ("true", "1", "yes", "y", "t")
                elif value == "null":
                    value = None
                target_dict[key] = value
            elif hasattr(cls, field_name):
                # Partial-date field shorthand: ?created=2026-04 expands to a
                # local-tz UTC range so __month/__year semantics are correct
                # under non-UTC databases. Only fires on the bare exact-match
                # key (no operator suffix) and only on date/datetime fields.
                if trailing_lookup is None:
                    field_obj = cls.get_model_field(field_name)
                    if field_obj is not None and field_obj.get_internal_type() in _DATE_FIELD_TYPES:
                        parts = dates.parse_partial_date(value)
                        if parts is not None:
                            tz = cls._resolve_filter_timezone(request)
                            try:
                                start, end = dates.partial_date_to_range(parts, timezone=tz)
                            except ValueError as e:
                                raise me.ValueException(
                                    f"Invalid {field_name}: {e}", code=400, status=400,
                                )
                            filters[f"{field_name}__gte"] = start
                            filters[f"{field_name}__lte"] = end
                            continue

                if key.endswith("__in"):
                    value = value.split(",") if isinstance(value, str) else value
                elif key.endswith("__not_in"):
                    # Convert __not_in to __in for exclude()
                    key = key.replace("__not_in", "__in")
                    value = value.split(",") if isinstance(value, str) else value
                elif key.endswith("__not"):
                    # Remove __not suffix for exclude()
                    key = key.replace("__not", "")
                elif key.endswith("__isnull"):
                    value = str(value).strip().lower() in ("true", "1", "yes", "y", "t")
                elif value == "null":
                    value = None

                # Detect a date-component lookup anywhere in the key parts.
                # ``created__month`` is parts[-1], ``created__month__in`` is
                # parts[-2] after the __in suffix, etc. Scan all parts after
                # the field name so the int coercion fires regardless of
                # which operator suffix is composed on top.
                normalized_lookup = None
                for part in key.split("__")[1:]:
                    if part in _DATE_COMPONENT_LOOKUPS:
                        normalized_lookup = part
                        break
                target_dict[key] = cls.normalize_rest_value(
                    request, field_name, value, lookup=normalized_lookup,
                )

        return filters, excludes

    @classmethod
    def on_rest_list_filter(cls, request, queryset):
        """
        Apply query-param filtering to the list queryset.

        Parses ``request.QUERY_PARAMS`` via ``build_rest_filters`` then applies
        search, inclusion filters, and exclusion filters. Supported operators:
        - Regular filters: ?category=ossec (include)
        - __in filters: ?category__in=ossec,system (include multiple)
        - __not filters: ?category__not=ossec (exclude single value)
        - __not_in filters: ?category__not_in=ossec,system (exclude multiple)
        - __isnull filters: ?category__isnull=true (NULL check)

        Any ``RestMeta.LIST_DEFAULT_FILTERS`` the model declares are layered in
        first as a baseline the request can override — see
        ``on_rest_list_default_filters``.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to filter.

        Returns:
            The filtered queryset.
        """
        filters, excludes = cls.build_rest_filters(request, request.QUERY_PARAMS)
        queryset = cls.on_rest_list_default_filters(request, queryset, filters, excludes)
        queryset = cls.on_rest_list_search(request, queryset)
        queryset = queryset.filter(**filters)
        if excludes:
            queryset = queryset.exclude(**excludes)
        return queryset

    @classmethod
    def on_rest_list_default_filters(cls, request, queryset, filters, excludes):
        """
        Apply RestMeta.LIST_DEFAULT_FILTERS as a baseline the request can override.

        The declared filters narrow a list endpoint by default — a notification
        list showing only unread rows, say. They are a baseline, not a cage:

        - A request param naming the same BASE FIELD replaces that default
          entirely. ``?is_unread=false``, ``?is_unread__not=true``,
          ``?is_unread__in=true,false`` and ``?user.id=3`` all suppress a
          default keyed on that field. Suppression is per field rather than per
          key, and covers exclusion filters as well as inclusion ones — a
          key-level check would leave an ``is_unread=True`` default sitting in
          ``filters`` while the caller's ``__not`` landed in ``excludes``, and
          AND the two into a guaranteed-empty list.
        - ``?_no_defaults=1`` drops every default for the request, for admin
          surfaces that must see the full set. Read via ``get_typed(..., bool)``
          because the raw query-param value ``"0"`` is a truthy string.

        Defaults AND onto whatever queryset the caller supplied, so the
        permission-scoped querysets ``on_rest_handle_list`` passes in keep their
        scoping — a default can only narrow a list, never widen it.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to narrow.
            filters: Inclusion filters already parsed from the request.
            excludes: Exclusion filters already parsed from the request.

        Returns:
            The queryset, narrowed by whichever defaults still apply.
        """
        defaults = cls.get_rest_meta_prop("LIST_DEFAULT_FILTERS", None)
        if not defaults:
            return queryset
        if request.DATA.get_typed("_no_defaults", False, bool):
            return queryset
        named = {key.split("__")[0] for key in filters}
        named.update(key.split("__")[0] for key in excludes)
        applied = {k: v for k, v in defaults.items() if k.split("__")[0] not in named}
        if applied:
            queryset = queryset.filter(**applied)
        return queryset

    @classmethod
    def on_rest_list_search(cls, request, queryset):
        """
        Search queryset based on 'search' param in the request for fields defined in 'SEARCH_FIELDS'.

        Supports advanced search syntax:
        - "bob smith" - searches for the exact phrase "bob smith"
        - bob smith - searches for records matching BOTH "bob" AND "smith" (default AND logic)
        - -excluded - exclude records containing "excluded"
        - field:value - search only in a specific field (e.g., email:john@example.com)
        - Combined: term1 term2 -excluded field:value "exact phrase"

        Note: By default, multiple unquoted terms use AND logic (all must match).
        Use quotes for exact phrase matching.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to search.

        Returns:
            The filtered queryset based on the search criteria.
        """
        import re

        search_query = request.DATA.get('search', None)
        if not search_query:
            return queryset

        # Trim whitespace
        search_query = search_query.strip()
        if not search_query:
            return queryset

        search_fields = getattr(cls.RestMeta, 'SEARCH_FIELDS', None)
        if search_fields is None:
            search_fields = [
                field.name for field in cls._meta.get_fields()
                if field.get_internal_type() in ["CharField", "TextField"]
            ]

        # Same oracle as the filter path, different door. With no SEARCH_FIELDS
        # declared the fallback above is EVERY CharField/TextField, which on
        # ApiKey means token_hash and mojo_secrets — so `?search=<prefix>` (and
        # the targeted `?search=token_hash:<prefix>` form) recovers a secret by
        # icontains. An explicitly declared SEARCH_FIELDS naming a sensitive
        # column is filtered too: SENSITIVE_FIELDS wins, mirroring how
        # AGGREGATION_FIELDS cannot widen past the sensitive gate.
        search_fields = [
            f for f in search_fields if not is_sensitive_filter_path(cls, f)
        ]
        if not search_fields:
            # Nothing left to match against. Returning `queryset` unchanged
            # would hand back the FULL list for a search that can match
            # nothing — the query below builds Q() per term, and `Q() & Q()` is
            # a no-op, so queryset.filter(Q()) returns everything. A search
            # that cannot match must return nothing.
            return queryset.none()

        # Parse search query to extract different term types
        # Pattern matches: quoted strings, field:value pairs, or words with optional - prefix
        pattern = r'"([^"]+)"|(-?)(\w+):(\S+)|(-?)(\S+)'
        matches = re.findall(pattern, search_query)

        search_terms = []  # Terms that must match (AND logic by default)
        excluded_terms = []  # Terms to exclude (NOT logic)
        field_searches = []  # Field-specific searches

        for match in matches:
            quoted, field_prefix, field_name, field_value, prefix, term = match

            if quoted:
                # Quoted phrase - always required (AND logic)
                search_terms.append(quoted)
            elif field_name and field_value:
                # Field-specific search (field:value)
                # Check if field exists in search_fields
                if field_name in search_fields:
                    is_excluded = field_prefix == '-'
                    field_searches.append((field_name, field_value, is_excluded))
            elif term:
                # Regular term with optional - prefix
                term = term.strip()
                if len(term) < 1:  # Skip empty terms
                    continue

                if prefix == '-':
                    excluded_terms.append(term)
                else:
                    # Default behavior: unquoted terms use AND logic
                    search_terms.append(term)

        # Build the query
        final_query = dm.Q()

        # Add search terms (AND logic - all must match)
        # Each term can match ANY of the search fields
        for term in search_terms:
            term_filter = dm.Q()
            for field in search_fields:
                term_filter |= dm.Q(**{f"{field}__icontains": term})
            final_query &= term_filter

        # Add field-specific searches
        for field_name, field_value, is_excluded in field_searches:
            field_filter = dm.Q(**{f"{field_name}__icontains": field_value})
            if is_excluded:
                final_query &= ~field_filter
            else:
                final_query &= field_filter

        # Add excluded terms (NOT logic)
        for term in excluded_terms:
            exclude_filter = dm.Q()
            for field in search_fields:
                exclude_filter |= dm.Q(**{f"{field}__icontains": term})
            final_query &= ~exclude_filter

        # Only apply filter if we have any search criteria
        if search_terms or excluded_terms or field_searches:
            # logger.info("search_filters", search_query, final_query)
            return queryset.filter(final_query)

        return queryset

    @classmethod
    def on_rest_list_sort(cls, request, queryset):
        """
        Apply sorting to the queryset.

        Nullable date/datetime fields always sort NULLs LAST regardless of
        direction or database backend. SQLite and MySQL otherwise place
        NULLs at the top of a descending sort (and the bottom of ascending),
        which is rarely what callers want when sorting an event log by
        ``-resolved_at`` or similar.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to sort.

        Returns:
            The sorted queryset.
        """
        if not hasattr(cls, '__rest_field_names__'):
            cls.__rest_field_names__ = [f.name for f in cls._meta.get_fields()]
        sort_field = request.DATA.pop("sort", "-id")
        bare = sort_field.lstrip('-')
        if bare not in cls.__rest_field_names__:
            return queryset
        # Ordering by a secret column is a weaker but real oracle — the caller
        # learns the relative order of values they cannot read. Ignored rather
        # than rejected, matching how this method already treats an unknown
        # sort field one line above.
        if is_sensitive_filter_path(cls, bare):
            return queryset
        descending = sort_field.startswith('-')
        field = cls.get_model_field(bare)
        if field is not None and getattr(field, "null", False) \
                and field.get_internal_type() in _DATE_FIELD_TYPES:
            expr = dm.F(bare)
            expr = expr.desc(nulls_last=True) if descending else expr.asc(nulls_last=True)
            return queryset.order_by(expr)
        return queryset.order_by(sort_field)

    @classmethod
    def return_rest_response(cls, data, flat=False):
        """
        Return the passed in data as a JSONResponse with root values of status=True and data=.

        Args:
            data: Data to include in the response.

        Returns:
            JsonResponse representing the data.
        """
        if flat:
            response_payload = data
        else:
            response_payload = {
                "status": True,
                "data": data
            }
        return JsonResponse(response_payload)

    def on_rest_created(self):
        """
        Handle the creation of an object.

        Args:
            request: Django HTTP request object.

        Returns:
            None
        """
        # Perform any additional actions after object creation
        pass

    def on_rest_pre_save(self, changed_fields, created):
        """
        Handle the pre-save of an object.

        Args:
            created: Boolean indicating whether the object is being created.
            changed_fields: Dictionary of fields that have changed.
        Returns:
            None
        """
        # Perform any additional actions before object save
        pass

    def on_rest_saved(self, changed_fields, created):
        """
        Handle the saving of an object.

        Args:
            created: Boolean indicating whether the object is being created.
            changed_fields: Dictionary of fields that have changed.
        Returns:
            None
        """
        # Perform any additional actions after object creation
        pass

    def on_rest_response(self, request, graph="default"):
        """
        Handle the response after a REST request.

        Args:
            request: Django HTTP request object.
            graph: String representing the graph to use for serialization.

        Returns:
            JsonResponse representing the object.
        """
        return self.on_rest_get(request, graph=graph)

    def on_rest_get(self, request, graph="default"):
        """
        Handle the retrieval of a single object.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the object.
        """
        graph = request.DATA.get("graph", graph)
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        serializer = manager.get_serializer(self, graph=graph)
        return serializer.to_response(request)

    def _set_field_change(self, key, old_value=None, new_value=None):
        if not hasattr(self, "__changed_fields__"):
            self.__changed_fields__ = objict.objict()
        if old_value != new_value:
            self.__changed_fields__[key] = old_value

    def has_field_changed(self, key):
        return key in self.__changed_fields__

    def has_changed(self):
        return bool(self.__changed_fields__)

    def get_changes(self, data):
        changes = {}
        for key, value in self.__changed_fields__.items():
            if "password" in key or "key" in key:
                changes[key] = "*** -> ***"
            else:
                changes[key] = f"{value} -> {data.get(key, None)}"
        return changes

    def on_rest_save(self, request, data_dict):
        """
        Create a model instance from a dictionary.

        Args:
            request: Django HTTP request object.
            data_dict: Dictionary containing the data to save.

        Returns:
            None
        """
        self.__changed_fields__ = objict.objict()
        # Get fields that should not be saved
        no_save_fields = self.get_rest_meta_prop("NO_SAVE_FIELDS", ["id", "pk", "created", "uuid"])
        post_save_actions = self.get_rest_meta_prop("POST_SAVE_ACTIONS", ['action'])
        post_save_data = {}
        action_resp = None  # an action may have a specific response
        actions_only = True  # track if request contains only post_save_actions (no real field data)
        # The field loop runs an FK-attach VIEW check per related field, and
        # _evaluate_permission re-binds request.group to the TARGET row's owning
        # tenant as a side effect — including to None when that row has no
        # tenant. The create-time auto-stamp below reads request.group, so
        # without this snapshot attaching a tenant-less FK would silently create
        # the new row with no group instead of the caller's. Same restore
        # on_rest_handle_batch already does between rows.
        _caller_group = getattr(request, "group", None)
        # Iterate a snapshot — perm checks (e.g. Group.check_view_permission)
        # legitimately mutate request.DATA mid-save (graph downgrade etc.), so a
        # live view trips CPython's dict-mutation guard.
        for key, value in list(data_dict.items()):
            # Skip fields that shouldn't be saved
            if key in no_save_fields:
                continue
            if key in post_save_actions:
                post_save_data[key] = value
                continue
            actions_only = False
            self.on_rest_save_field(key, value, request)
        request.group = _caller_group

        created = self.pk is None
        if not actions_only or created:
            if created:
                owner_field = self.get_rest_meta_prop("CREATED_BY_OWNER_FIELD", "user")
                if self.get_model_field(owner_field):
                    # Resolve the actor to stamp. request.acting_user is the
                    # member an ApiKey acts as (account.ApiKey.user) and wins
                    # when present, in BOTH key modes — attributing a key's
                    # writes to a real member is the point of the link, and it
                    # is independent of whether the key also assumes that
                    # member's authority.
                    #
                    # The actor resolution is load-bearing, not defensive: this
                    # used to stamp request.user unconditionally, so an api-key
                    # create of any model with a user FK assigned an ApiKey to
                    # it and Django raised
                    # ValueError: Cannot assign "<ApiKey>" ... must be a "User"
                    # instance — a 500. _resolve_stamp_actor returns None for
                    # any non-model identity — ApiKey, ANONYMOUS_USER, the
                    # system pseudo-user — leaving the field null, which is
                    # what the per-app shims did by hand (phonehub/rest/sms.py).
                    #
                    # Only auto-stamp when the body did not provide a value.
                    # Mirrors the "group" behavior directly below: body wins.
                    # Models that want strict self-ownership must opt out with
                    # CREATED_BY_OWNER_FIELD = None and re-stamp in on_rest_pre_save.
                    actor = _resolve_stamp_actor(request)
                    if actor is not None and getattr(self, owner_field, None) is None:
                        setattr(self, owner_field, actor)
                if request.group:
                    # Auto-assign the group-scoping FK on create when the body
                    # omitted it (body wins), mirroring the historical `group`
                    # behavior. Honors a RestMeta.GROUP_FIELD that names a
                    # DIRECT FK; a related path (e.g. "agent__project") has no
                    # local field to assign — its tenant is derived from the
                    # FK the body provides.
                    gf = self.get_rest_meta_prop("GROUP_FIELD", None)
                    if gf and "__" not in gf and self.get_model_field(gf):
                        if getattr(self, gf, None) is None:
                            setattr(self, gf, request.group)
                    elif self.get_model_field("group"):
                        if getattr(self, "group", None) is None:
                            self.group = request.group
            else:
                owner_field = self.get_rest_meta_prop("UPDATED_BY_OWNER_FIELD", "modified_by")
                if self.get_model_field(owner_field):
                    # Same resolution as the create branch above: prefer the
                    # member an ApiKey acts as, and never assign a non-User.
                    # Without the type guard an api-key UPDATE of any model with
                    # a modified_by FK raises the same ValueError the create
                    # branch used to — and reference-mode attribution, the
                    # whole point of ApiKey.user, would be missing on updates.
                    #
                    # No "already set" guard here, unlike create: "who last
                    # modified" is an actor fact, so it overwrites on every
                    # update. That is also why an unresolvable actor MUST leave
                    # the field alone rather than clear it.
                    actor = _resolve_stamp_actor(request)
                    if actor is not None:
                        setattr(self, owner_field, actor)
            self.on_rest_pre_save(self.__changed_fields__, created)
            if "files" in data_dict:
                self.on_rest_save_files(data_dict["files"])
            self.atomic_save()
            self.on_rest_saved(self.__changed_fields__, created)
        for key, value in post_save_data.items():
            # post save fields can only be called via on_action_
            handler = getattr(self, f'on_action_{key}', None)
            if callable(handler):
                action_resp = handler(value)

        if self.get_rest_meta_prop("LOG_CHANGES", False) and self.has_changed():
            self.log(kind="model:changed", log=self.get_changes(data_dict))
        return action_resp

    def on_rest_save_field(self, key, value, request):
        # First check for custom setter method
        set_field_method = getattr(self, f'set_{key}', None)
        if callable(set_field_method):
            if self.has_field(key):
                old_value = getattr(self, key, None)
                set_field_method(value)
                new_value = getattr(self, key, None)
                self._set_field_change(key, old_value, new_value)
            else:
                set_field_method(value)
            return

        # Check if this is a model field
        field = self.get_model_field(key)
        if field is None:
            return
        if field.is_relation:
            self.on_rest_save_related_field(field, value, request)
        elif field.get_internal_type() == "JSONField":
            self.on_rest_update_jsonfield(key, value, request)
        else:
            if field.get_internal_type() == "BooleanField":
                if isinstance(value, str):
                    value = value.lower() in ("true", "1")
                elif isinstance(value, int):
                    value = bool(value)
                else:
                    value = bool(value)
            elif value == "" and field.null:
                value = None
            elif value is not None:
                if field.get_internal_type() in ["DateTimeField", "DateField"]:
                    if not isinstance(value, (datetime.datetime, datetime.date)):
                        value = dates.parse_datetime(value)
                elif value == "" and field.get_internal_type() in ["IntegerField", "FloatField", "BigIntegerField"]:
                    value = 0
            self._set_field_change(key, getattr(self, key, None), value)
            setattr(self, key, value)

    def on_rest_save_files(self, files):
        for name, file in files.items():
            self.on_rest_save_file(name, file)

    def on_rest_save_file(self, name, file):
        # Implement file saving logic here
        # self.debug("Finding file for field: %s", name)
        field = self.get_model_field(name)
        if field is None:
            return
        # self.debug("Saving file for field: %s", name)
        if field.related_model and hasattr(field.related_model, "create_from_file"):
            # self.debug("Found file for field: %s", name)
            related_model = field.related_model
            instance = related_model.create_from_file(file, name)
            setattr(self, name, instance)

    def on_rest_save_and_respond(self, request):
        resp = self.on_rest_save(request, request.DATA)
        if resp is None:
            return self.on_rest_get(request)
        return JsonResponse(resp)

    @classmethod
    def _report_batch_row_denied(cls, request, instance, index, branch):
        """
        Emit a `batch_row_denied` incident when a batch row is dropped because
        the requester fails the per-row permission check in
        on_rest_handle_batch. Mirrors _report_fk_attach_denied: the check
        itself is the event-free boolean rest_check_permission, so the audit
        event is emitted here.
        """
        try:
            cls.class_report_incident_for_user(
                details=f"Batch row denied: index {index} on {cls.__name__}",
                event_type="batch_row_denied",
                level=2,
                request=request,
                branch=branch,
                index=index,
                instance_id=getattr(instance, "pk", None),
                model_name=cls.__name__,
                request_path=getattr(request, "path", None),
            )
        except Exception:
            # Audit reporting must never block a save flow.
            pass

    @classmethod
    def _report_batch_row_feature_disabled(cls, request, instance, index, branch):
        """
        Emit a `feature_disabled` incident when a batch row is dropped because
        the row's verb is hard-disabled by a RestMeta flag (CAN_UPDATE for
        update rows, CAN_CREATE for create rows) in on_rest_handle_batch. Uses
        the same event category as the single-instance feature gates so the
        block reads as policy, not a per-user permission shortfall; the branch
        (`batch_can_update_false`/`batch_can_create_false`) marks the batch
        origin.
        """
        try:
            cls.class_report_incident_for_user(
                details=f"Batch row feature-disabled: index {index} on {cls.__name__}",
                event_type="feature_disabled",
                level=2,
                request=request,
                branch=branch,
                index=index,
                instance_id=getattr(instance, "pk", None),
                model_name=cls.__name__,
                request_path=getattr(request, "path", None),
            )
        except Exception:
            # Audit reporting must never block a save flow.
            pass

    def _report_fk_attach_denied(self, field, related_instance, request, branch):
        """
        Emit an `fk_attach_denied` incident when an FK assignment is silently
        skipped because the requester lacks VIEW_PERMS on the related model.
        Replaces the previous side-effect reporting that lived inside
        rest_check_permission.
        """
        try:
            self.__class__.class_report_incident_for_user(
                details=f"FK attach denied: {field.name} on {self.__class__.__name__}",
                event_type="fk_attach_denied",
                level=2,
                request=request,
                branch=branch,
                field_name=field.name,
                related_model=field.related_model.__name__,
                related_id=getattr(related_instance, "id", None),
                model_name=self.__class__.__name__,
                request_path=getattr(request, "path", None),
            )
        except Exception:
            # Audit reporting must never block a save flow.
            pass

    def on_rest_save_related_field(self, field, field_value, request):
        if isinstance(field_value, dict):
            # we want to check if we have an existing field and if so we will update it after security
            related_instance = getattr(self, field.name)
            if related_instance is None:
                # skip None fields for now
                # FUTURE look at creating a new instance
                return
            if hasattr(field.related_model, "rest_check_permission"):
                if field.related_model.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], related_instance):
                    related_instance.on_rest_save(request, field_value)
                else:
                    self._report_fk_attach_denied(field, related_instance, request, branch="related_dict_save")
            return
        if hasattr(field.related_model, "on_rest_related_save"):
            related_instance = getattr(self, field.name)
            # Attach-by-id must clear the related model's VIEW_PERMS on the
            # target instance, exactly like the scalar-pk branch below. A custom
            # on_rest_related_save (e.g. fileman.File) otherwise attaches ANY
            # record by id with no permission check — a cross-user/cross-tenant
            # FK-attach hole. Gate on `isinstance(int)` ALONE, matching the exact
            # condition of the branch this guards (File.on_rest_related_save's
            # `elif isinstance(field_value, int)`): a `> 0` guard here would let
            # id 0 / False (bool is an int subclass, coerced to pk 0) skip the
            # gate yet still reach that ungated fetch-and-attach. String payloads
            # (base64 / data URLs) are an inline CREATE the caller implicitly
            # owns — not an "attach existing" — so they skip this gate.
            # NO_FK_VIEW_CHECK_FIELDS exempts a field just as for the scalar-pk
            # branch below.
            if isinstance(field_value, int):
                no_fk_check = self.get_rest_meta_prop("NO_FK_VIEW_CHECK_FIELDS", [])
                if field.name not in no_fk_check and hasattr(field.related_model, "rest_check_permission"):
                    target = field.related_model.objects.get(pk=field_value)
                    if not field.related_model.rest_check_permission(request, "VIEW_PERMS", target):
                        self._report_fk_attach_denied(field, target, request, branch="related_save_pk_assign")
                        return
            field.related_model.on_rest_related_save(self, field.name, field_value, related_instance)
        elif isinstance(field_value, int) or (isinstance(field_value, str)):
            # self.debug(f"Related Model: {field.related_model.__name__}, Field Value: {field_value}")
            # A blank string FK ("" or whitespace) means "not provided" — coerce
            # it to None instead of letting int("") raise ValueError. The falsy
            # check below was always meant to map "" → None (see its comment),
            # but the int() ran first and crashed before it could fire.
            if isinstance(field_value, str) and not field_value.strip():
                old_value = getattr(self, field.name, None)
                self._set_field_change(field.name, old_value, None)
                setattr(self, field.name, None)
                return
            field_value = int(field_value)
            if not bool(field_value):
                # None, 0 will set it to None
                # logger.info(f"Setting field {field.name} to None")
                old_value = getattr(self, field.name, None)
                self._set_field_change(field.name, old_value, None)
                setattr(self, field.name, None)
                return
            if field.related_model == type(self) and self.pk == field_value:
                self.debug("Skipping self-reference")
                return
            related_instance = field.related_model.objects.get(pk=field_value)
            # Gate the assignment by VIEW_PERMS on the related instance —
            # otherwise a user with SAVE_PERMS on this model could attach FKs
            # to records they cannot otherwise see. Mirrors the dict-value
            # branch's perm check above. Silent skip on denial, with an
            # explicit `fk_attach_denied` incident event so the audit trail
            # survives now that rest_check_permission is event-free.
            #
            # Models can exempt specific FK fields from this check by listing
            # them in RestMeta.NO_FK_VIEW_CHECK_FIELDS — use this when the
            # model's own on_rest_pre_save already enforces field-level access
            # (e.g. User.org is guarded by MANAGE_USERS_ONLY_FIELDS).
            no_fk_check = self.get_rest_meta_prop("NO_FK_VIEW_CHECK_FIELDS", [])
            if field.name not in no_fk_check and hasattr(field.related_model, "rest_check_permission"):
                if not field.related_model.rest_check_permission(
                    request, "VIEW_PERMS", related_instance,
                ):
                    self._report_fk_attach_denied(field, related_instance, request, branch="related_pk_assign")
                    return
            old_value = getattr(self, field.name, None)
            self._set_field_change(field.name, old_value, related_instance)
            setattr(self, field.name, related_instance)
        elif field_value is None:
            old_value = getattr(self, field.name, None)
            self._set_field_change(field.name, old_value, None)
            setattr(self, field.name, None)

    def on_rest_update_jsonfield(self, field_name, field_value, request=None):
        """Update a JSONField via merge or replace.

        By default, dicts are merged into the existing value (partial updates).
        Full replace happens when:
        - The field is listed in RestMeta.JSON_REPLACE_FIELDS, OR
        - The incoming dict contains "__replace": true (stripped before saving)

        The "protected" root key is guarded on every path — merge, replace, and
        non-dict overwrite — whether the incoming value carries it or an existing
        one would be clobbered.
        """
        # parse JSON strings into dicts/lists (e.g. form submissions send strings)
        if isinstance(field_value, str):
            try:
                field_value = json.loads(field_value)
            except (json.JSONDecodeError, ValueError):
                pass
        existing_value = getattr(self, field_name, {}) or {}
        if isinstance(field_value, dict) and isinstance(existing_value, dict):
            # Check for replace mode: RestMeta.JSON_REPLACE_FIELDS or __replace signal
            replace_fields = self.get_rest_meta_prop("JSON_REPLACE_FIELDS", [])
            should_replace = field_name in replace_fields or field_value.pop("__replace", False)

            # guard the "protected" root key — only superuser or users with PROTECTED_JSON_PERMS
            # can modify it; a merge touches it only when the incoming dict carries it, but a
            # replace also clobbers any existing protected subtree
            touches_protected = "protected" in field_value or (should_replace and "protected" in existing_value)
            if touches_protected:
                if not self._can_edit_protected_json(request):
                    raise me.PermissionDeniedException("Permission denied: cannot modify protected metadata")
                # always audit protected metadata changes regardless of LOG_CHANGES/LOG_META_CHANGES
                username = getattr(getattr(request, "user", None), "username", "unknown")
                incoming_prot = field_value.get("protected")
                existing_prot = existing_value.get("protected")
                if isinstance(incoming_prot, (dict, type(None))) and isinstance(existing_prot, (dict, type(None))):
                    # a replace drops unsent keys; merging an explicit null wipes the whole subtree
                    wipes_existing = should_replace or ("protected" in field_value and incoming_prot is None)
                    incoming_prot = incoming_prot or {}
                    existing_prot = existing_prot or {}
                    keys = set(incoming_prot) | (set(existing_prot) if wipes_existing else set())
                    changed_keys = sorted(k for k in keys if incoming_prot.get(k) != existing_prot.get(k))
                else:
                    changed_keys = ["*"]
                self.model_logit(request, f"{username} modified {field_name}.protected keys: {', '.join(changed_keys)} on pk={self.pk}", kind="meta:protected_changed")

            if should_replace:
                setattr(self, field_name, field_value)
            else:
                merged_value = objict.merge_dicts(existing_value, field_value)
                setattr(self, field_name, merged_value)
            # log all jsonfield key changes if enabled
            if self.get_rest_meta_prop("LOG_META_CHANGES", False):
                changed_keys = [k for k, v in field_value.items() if v != existing_value.get(k)]
                if changed_keys:
                    username = getattr(getattr(request, "user", None), "username", "unknown")
                    self.model_logit(request, f"{username} modified {field_name} keys: {', '.join(changed_keys)} on pk={self.pk}", kind="meta:changed")
        else:
            # wholesale overwrite with/of a non-dict can also create or clobber "protected"
            if (isinstance(existing_value, dict) and "protected" in existing_value) or \
                    (isinstance(field_value, dict) and "protected" in field_value):
                if not self._can_edit_protected_json(request):
                    raise me.PermissionDeniedException("Permission denied: cannot modify protected metadata")
                username = getattr(getattr(request, "user", None), "username", "unknown")
                self.model_logit(request, f"{username} modified {field_name}.protected keys: * on pk={self.pk}", kind="meta:protected_changed")
            setattr(self, field_name, field_value)

    def _can_edit_protected_json(self, request):
        """returns True if the request user can modify the 'protected' key in any JSONField"""
        if request is None:
            return False
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        # request.user may be an ApiKey (no is_superuser attribute)
        if getattr(user, "is_superuser", False):
            return True
        perms = self.get_rest_meta_prop("PROTECTED_JSON_PERMS", [])
        if perms:
            return user.has_permission(perms)
        return False

    def jsonfield_as_objict(self, field_name):
        existing_value = getattr(self, field_name, {})
        if not isinstance(existing_value, objict.objict):
            existing_value = objict.objict.fromdict(existing_value)
            setattr(self, field_name, existing_value)
        return existing_value

    def on_rest_pre_delete(self):
        """
        Handle the pre-deletion of an object.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the result of the pre-delete operation.
        """
        pass

    def on_rest_delete(self, request):
        """
        Handle the deletion of an object.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the result of the delete operation.
        """
        try:
            self.on_rest_pre_delete()
            with transaction.atomic():
                self.delete()
            return JsonResponse({"status": "deleted"}, status=200)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

    def to_dict(self, graph="default"):
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        return manager.serialize(self, graph=graph)

    @classmethod
    def to_csv(cls, queryset, format="csv", timezone=None):
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        format_key = format.split("_")[0]
        serializer = manager.get_format_serializer(format_key)
        formats = cls.get_rest_meta_prop("FORMATS")
        localize = None
        if formats is not None and format in formats:
            fields = formats[format]
        else:
            graph_obj = cls.get_rest_meta_graph(["basic", "default"])
            if not graph_obj or not graph_obj.get("fields"):
                raise me.ValueException("No valid graph found")
            fields = graph_obj.get("fields")
            # Get localize config from graph if available
            localize = graph_obj.get("localize")
        # Check if localize is defined in FORMATS_LOCALIZE
        formats_localize = cls.get_rest_meta_prop("FORMATS_LOCALIZE")
        if formats_localize and format in formats_localize:
            localize = formats_localize[format]
        return serializer.serialize_queryset(
            queryset,
            fields=fields,
            localize=localize,
            timezone=timezone,
            raw_data=True
        )

    @classmethod
    def queryset_to_dict(cls, qset, graph="default"):
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        return manager.serialize(qset, graph=graph, many=True)

    def save_now(self):
        self.save()
        transaction.commit()

    def atomic_save(self):
        return self.save_now()

    def report_incident(self, details, event_type="info", level=1, request=None, scope=None, **context):
        """
        Instance-level audit/event reporting. Automatically includes model+id
        and stamps ``group`` from ``self.group`` when the model exposes that
        relationship — instance group beats request group, matching the
        precedence documented in mojo/apps/incident/reporter.py.
        Caller-supplied ``group=`` (including explicit ``None``) wins.
        """
        context = dict(context)
        context.setdefault("model_name", self.__class__.get_model_string())
        if hasattr(self, 'id') and self.id is not None:
            context.setdefault("model_id", self.id)
        if "group" not in context:
            instance_group = getattr(self, "group", None)
            try:
                from mojo.apps.account.models import Group
                if isinstance(instance_group, Group):
                    context["group"] = instance_group
            except Exception:
                pass
        self.__class__.class_report_incident(
            details, event_type=event_type, level=level, request=request, scope=scope, **context
        )

    @classmethod
    def class_report_incident_for_user(cls, details, event_type="info", level=1, request=None, scope=None, **context):
        """
        Class-level audit/event reporting for a specific user.

        Auto-stamps ``group`` from ``request.group`` when the request
        carries one and the caller did not supply ``group=`` explicitly.
        Caller-supplied ``group=`` (including ``None``) always wins.

        details: Human description.
        event_type: Category/kind (e.g. "permission_denied", "security_alert").
        level: Numeric severity.
        request: Optional HTTP request or actor.
        **context: Any additional context.
        """
        if request is None:
            request = ACTIVE_REQUEST.get()
        if "group" not in context and request is not None:
            req_group = getattr(request, "group", None)
            try:
                from mojo.apps.account.models import Group
                if isinstance(req_group, Group):
                    context["group"] = req_group
            except Exception:
                pass
        if request and request.user.is_authenticated:
            return request.user.report_incident(details, event_type=event_type, level=level, request=request, scope=scope, **context)
        return cls.class_report_incident(details, event_type=event_type, level=level, request=request, scope=scope, **context)

    @classmethod
    def class_report_incident(cls, details, event_type="info", level=1, request=None, scope=None, **context):
        """
        Class-level audit/event reporting. Auto-stamps ``group`` from
        ``request.group`` when the request carries one and the caller
        did not supply ``group=`` explicitly.

        details: Human description.
        event_type: Category/kind (e.g. "permission_denied", "security_alert").
        level: Numeric severity.
        request: Optional HTTP request or actor.
        **context: Any additional context.
        """
        from mojo.apps import incident
        context = dict(context)
        context.setdefault("model_name", cls.__name__)
        if request is None:
            request = ACTIVE_REQUEST.get()
        if "group" not in context and request is not None:
            req_group = getattr(request, "group", None)
            try:
                from mojo.apps.account.models import Group
                if isinstance(req_group, Group):
                    context["group"] = req_group
            except Exception:
                pass
        if scope is None:
            if hasattr(cls, "_meta"):
                scope = cls._meta.app_config.label
            else:
                scope = "global"
        incident.report_event(
            details,
            title=details[:80],
            category=event_type,
            scope=scope,
            level=level,
            request=request,
            **context
        )

    def log(self, log="", kind="model_log", level="info", **kwargs):
        if "gid" not in kwargs:
            group_id = getattr(self, "group_id", None)
            if group_id:
                kwargs["gid"] = group_id
        return self.class_logit(ACTIVE_REQUEST.get(), log, kind, self.id, level, **kwargs)

    def model_logit(self, request, log, kind="model_log", level="info", **kwargs):
        return self.class_logit(request, log, kind, self.id, level, **kwargs)

    @classmethod
    def debug(cls, log, *args):
        return logger.info(log, *args)

    @classmethod
    def get_model_string(cls):
        return f"{ cls._meta.app_label.lower()}.{cls.__name__}"

    @classmethod
    def class_logit(cls, request, log, kind="cls_log", model_id=0, level="info", **kwargs):
        from mojo.apps.logit.models import Log
        return Log.logit(request, log, kind, cls.get_model_string(), model_id, level, **kwargs)

    @classmethod
    def get_model_field(cls, field_name):
        """
        Get a model field by name.
        """
        try:
            return cls._meta.get_field(field_name)
        except Exception:
            return None

    @classmethod
    def has_field(cls, field_name):
        return cls.get_model_field(field_name) is not None

    @classmethod
    def _instance_group(cls, instance):
        """Resolve the owning Group for an instance, GROUP_FIELD or direct FK.

        The single answer to "which tenant does this row belong to?". Both seams
        in `_evaluate_permission` — the ApiKey pre-gate and the `request.group`
        re-bind — go through here so they cannot drift apart.

        Returns None when the row has no owning tenant, which is a real answer
        and not an absence of one: callers MUST treat None as "no group" and
        fall through to the flat user/superuser check. They must NOT skip the
        re-bind and leave a caller-supplied ?group= standing — that was the
        GROUP_FIELD branch's bug (maestro item 953), and it let a GroupMember
        grant in an unrelated tenant authorize a read of a null-tenant row.
        """
        group_field = cls.get_rest_meta_prop("GROUP_FIELD", None)
        if group_field:
            return cls._resolve_group_from_instance(instance, group_field)
        return getattr(instance, "group", None)

    @classmethod
    def _resolve_group_from_instance(cls, instance, group_field):
        """Resolve the owning Group for an instance given a GROUP_FIELD name.

        ``group_field`` may be a Django related path (e.g. "original_file__group"
        or "agent__project"); each hop is traversed with getattr, returning None
        if any link along the path is null. A plain field name (e.g. "group" or
        "project") is a single getattr. Used by the permission layer to scope
        detail access to the row's true tenant regardless of the FK's name.
        """
        if not instance or not group_field:
            return None
        obj = instance
        for part in group_field.split("__"):
            if obj is None:
                return None
            obj = getattr(obj, part, None)
        return obj
