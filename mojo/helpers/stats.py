from django.db.models import Sum, Avg, Max, Min, Count, StdDev, Variance, Q
from django.db.models.functions import Coalesce
from types import SimpleNamespace


def get_sum(queryset, *field_names):
    """
    Get sum of one or more fields.

    Usage:
        total = get_sum(qset, "opens")
        stats = get_sum(qset, "opens", "closes")
        stats.opens, stats.closes
    """
    if len(field_names) == 1:
        result = queryset.aggregate(result=Coalesce(Sum(field_names[0]), 0))
        return result['result']

    aggregations = {name: Coalesce(Sum(name), 0) for name in field_names}
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_average(queryset, *field_names):
    """Get average of one or more fields."""
    if len(field_names) == 1:
        result = queryset.aggregate(result=Avg(field_names[0]))
        return result['result']

    aggregations = {name: Avg(name) for name in field_names}
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_avg(queryset, *field_names):
    """Alias for get_average."""
    return get_average(queryset, *field_names)


def get_max(queryset, *field_names):
    """Get maximum value of one or more fields."""
    if len(field_names) == 1:
        result = queryset.aggregate(result=Max(field_names[0]))
        return result['result']

    aggregations = {name: Max(name) for name in field_names}
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_min(queryset, *field_names):
    """Get minimum value of one or more fields."""
    if len(field_names) == 1:
        result = queryset.aggregate(result=Min(field_names[0]))
        return result['result']

    aggregations = {name: Min(name) for name in field_names}
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_count(queryset, field_name=None):
    """
    Get count of records or distinct values.

    Usage:
        total = get_count(qset)
        unique_users = get_count(qset, "user_id")
    """
    if field_name:
        result = queryset.aggregate(result=Count(field_name, distinct=True))
    else:
        result = queryset.aggregate(result=Count('*'))
    return result['result']


def get_stddev(queryset, *field_names):
    """Get standard deviation of one or more fields."""
    if len(field_names) == 1:
        result = queryset.aggregate(result=StdDev(field_names[0]))
        return result['result']

    aggregations = {name: StdDev(name) for name in field_names}
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_variance(queryset, *field_names):
    """Get variance of one or more fields."""
    if len(field_names) == 1:
        result = queryset.aggregate(result=Variance(field_names[0]))
        return result['result']

    aggregations = {name: Variance(name) for name in field_names}
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_range(queryset, field_name):
    """
    Get min and max together.

    Usage:
        range_stats = get_range(qset, "duration")
        range_stats.min, range_stats.max
    """
    result = queryset.aggregate(
        min=Min(field_name),
        max=Max(field_name)
    )
    return SimpleNamespace(**result)


def get_summary(queryset, field_name):
    """
    Get common stats bundle (count, sum, avg, min, max).

    Usage:
        stats = get_summary(qset, "duration")
        stats.count, stats.sum, stats.avg, stats.min, stats.max
    """
    result = queryset.aggregate(
        count=Count('*'),
        sum=Coalesce(Sum(field_name), 0),
        avg=Avg(field_name),
        min=Min(field_name),
        max=Max(field_name)
    )
    return SimpleNamespace(**result)


def get_stats(queryset, **aggregations):
    """
    Get custom aggregations.

    Usage:
        from django.db.models import Sum, Avg
        stats = get_stats(qset,
            total_opens=Sum("opens"),
            avg_duration=Avg("duration")
        )
        stats.total_opens, stats.avg_duration
    """
    result = queryset.aggregate(**aggregations)
    return SimpleNamespace(**result)


def get_count_if(queryset, condition):
    """
    Get count with condition.

    Usage:
        count = get_count_if(qset, Q(status='active'))
    """
    return queryset.filter(condition).count()


def get_sum_if(queryset, field_name, condition):
    """
    Get sum with condition.

    Usage:
        total = get_sum_if(qset, "amount", Q(status='paid'))
    """
    result = queryset.filter(condition).aggregate(result=Coalesce(Sum(field_name), 0))
    return result['result']


def get_percentage(queryset, condition):
    """
    Get percentage of records matching condition.

    Usage:
        pct = get_percentage(qset, Q(status='complete'))
    """
    total = queryset.count()
    if total == 0:
        return 0.0
    matching = queryset.filter(condition).count()
    return (matching / total) * 100


def get_earliest(queryset, field_name):
    """Get earliest date/time value."""
    result = queryset.aggregate(result=Min(field_name))
    return result['result']


def get_latest(queryset, field_name):
    """Get latest date/time value."""
    result = queryset.aggregate(result=Max(field_name))
    return result['result']


def get_duration_stats(queryset, field_name):
    """
    Get specialized stats for duration fields.

    Usage:
        stats = get_duration_stats(qset, "duration")
        stats.avg, stats.max, stats.min, stats.total
    """
    result = queryset.aggregate(
        avg=Avg(field_name),
        max=Max(field_name),
        min=Min(field_name),
        total=Coalesce(Sum(field_name), 0),
        count=Count('*')
    )
    return SimpleNamespace(**result)
