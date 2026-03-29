from django.db.models import Count, Avg, Q
from mojo import decorators as md
from mojo.apps.account.models.login_event import UserLoginEvent


@md.URL('logins')
@md.URL('logins/<int:pk>')
@md.uses_model_security(UserLoginEvent)
def on_login_event(request, pk=None):
    return UserLoginEvent.on_rest_request(request, pk)


@md.GET('logins/summary')
@md.requires_perms('manage_users', 'security', 'users')
def on_login_geo_summary(request):
    qs = UserLoginEvent.objects.exclude(country_code__isnull=True).exclude(country_code='')

    dr_start = request.DATA.get('dr_start')
    dr_end = request.DATA.get('dr_end')
    if dr_start:
        qs = qs.filter(created__gte=dr_start)
    if dr_end:
        qs = qs.filter(created__lte=dr_end)

    country_code = request.DATA.get('country_code')
    drill_region = request.DATA.get('region')

    if country_code and drill_region:
        # Region drill-down within a country
        qs = qs.filter(country_code=country_code)
        rows = qs.values('country_code', 'region').annotate(
            count=Count('id'),
            latitude=Avg('latitude'),
            longitude=Avg('longitude'),
            new_region_count=Count('id', filter=Q(is_new_region=True)),
        ).order_by('-count')
    else:
        # Country-level aggregation
        rows = qs.values('country_code').annotate(
            count=Count('id'),
            latitude=Avg('latitude'),
            longitude=Avg('longitude'),
            new_country_count=Count('id', filter=Q(is_new_country=True)),
        ).order_by('-count')

    return {"status": True, "data": list(rows)}


@md.GET('logins/user')
@md.requires_perms('manage_users', 'security', 'users')
@md.requires_params('user_id')
def on_login_geo_user(request):
    user_id = request.DATA.get('user_id')
    qs = UserLoginEvent.objects.filter(
        user_id=user_id
    ).exclude(country_code__isnull=True).exclude(country_code='')

    dr_start = request.DATA.get('dr_start')
    dr_end = request.DATA.get('dr_end')
    if dr_start:
        qs = qs.filter(created__gte=dr_start)
    if dr_end:
        qs = qs.filter(created__lte=dr_end)

    country_code = request.DATA.get('country_code')
    drill_region = request.DATA.get('region')

    if country_code and drill_region:
        qs = qs.filter(country_code=country_code)
        rows = qs.values('country_code', 'region').annotate(
            count=Count('id'),
            latitude=Avg('latitude'),
            longitude=Avg('longitude'),
            new_region_count=Count('id', filter=Q(is_new_region=True)),
        ).order_by('-count')
    else:
        rows = qs.values('country_code').annotate(
            count=Count('id'),
            latitude=Avg('latitude'),
            longitude=Avg('longitude'),
            new_country_count=Count('id', filter=Q(is_new_country=True)),
        ).order_by('-count')

    return {"status": True, "data": list(rows)}
