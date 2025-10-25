from objict import objict, nobjict
from .request_parser import RequestDataParser
from mojo.helpers.settings import settings

DUID_HEADER = settings.get('DUID_HEADER', 'X-Mojo-UID').replace('-', '_').upper()
DUID_HEADER = f"HTTP_{DUID_HEADER}"

REQUEST_PARSER = RequestDataParser()

def parse_request_data(request):
    """
    Consolidates all GET, POST, JSON body, and FILE data into one objict dict.
    Handles dotted keys and repeated fields.
    """
    return REQUEST_PARSER.parse(request)


# Additional helper function for debugging
def debug_request_data(request):
    """
    Debug version that shows step-by-step processing
    """
    print("=== DEBUG REQUEST PARSING ===")
    print(f"Method: {request.method}")
    print(f"Content-Type: {getattr(request, 'content_type', 'Not set')}")
    print(f"GET: {dict(request.GET)}")
    print(f"POST: {dict(request.POST)}")
    print(f"FILES: {list(request.FILES.keys())}")

    result = parse_request_data(request)
    print(f"Final result: {result}")
    return result


def get_referer(request):
    return request.META.get('HTTP_REFERER')


def get_remote_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def get_ip_sources(request):
    return objict({
        'x_forwarded_for': request.META.get('HTTP_X_FORWARDED_FOR'),
        'x_forwarded_proto': request.META.get('HTTP_X_FORWARDED_PROTO'),
        'x_forwarded_port': request.META.get('HTTP_X_FORWARDED_PORT'),
        'remote_addr': request.META.get('REMOTE_ADDR'),  # Will be ALB's IP
        'x_amzn_trace_id': request.META.get('HTTP_X_AMZN_TRACE_ID'),
    })

def get_device_id(request):
    # Look for 'buid' or 'duid' in GET parameters
    duid = request.META.get(DUID_HEADER, None)
    if duid:
        return duid

    for key in ['__buid__', 'duid', "buid"]:
        if key in request.GET:
            return request.GET[key]

    # Look for 'buid' or 'duid' in POST parameters
    for key in ['buid', 'duid']:
        if key in request.POST:
            return request.POST[key]

    return None

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
