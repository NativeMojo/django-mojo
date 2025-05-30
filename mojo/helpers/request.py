import ujson
from objict import objict

def parse_request_data(request):
    """
    Converts a Django request into a dictionary, handling all request methods,
    form data, JSON body, query parameters, and file uploads.

    :param request: Django HttpRequest object
    :return: dict containing parsed request data
    """
    data = objict()

    # Include query parameters (GET)
    data.update(request.GET.dict())

    # Handle JSON Body (for POST, PUT, PATCH, DELETE)
    if request.method in ["POST", "PUT", "PATCH", "DELETE"]:
        if request.content_type == "application/json":
            try:
                json_data = ujson.loads(request.body.decode("utf-8"))
                if isinstance(json_data, dict):  # Ensure it's a dictionary
                    data.update(json_data)
            except Exception:
                pass  # Ignore if body isn't valid JSON

        # Handle Form Data (POST, PUT, PATCH, DELETE)
        data.update(request.POST.dict())

    # Handle File Uploads
    if request.FILES:
        data["files"] = {}
        for key in request.FILES:
            files_list = request.FILES.getlist(key)
            data["files"][key] = files_list if len(files_list) > 1 else files_list[0]

    return data


def get_referer(request):
    return request.META.get('HTTP_REFERER')


def get_remote_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def get_user_agent(request):
    return request.META.get("HTTP_USER_AGENT", "")


def parse_user_agent(text):
    """
    returns:
        {
          'user_agent': {
            'family': 'Mobile Safari',
            'major': '13',
            'minor': '5',
            'patch': None
          },
          'os': {
            'family': 'iOS',
            'major': '13',
            'minor': '5',
            'patch': None,
            'patch_minor': None
          },
          'device': {
            'family': 'iPhone',
            'brand': None,
            'model': None
          },
          'string': '...original UA string...'
        }
    """
    if not isinstance(text, str):
        text = get_user_agent(text)
    from ua_parser import user_agent_parser
    return objict.from_dict(user_agent_parser.Parse(text))
