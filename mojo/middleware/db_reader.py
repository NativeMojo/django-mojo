"""Request scoping for database reader routing."""

from mojo.db import pinning


class ReaderPinMiddleware:
    SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tokens = pinning.activate(pinned=request.method not in self.SAFE_METHODS)
        try:
            return self.get_response(request)
        finally:
            pinning.deactivate(tokens)
