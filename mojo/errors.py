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

    Attributes:
        reason (str): The reason for the exception. Defaults to 'Permission Denied'.
        code (int): The error code associated with the exception. Defaults to 403.
        status (int, optional): The HTTP status code. Defaults to 403.
    """

    def __init__(self, reason='Permission Denied', code=403, status=403):
        """
        Initialize a PermissionDeniedException instance.

        Args:
            reason (str, optional): The reason for the exception. Defaults to 'Permission Denied'.
            code (int, optional): The error code associated with the exception. Defaults to 403.
            status (int, optional): The HTTP status code. Defaults to 403.
        """
        super().__init__(reason, code, status)

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
