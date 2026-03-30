import re
from django.db.models import Count, Avg, Q
from mojo import decorators as md
from mojo.helpers import dates
from mojo.apps.account.models.login_event import UserLoginEvent


@md.URL('account/logins')
@md.URL('account/logins/<int:pk>')
@md.uses_model_security(UserLoginEvent)
def on_login_event(request, pk=None):
    return UserLoginEvent.on_rest_request(request, pk)


def _parse_date(value):
    if not value:
        return None
    return dates.parse(value)


def _apply_date_filters(qs, request):
    dr_start = _parse_date(request.DATA.get('dr_start'))
    dr_end = _parse_date(request.DATA.get('dr_end'))
    if dr_start:
        qs = qs.filter(created__gte=dr_start)
    if dr_end:
        qs = qs.filter(created__lte=dr_end)
    return qs


COUNTRY_CODE_RE = re.compile(r'^[A-Z]{2,3}$')
MAX_REGION_RESULTS = 500


def _validate_country_code(value):
    if not value:
        return None
    value = str(value).upper()
    if not COUNTRY_CODE_RE.match(value):
        return None
    return value


def _build_aggregation(qs, country_code, drill_region):
    country_code = _validate_country_code(country_code)

    if country_code and drill_region:
        qs = qs.filter(country_code=country_code)
        rows = qs.values('country_code', 'region').annotate(
            count=Count('id'),
            latitude=Avg('latitude'),
            longitude=Avg('longitude'),
            new_region_count=Count('id', filter=Q(is_new_region=True)),
        ).order_by('-count')[:MAX_REGION_RESULTS]
    else:
        rows = qs.values('country_code').annotate(
            count=Count('id'),
            latitude=Avg('latitude'),
            longitude=Avg('longitude'),
            new_country_count=Count('id', filter=Q(is_new_country=True)),
        ).order_by('-count')

    return list(rows)


@md.GET('account/logins/summary')
@md.requires_perms('manage_users', 'security', 'users')
def on_login_geo_summary(request):
    qs = UserLoginEvent.objects.exclude(country_code__isnull=True).exclude(country_code='')
    qs = _apply_date_filters(qs, request)

    country_code = request.DATA.get('country_code')
    drill_region = request.DATA.get('region')

    return {"status": True, "data": _build_aggregation(qs, country_code, drill_region)}


@md.GET('account/logins/user')
@md.requires_perms('manage_users', 'security', 'users')
@md.requires_params('user_id')
def on_login_geo_user(request):
    try:
        user_id = int(request.DATA.get('user_id'))
    except (ValueError, TypeError):
        return {"status": False, "error": "user_id must be an integer", "code": 400}

    qs = UserLoginEvent.objects.filter(
        user_id=user_id
    ).exclude(country_code__isnull=True).exclude(country_code='')
    qs = _apply_date_filters(qs, request)

    country_code = request.DATA.get('country_code')
    drill_region = request.DATA.get('region')

    return {"status": True, "data": _build_aggregation(qs, country_code, drill_region)}
