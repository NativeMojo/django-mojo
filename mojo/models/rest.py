# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.serializers.simple import GraphSerializer
from mojo.serializers.manager import get_serializer_manager
from mojo.helpers import modules
from mojo.helpers.settings import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction, models as dm
import objict
from mojo.helpers import dates, logit


logger = logit.get_logger("debug", "debug.log")
ACTIVE_REQUEST = None
LOGGING_CLASS = None
MOJO_APP_STATUS_200_ON_ERROR = settings.MOJO_APP_STATUS_200_ON_ERROR

class MojoModel:
    """Base model class for REST operations with GraphSerializer integration."""

    @property
    def active_request(self):
        """Returns the active request being processed."""
        return ACTIVE_REQUEST

    @property
    def active_user(self):
        """Returns the active user being processed."""
        if ACTIVE_REQUEST:
            return ACTIVE_REQUEST.user
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
            if "owner" in perms and getattr(instance, "user", None) is not None:
                if instance.user.id == request.user.id:
                    return True

        if request.group and hasattr(cls, "group"):
            allowed = request.group.member_has_permission(request.user, perms)
            if not allowed:
                cls.class_report_incident(
                    details="Permission denied: group_member_permission_denied",
                    event_type="group_member_permission_denied",
                    request=request,
                    perms=perms,
                    permission_keys=permission_keys,
                    group=getattr(request, "group", None),
                    branch="group.member_has_permission",
                    instance=repr(instance) if instance else None,
                    request_path=getattr(request, "path", None),
                )
            return allowed

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
        cls.debug("on_rest_handle_list")
        if cls.rest_check_permission(request, "VIEW_PERMS"):
            return cls.on_rest_list(request)
        return cls.rest_error_response(request, 403, error=f"GET permission denied: {cls.__name__}")

    @classmethod
    def on_rest_handle_create(cls, request):
        """
        Handle POST and PUT requests for creating resources with permission checks.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the result of the create operation.
        """
        if cls.rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"]):
            instance = cls()
            instance.on_rest_save(request, request.DATA)
            instance.on_rest_created()
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
            return cls.on_rest_handle_create(request)

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
        cls.debug("on_rest_list:start")
        if queryset is None:
            queryset = cls.objects.all()
        if request.group is not None and hasattr(cls, "group"):
            if "group" in request.DATA:
                del request.DATA["group"]
            queryset = queryset.filter(group=request.group)
        queryset = cls.on_rest_list_filter(request, queryset)
        queryset = cls.on_rest_list_date_range_filter(request, queryset)
        queryset = cls.on_rest_list_sort(request, queryset)
        cls.debug("on_rest_list:end")
        return cls.on_rest_list_response(request, queryset)

    @classmethod
    def on_rest_list_response(cls, request, queryset):
        # Implement pagination
        page_size = request.DATA.get_typed("size", 10, int)
        page_start = request.DATA.get_typed("start", 0, int)
        page_end = page_start+page_size
        paged_queryset = queryset[page_start:page_end]
        graph = request.DATA.get("graph", "list")
        # Use serializer manager for optimal performance
        manager = get_serializer_manager()
        serializer = manager.get_serializer(paged_queryset, graph=graph, many=True)
        return serializer.to_response(request, count=queryset.count(), start=page_start, size=page_size)

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
    def on_rest_list_filter(cls, request, queryset):
        """
        Apply filtering logic based on request parameters, including foreign key fields.

        Args:
            request: Django HTTP request object.
            queryset: The queryset to filter.

        Returns:
            The filtered queryset.
        """
        filters = {}
        for key, value in request.GET.items():
            # Split key to check for foreign key relationships
            key_parts = key.split('__')
            field_name = key_parts[0]
            if hasattr(cls, field_name):
                filters[key] = value
            elif field_name in cls.__rest_field_names__ and cls._meta.get_field(field_name).is_relation:
                filters[key] = value
        # logger.info("filters", filters)
        queryset = cls.on_rest_list_search(request, queryset)
        return queryset.filter(**filters)

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
        search_query = request.GET.get('search', None)
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

    def on_rest_get(self, request, graph="default"):
        """
        Handle the retrieval of a single object.

        Args:
            request: Django HTTP request object.

        Returns:
            JsonResponse representing the object.
        """
        graph = request.GET.get("graph", graph)
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

        # Iterate through data_dict keys instead of model fields
        for key, value in data_dict.items():
            # Skip fields that shouldn't be saved
            if key in no_save_fields:
                continue

            # First check for custom setter method
            set_field_method = getattr(self, f'set_{key}', None)
            if callable(set_field_method):
                old_value = getattr(self, key, None)
                set_field_method(value)
                new_value = getattr(self, key, None)
                self._set_field_change(key, old_value, new_value)
                continue

            # Check if this is a model field
            field = self.get_model_field(key)
            if field is None:
                continue
            if field.get_internal_type() == "ForeignKey":
                self.on_rest_save_related_field(field, value, request)
            elif field.get_internal_type() == "JSONField":
                self.on_rest_update_jsonfield(key, value)
            else:
                self._set_field_change(key, getattr(self, key), value)
                setattr(self, key, value)

        created = self.pk is None
        if created:
            if request.user.is_authenticated and self.get_model_field("user"):
                if getattr(self, "user", None) is None:
                    self.user = request.user
            if request.group and self.get_model_field("group"):
                if getattr(self, "group", None) is None:
                    self.group = request.group
        self.on_rest_pre_save(self.__changed_fields__, created)
        if "files" in data_dict:
            self.on_rest_save_files(data_dict["files"])
        self.atomic_save()
        self.on_rest_saved(self.__changed_fields__, created)

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
        self.on_rest_save(request, request.DATA)
        return self.on_rest_get(request)

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
            if not bool(field_value):
                # None, "", 0 will set it to None
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

    def atomic_save(self):
        """
        Save the object atomically to the database.
        """
        with transaction.atomic():
            self.save()

    def report_incident(self, details, event_type="info", level=1, request=None, **context):
        """
        Instance-level audit/event reporting. Automatically includes model+id.
        """
        context = dict(context)
        context.setdefault("model_name", self.__class__.__name__)
        if hasattr(self, 'id'):
            context.setdefault("model_id", self.id)
        self.__class__.class_report_incident(
            details, event_type=event_type, level=level, request=request, **context
        )

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
        incident.report_event(
            details,
            title=details[:80],
            category=event_type,
            level=level,
            request=request,
            **context
        )

    def log(self, log="", kind="model_log", level="info", **kwargs):
        return self.class_logit(ACTIVE_REQUEST, log, kind, self.id, level, **kwargs)

    def model_logit(self, request, log, kind="model_log", level="info", **kwargs):
        return self.class_logit(request, log, kind, self.id, level, **kwargs)

    @classmethod
    def debug(cls, log, *args):
        return logger.info(log, *args)

    @classmethod
    def class_logit(cls, request, log, kind="cls_log", model_id=0, level="info", **kwargs):
        from mojo.apps.logit.models import Log
        return Log.logit(request, log, kind, cls.__name__, model_id, level, **kwargs)

    @classmethod
    def get_model_field(cls, field_name):
        """
        Get a model field by name.
        """
        try:
            return cls._meta.get_field(field_name)
        except Exception:
            return None
