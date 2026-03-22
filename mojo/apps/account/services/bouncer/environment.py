import re

_HEADLESS_UA_RE = re.compile(
    r'HeadlessChrome|PhantomJS|Playwright|Puppeteer|python-requests|'
    r'Go-http-client|curl/|Wget/',
    re.IGNORECASE,
)


class EnvironmentService:
    """Server-side signal analysis from HTTP headers and GeoIP enrichment."""

    @classmethod
    def analyze_request(cls, request, geo_ip=None):
        signals = {
            'headers': cls._analyze_headers(request),
        }
        if geo_ip:
            signals['geo'] = cls._analyze_geo(geo_ip)
        return signals

    @classmethod
    def _analyze_headers(cls, request):
        ua = getattr(request, 'user_agent', '') or request.META.get('HTTP_USER_AGENT', '')
        accept = request.META.get('HTTP_ACCEPT', '')
        accept_lang = request.META.get('HTTP_ACCEPT_LANGUAGE', '')

        missing_accept = not bool(accept)
        missing_accept_language = not bool(accept_lang)
        headless_ua = bool(_HEADLESS_UA_RE.search(ua))

        # Contradiction: looks like a real browser but missing standard headers
        looks_human = bool(ua) and ('Mozilla' in ua or 'Chrome' in ua or 'Safari' in ua)
        signal_contradiction = looks_human and (missing_accept or missing_accept_language)

        return {
            'missing_accept': missing_accept,
            'missing_accept_language': missing_accept_language,
            'headless_ua': headless_ua,
            'signal_contradiction': signal_contradiction,
        }

    @classmethod
    def _analyze_geo(cls, geo_ip):
        return {
            'is_vpn': bool(geo_ip.is_vpn),
            'is_tor': bool(geo_ip.is_tor),
            'is_proxy': bool(geo_ip.is_proxy),
            'is_datacenter': bool(geo_ip.is_datacenter),
            'is_known_attacker': bool(geo_ip.is_known_attacker),
            'is_known_abuser': bool(geo_ip.is_known_abuser),
            'threat_level': geo_ip.threat_level or 'low',
            'country_code': geo_ip.country_code or '',
        }
