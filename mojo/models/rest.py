# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.middleware.mojo import ANONYMOUS_USER
from mojo.serializers import get_serializer_manager
from mojo.helpers.settings import settings
from mojo import errors as me
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction, models as dm
import objict
import datetime
from mojo.helpers import dates, logit
from contextvars import ContextVar


logger = logit.get_logger("debug", "debug.log")
ACTIVE_REQUEST = ContextVar("ACTIVE_REQUEST", default=None)
LOGGING_CLASS = None
MOJO_APP_STATUS_200_ON_ERROR = settings.MOJO_APP_STATUS_200_ON_ERROR
MOJO_REST_LIST_PERM_DENY = settings.get("MOJO_REST_LIST_PERM_DENY", True)

# use this when there is no ACTIVE_REQUEST
SYSTEM_REQUEST = objict.objict()
SYSTEM_REQUEST.user = objict.objict()
SYSTEM_REQUEST.user.id = 1
SYSTEM_REQUEST.user.display_name = "System"
SYSTEM_REQUEST.user.username = "system"
SYSTEM_REQUEST.user.email = ""
SYSTEM_REQUEST.user.is_authenticated = True
SYSTEM_REQUEST.user.has_permission = lambda perm: True
SYSTEM_REQUEST.DATA = objict.objict()


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

            elif request.method in ['POST', 'PUT']:
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
            The requested instance or a JsonResponse for a 404 error.
        """
        try:
            return cls.objects.get(pk=pk)
        except ObjectDoesNotExist:
            return cls.rest_error_response(None, 404, error=f"{cls.__name__} not found")

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
    def rest_check_permission(cls, request, permission_keys, instance=None):
        """
        Check permissions for a given request. Reports granular denied feedback to incident/event system.

        Args:
            request: Django HTTP request object.
            permission_keys: Keys to check for permissions.
            instance: Optional instance to check instance-specific permissions.

        Returns:
            True if the request has the necessary permissions, otherwise False.
        """
        perms = cls.get_rest_meta_prop(permission_keys, [])
        if perms is None or len(perms) == 0:
            return True

        if "all" not in perms:
            if request.user is None or not request.user.is_authenticated:
                cls.class_report_incident(
                    details="Permission denied: unauthenticated user",
                    event_type="unauthenticated",
                    request=request,
                    perms=perms,
                    permission_keys=permission_keys,
                    branch="unauthenticated",
                    instance=repr(instance) if instance else None,
                    request_path=getattr(request, "path", None),
                )
                return False

        if instance is not None:
            is_view = isinstance(permission_keys, list) and "VIEW_PERMS" in permission_keys
            if not is_view and isinstance(permission_keys, str):
                is_view = permission_keys == "VIEW_PERMS"
            if is_view:
                if hasattr(instance, "check_view_permission"):
                    allowed = instance.check_view_permission(perms, request)
                    if not allowed:
                        cls.class_report_incident(
                            details="Permission denied: view_permission_denied",
                            event_type="view_permission_denied",
                            request=request,
                            perms=perms,
                            permission_keys=permission_keys,
                            branch="instance.check_view_permission",
                            instance=repr(instance),
                            request_path=getattr(request, "path", None),
                        )
                    return allowed

            if hasattr(instance, "check_edit_permission"):
                allowed = instance.check_edit_permission(perms, request)
                if not allowed:
                    cls.class_report_incident(
                        details="Permission denied: edit_permission_denied",
                        event_type="edit_permission_denied",
                        request=request,
                        perms=perms,
                        permission_keys=permission_keys,
                        branch="instance.check_edit_permission",
                        instance=repr(instance),
                        request_path=getattr(request, "path", None),
                    )
                return allowed

            if "owner" in perms:
                owner_field = instance.get_rest_meta_prop("OWNER_FIELD", "user")
                owner = getattr(instance, owner_field, None)
                if owner is not None and owner.id == request.user.id:
                    return True
            if hasattr(instance, "group"):
                request.group = getattr(instance, "group", None)

        if request.group and hasattr(cls, "group"):
            allowed = request.group.user_has_permission(request.user, perms)
            if not allowed:
                cls.class_report_incident(
                    details="Permission denied: group_member_permission_denied",
                    event_type="group_member_permission_denied",
                    request=request,
                    perms=perms,
                    permission_keys=permission_keys,
                    group=getattr(request, "group", None),
                    branch="group.user_has_permission",
                    instance=repr(instance) if instance else None,
                    request_path=getattr(request, "path", None),
                )
            return allowed
        if request.user is None or not request.user.is_authenticated:
            return False
        allowed = request.user.has_permission(perms)
        if not allowed:
            cls.class_report_incident(
                details="Permission denied: user_permission_denied",
                event_type="user_permission_denied",
                request=request,
                perms=perms,
                permission_keys=permission_keys,
                branch="user.has_permission",
                instance=repr(instance) if instance else None,
                request_path=getattr(request, "path", None),
            )
        return allowed

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
        if cls.rest_check_permission(request, "VIEW_PERMS", instance):
            return instance.on_rest_get(request)
        return cls.rest_error_response(request, 403, error=f"GET permission denied: {cls.__name__}")

    @classmethod
    def on_rest_handle_save(cls, request, instance):
        """
        Handle POST and PUT requests with permission checks.

        Args:
            request: Django HTTP request object.
            instance: The instance to save or update.

        Returns:
            JsonResponse representing the result of the save operation.
        """
        if cls.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], instance):
            return instance.on_rest_save_and_respond(request)
        return cls.rest_error_response(request, 403, error=f"{request.method} permission denied: {cls.__name__}")

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
            return cls.rest_error_response(request, 403, error=f"DELETE not allowed: {cls.__name__}")

        if cls.rest_check_permission(request, ["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"], instance):
            return instance.on_rest_delete(request)
        return cls.rest_error_response(request, 403, error=f"DELETE permission denied: {cls.__name__}")

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

        # Check for owner permission
        if perms and "owner" in perms and request.user.is_authenticated:
            owner_field = cls.get_rest_meta_prop("OWNER_FIELD", "user")
            if owner_field == "self":
                q = {"pk": request.user.pk}
            else:
                q = {owner_field: request.user}
            return cls.on_rest_list(request, cls.objects.filter(**q))

        # Check if model has a group field and user might have group-level permissions
        if request.user.is_authenticated and hasattr(cls, "group"):
            # User doesn't have system-level permissions, but might have group-level permissions
            groups_with_perms = request.user.get_groups_with_permission(perms)
            if groups_with_perms.exists():
                # Filter queryset to only include objects from groups where user has permission
                group_field = cls.get_rest_meta_prop("GROUP_FIELD", "group")
                q = {f"{group_field}__in": groups_with_perms}
                return cls.on_rest_list(request, cls.objects.filter(**q))

        if MOJO_REST_LIST_PERM_DENY:
            return cls.rest_error_response(request, 403, error=f"GET permission denied: {cls.__name__}")
        return cls.on_rest_list_response(request, cls.objects.none())

    def update_from_dict(self, dict_data):
        request = ACTIVE_REQUEST.get() or SYSTEM_REQUEST
        return self.on_rest_save(request, dict_data)

    @classmethod
    def create_from_dict(cls, dict_data, **kwargs):
        request = kwargs.pop('request', ACTIVE_REQUEST.get() or SYSTEM_REQUEST)
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
        if cls.rest_check_permission(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"]):
            instance = cls.create_from_request(request)
            return instance.on_rest_get(request)
        return cls.rest_error_response(request, 403, error=f"CREATE permission denied: {cls.__name__}")

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
        elif request.method in ['POST', 'PUT']:
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

        Returns a JSON response with serialized results and optional errors.
        """
        if not cls.get_rest_meta_prop("CAN_BATCH", False):
            return cls.rest_error_response(request, 403, error=f"BATCH not allowed: {cls.__name__}")

        if not cls.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"]):
            return cls.rest_error_response(request, 403, error=f"BATCH permission denied: {cls.__name__}")

        batched = request.DATA.get("batched")
        if not isinstance(batched, list):
            return cls.rest_error_response(request, 400, error="Invalid 'batched' payload: expected a list")

        results = []
        errors = []
        for idx, item in enumerate(batched):
            try:
                if not isinstance(item, dict):
                    raise ValueError("Batch item must be an object")
                pk = item.get("id") or item.get("pk")
                if pk:
                    instance = cls.objects.filter(pk=pk).first()
                    if instance:
                        instance.update_from_dict(item)
                    else:
                        instance = cls.create_from_dict(item, request=request)
                else:
                    instance = cls.create_from_dict(item, request=request)
                results.append(instance)
            except Exception as e:
                errors.append({"index": idx, "error": str(e)})

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

            logger.info(f"Serializing queryset with fields: {fields}")
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
    def on_rest_list_date_range_filter(cls, request, queryset):
        """
        Filter queryset based on a date range provided in the request.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to filter.

        Returns:
            The filtered queryset.
        """
        dr_field = request.DATA.get("dr_field", "created")
        dr_start = request.DATA.get("dr_start")
        dr_end = request.DATA.get("dr_end")

        if dr_start:
            dr_start = dates.parse_datetime(dr_start)
            if request.group:
                dr_start = request.group.get_local_time(dr_start)
            queryset = queryset.filter(**{f"{dr_field}__gte": dr_start})

        if dr_end:
            dr_end = dates.parse_datetime(dr_end)
            if request.group:
                dr_end = request.group.get_local_time(dr_end)
            queryset = queryset.filter(**{f"{dr_field}__lte": dr_end})
        return queryset

    @classmethod
    def normalize_rest_value(cls, request, field_name, value):
        field = cls.get_model_field(field_name)
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
    def on_rest_list_filter(cls, request, queryset):
        """
        Apply filtering logic based on request parameters, including foreign key fields.

        Supports both inclusion and exclusion filters:
        - Regular filters: ?category=ossec (include)
        - __in filters: ?category__in=ossec,system (include multiple)
        - __not filters: ?category__not=ossec (exclude single value)
        - __not_in filters: ?category__not_in=ossec,system (exclude multiple)
        - __isnull filters: ?category__isnull=true (NULL check)

        Args:
            request: Django HTTP request object.
            queryset: The queryset to filter.

        Returns:
            The filtered queryset.
        """
        reserved_keys = ["start", "size", "download_format", "dr_start", "dr_end", "limit", "offset"]
        filters = {}
        excludes = {}

        for key, value in request.QUERY_PARAMS.items():
            # Split key to check for foreign key relationships
            if "." in key:
                key = key.replace(".", "__")
            key_parts = key.split('__')
            field_name = key_parts[0]
            if field_name in reserved_keys:
                continue

            # Determine if this is an exclusion filter
            is_exclusion = key.endswith("__not") or key.endswith("__not_in")
            target_dict = excludes if is_exclusion else filters

            if field_name in cls.__rest_field_names__ and cls._meta.get_field(field_name).is_relation:
                # TODO Normalize relation field values
                if key.endswith("__in"):
                    value = value.split(",")
                elif key.endswith("__not_in"):
                    # Convert __not_in to __in for exclude()
                    key = key.replace("__not_in", "__in")
                    value = value.split(",")
                elif key.endswith("__not"):
                    # Remove __not suffix for exclude()
                    key = key.replace("__not", "")
                elif key.endswith("__isnull"):
                    value = value.lower() == "true"
                elif value == "null":
                    value = None
                target_dict[key] = value
            elif hasattr(cls, field_name):
                if key.endswith("__in"):
                    value = value.split(",")
                elif key.endswith("__not_in"):
                    # Convert __not_in to __in for exclude()
                    key = key.replace("__not_in", "__in")
                    value = value.split(",")
                elif key.endswith("__not"):
                    # Remove __not suffix for exclude()
                    key = key.replace("__not", "")
                elif key.endswith("__isnull"):
                    value = value.lower() == "true"
                elif value == "null":
                    value = None
                target_dict[key] = cls.normalize_rest_value(request, field_name, value)

        logger.info("filters", filters)
        logger.info("excludes", excludes)

        queryset = cls.on_rest_list_search(request, queryset)
        queryset = queryset.filter(**filters)

        if excludes:
            queryset = queryset.exclude(**excludes)

        return queryset

    @classmethod
    def on_rest_list_search(cls, request, queryset):
        """
        Search queryset based on 'search' param in the request for fields defined in 'SEARCH_FIELDS'.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to search.

        Returns:
            The filtered queryset based on the search criteria.
        """
        search_query = request.DATA.get('search', None)
        if not search_query:
            return queryset

        search_fields = getattr(cls.RestMeta, 'SEARCH_FIELDS', None)
        if search_fields is None:
            search_fields = [
                field.name for field in cls._meta.get_fields()
                if field.get_internal_type() in ["CharField", "TextField"]
            ]

        query_filters = dm.Q()
        for field in search_fields:
            query_filters |= dm.Q(**{f"{field}__icontains": search_query})

        logger.info("search_filters", query_filters)
        return queryset.filter(query_filters)

    @classmethod
    def on_rest_list_sort(cls, request, queryset):
        """
        Apply sorting to the queryset.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to sort.

        Returns:
            The sorted queryset.
        """
        sort_field = request.DATA.pop("sort", "-id")
        if sort_field.lstrip('-') in cls.__rest_field_names__:
            return queryset.order_by(sort_field)
        return queryset

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
        # Iterate through data_dict keys instead of model fields
        for key, value in data_dict.items():
            # Skip fields that shouldn't be saved
            if key in no_save_fields:
                continue
            if key in post_save_actions:
                post_save_data[key] = value
                continue
            self.on_rest_save_field(key, value, request)

        created = self.pk is None
        if created:
            owner_field = self.get_rest_meta_prop("CREATED_BY_OWNER_FIELD", "user")
            if request.user.is_authenticated and self.get_model_field(owner_field):
                setattr(self, owner_field, request.user)
            if request.group and self.get_model_field("group"):
                if getattr(self, "group", None) is None:
                    self.group = request.group
        else:
            owner_field = self.get_rest_meta_prop("UPDATED_BY_OWNER_FIELD", "modified_by")
            if request.user.is_authenticated and self.get_model_field(owner_field):
                setattr(self, owner_field, request.user)
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
        if field.get_internal_type() == "ForeignKey":
            self.on_rest_save_related_field(field, value, request)
        elif field.get_internal_type() == "JSONField":
            self.on_rest_update_jsonfield(key, value)
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
            self._set_field_change(key, getattr(self, key), value)
            setattr(self, key, value)

    def on_rest_save_files(self, files):
        for name, file in files.items():
            self.on_rest_save_file(name, file)

    def on_rest_save_file(self, name, file):
        # Implement file saving logic here
        self.debug("Finding file for field: %s", name)
        field = self.get_model_field(name)
        if field is None:
            return
        self.debug("Saving file for field: %s", name)
        if field.related_model and hasattr(field.related_model, "create_from_file"):
            self.debug("Found file for field: %s", name)
            related_model = field.related_model
            instance = related_model.create_from_file(file, name)
            setattr(self, name, instance)

    def on_rest_save_and_respond(self, request):
        resp = self.on_rest_save(request, request.DATA)
        if resp is None:
            return self.on_rest_get(request)
        return JsonResponse(resp)

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
            return
        if hasattr(field.related_model, "on_rest_related_save"):
            related_instance = getattr(self, field.name)
            field.related_model.on_rest_related_save(self, field.name, field_value, related_instance)
        elif isinstance(field_value, int) or (isinstance(field_value, str)):
            # self.debug(f"Related Model: {field.related_model.__name__}, Field Value: {field_value}")
            field_value = int(field_value)
            if not bool(field_value):
                # None, "", 0 will set it to None
                logger.info(f"Setting field {field.name} to None")
                setattr(self, field.name, None)
                return
            field_value = int(field_value)
            if (self.pk == field_value):
                self.debug("Skipping self-reference")
                return
            related_instance = field.related_model.objects.get(pk=field_value)
            setattr(self, field.name, related_instance)

    def on_rest_update_jsonfield(self, field_name, field_value):
        """helper to update jsonfield by merge in changes"""
        existing_value = getattr(self, field_name, {})
        # logger.info("JSONField", existing_value, "New Value", field_value)
        if isinstance(field_value, dict) and isinstance(existing_value, dict):
            merged_value = objict.merge_dicts(existing_value, field_value)
            setattr(self, field_name, merged_value)

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
    def queryset_to_dict(cls, qset, graph="default"):
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        return manager.serialize(qset, graph=graph, many=True)

    def save_now(self):
        self.save()
        transaction.commit()

    def atomic_save(self):
        return self.save_now()

    def report_incident(self, details, event_type="info", level=1, request=None, **context):
        """
        Instance-level audit/event reporting. Automatically includes model+id.
        """
        context = dict(context)
        context.setdefault("model_name", self.__class__.get_model_string())
        if hasattr(self, 'id') and self.id is not None:
            context.setdefault("model_id", self.id)
        self.__class__.class_report_incident(
            details, event_type=event_type, level=level, request=request, **context
        )

    @classmethod
    def class_report_incident_for_user(cls, details, event_type="info", level=1, request=None, **context):
        """
        Class-level audit/event reporting for a specific user.
        details: Human description.
        event_type: Category/kind (e.g. "permission_denied", "security_alert").
        level: Numeric severity.
        request: Optional HTTP request or actor.
        **context: Any additional context.
        """
        if request is None:
            request = ACTIVE_REQUEST.get()
        if request and request.user.is_authenticated:
            return request.user.report_incident(details, event_type=event_type, level=level, request=request, **context)
        return cls.class_report_incident(details, event_type=event_type, level=level, request=request, **context)

    @classmethod
    def class_report_incident(cls, details, event_type="info", level=1, request=None, **context):
        """
        Class-level audit/event reporting.
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
        incident.report_event(
            details,
            title=details[:80],
            category=event_type,
            level=level,
            request=request,
            **context
        )

    def log(self, log="", kind="model_log", level="info", **kwargs):
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
