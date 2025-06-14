from objict import objict
from django.http import HttpResponse


class JsonResponse(HttpResponse):
    def __init__(self, data, status=200, safe=True, **kwargs):
        if safe and not isinstance(data, dict):
            raise TypeError(
                'In order to allow non-dict objects to be serialized set the '
                'safe parameter to False.'
            )
        kwargs.setdefault('content_type', 'application/json')
        if not isinstance(data, objict):
            data = objict.from_dict(data)
        if "code" not in data:
            data.code = status
        data = data.to_json(as_string=True)
        super().__init__(content=data, status=status, **kwargs)
