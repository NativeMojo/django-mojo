"""Backport Django's thread-sensitive ASGI error rendering for pooled APIs."""

from functools import wraps


_MARKER = "_mojo_thread_sensitive_error_responses"


def install_thread_sensitive_error_responses():
    """Install Django 6.2's #36027 fix before the ASGI handler is built.

    Django 5.2 renders ASGI error handlers on the event loop's shared executor.
    A database connection opened there is invisible to request-finished cleanup
    and can permanently consume a pool slot. Django fixed this in 6.2 by using
    the request's thread-sensitive worker. django-mojo supports Django 5.2, so
    pooled API processes need the same behavior while that remains the case.
    """
    from asgiref.sync import iscoroutinefunction, sync_to_async
    from django.core.handlers import base, exception

    current = exception.convert_exception_to_response
    if getattr(current, _MARKER, False):
        base.convert_exception_to_response = current
        return False
    if base.convert_exception_to_response is not current:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "database pooling requires Django's unchanged ASGI exception "
            "converter; refusing an unverified handler patch"
        )

    @wraps(current)
    def convert_exception_to_response(get_response):
        if not iscoroutinefunction(get_response):
            return current(get_response)

        @wraps(get_response)
        async def inner(request):
            try:
                response = await get_response(request)
            except Exception as error:
                response = await sync_to_async(
                    exception.response_for_exception,
                    thread_sensitive=True,
                )(request, error)
            return response

        return inner

    setattr(convert_exception_to_response, _MARKER, True)
    exception.convert_exception_to_response = convert_exception_to_response
    base.convert_exception_to_response = convert_exception_to_response
    return True
