import ujson
from django.http import HttpResponse


class JsonResponse(HttpResponse):
    def __init__(self, data, status=200, safe=True, **kwargs):
        if safe and not isinstance(data, dict):
            raise TypeError(
                'In order to allow non-dict objects to be serialized set the '
                'safe parameter to False.'
            )
        kwargs.setdefault('content_type', 'application/json')
        data = ujson.dumps(data)
        super().__init__(content=data, status=status, **kwargs)
