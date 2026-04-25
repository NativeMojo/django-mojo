class MojoException(Exception):
    """
    Base exception class for Mojo-related errors.

    Attributes:
        reason (str): The reason for the exception.
        code (int): The error code associated with the exception.
        status (int, optional): The HTTP status code. Defaults to None.
    """

    def __init__(self, reason, code, status=500):
        """
        Initialize a MojoException instance.

        Args:
            reason (str): The reason for the exception.
            code (int): The error code associated with the exception.
            status (int, optional): The HTTP status code. Defaults to None.
        """
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.status = status


class ValueException(MojoException):
    """
    Exception raised for REST API value errors.

    Attributes:
        reason (str): The reason for the exception. Defaults to 'REST API Error'.
        code (int): The error code associated with the exception. Defaults to 500.
        status (int, optional): The HTTP status code. Defaults to 500.
    """

    def __init__(self, reason='REST API Error', code=400, status=400):
        """
        Initialize a RestErrorException instance.

        Args:
            reason (str, optional): The reason for the exception. Defaults to 'REST API Error'.
            code (int, optional): The error code associated with the exception. Defaults to 500.
            status (int, optional): The HTTP status code. Defaults to 500.
        """
        super().__init__(reason, code, status)


class PermissionDeniedException(MojoException):
    """
    Exception raised for permission denied errors.

    Carries structured metadata that the REST dispatcher in
    mojo/decorators/http.py reads when emitting the security incident, so
    operators can filter and bundle on the actual denial cause instead of
    parsing free-form messages. Default-only construction stays compatible
    with existing call sites.

    Attributes:
        reason (str): Human-readable reason. Defaults to 'Permission Denied'.
        code (int): Error code (mirrored into the response body). Defaults to 403.
        status (int): HTTP status code. Defaults to 403; pass 401 for unauth.
        branch (str|None): Predicate branch that produced the denial
            (e.g. "user.has_permission", "instance.check_view_permission",
            "group.user_has_permission", "list_perm_deny"). Used as event metadata.
        perms (list|None): The list of permission strings that were checked.
        permission_keys (str|list|None): The RestMeta key(s) the perms came from
            (e.g. "VIEW_PERMS" or ["SAVE_PERMS", "VIEW_PERMS"]).
        model_name (str|None): The model class name being accessed.
        instance (str|None): repr() of the instance, when an instance check failed.
        event_type (str): Incident category. Defaults to 'user_permission_denied'.
            Other values: 'unauthenticated', 'view_permission_denied',
            'edit_permission_denied', 'group_member_permission_denied',
            'feature_disabled'.
    """

    def __init__(self, reason='Permission Denied', code=403, status=403, *,
                 branch=None, perms=None, permission_keys=None,
                 model_name=None, instance=None,
                 event_type="user_permission_denied"):
        super().__init__(reason, code, status)
        self.branch = branch
        self.perms = perms
        self.permission_keys = permission_keys
        self.model_name = model_name
        self.instance = instance
        self.event_type = event_type

class RestErrorException(MojoException):
    """
    Exception raised for REST API errors.

    Attributes:
        reason (str): The reason for the exception. Defaults to 'REST API Error'.
        code (int): The error code associated with the exception. Defaults to 500.
        status (int, optional): The HTTP status code. Defaults to 500.
    """

    def __init__(self, reason='REST API Error', code=500, status=500):
        """
        Initialize a RestErrorException instance.

        Args:
            reason (str, optional): The reason for the exception. Defaults to 'REST API Error'.
            code (int, optional): The error code associated with the exception. Defaults to 500.
            status (int, optional): The HTTP status code. Defaults to 500.
        """
        super().__init__(reason, code, status)


class TimeoutException(MojoException):
    """
    Exception raised when operations timeout.

    Attributes:
        reason (str): The reason for the exception. Defaults to 'Operation timed out'.
        code (int): The error code associated with the exception. Defaults to 408.
        status (int, optional): The HTTP status code. Defaults to 408.
    """

    def __init__(self, reason='Operation timed out', code=408, status=408):
        """
        Initialize a TimeoutException instance.

        Args:
            reason (str, optional): The reason for the exception. Defaults to 'Operation timed out'.
            code (int, optional): The error code associated with the exception. Defaults to 408.
            status (int, optional): The HTTP status code. Defaults to 408.
        """
        super().__init__(reason, code, status)
