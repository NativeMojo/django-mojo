from typing import Callable

# Marks a field the caller did not pass. A sentinel, not a comparison against
# "*": an explicit minutes="*" is a choice and is honoured; an omitted one is
# resolved by the rule below.
OMITTED = object()

# Coarsest to finest. Resolution walks this order so that "a coarser field is
# named" is known by the time the finer fields are decided.
FIELDS = ('weekdays', 'months', 'days', 'hours', 'minutes')

# The fields that collapse to "0" when a coarser one is named. days, months
# and weekdays never collapse: weekdays="0" means every Monday, not "Monday in
# month 0".
COLLAPSING_FIELDS = ('hours', 'minutes')


def resolve_fields(minutes=OMITTED, hours=OMITTED, days=OMITTED,
                   months=OMITTED, weekdays=OMITTED):
    """
    Resolve the five cron fields into the strings the matcher will see.

    A passed value is stored exactly as written, including an explicit "*".
    A field is *named* when it is passed with a value other than "*". An
    omitted `hours` or `minutes` becomes "0" when any coarser field is named
    and "*" otherwise, so `hours="3"` means 03:00 once and `days="1"` means
    00:00 on the 1st, while a bare call — or `hours="*"` — stays every
    minute. Explicit sub-hour minutes beside a named day still collapse the
    omitted hour: `minutes="*/5", weekdays="5,6"` is 00:00-00:55 at weekends,
    and the load-time warning says so; name `hours="*"` for all day.
    """
    given = {'minutes': minutes, 'hours': hours, 'days': days,
             'months': months, 'weekdays': weekdays}
    resolved = {}
    coarser_named = False
    for field in FIELDS:
        value = given[field]
        if value is OMITTED:
            value = '0' if coarser_named and field in COLLAPSING_FIELDS else '*'
        elif value != '*':
            coarser_named = True
        resolved[field] = value
    return resolved


def schedule(minutes=OMITTED, hours=OMITTED, days=OMITTED, months=OMITTED,
             weekdays=OMITTED, per_node=False):
    """
    A decorator to schedule functions based on cron syntax.

    Omitted finer fields mean "0", not "every": when a coarser field is
    named (passed with a value other than "*") and a finer one is left
    out, the finer one resolves to "0". So `@schedule(hours="3")` runs once
    at 03:00 — not at every minute from 03:00 to 03:59 — and
    `@schedule(days="1")` runs at 00:00 on the 1st. A bare `@schedule()`
    still runs every minute, and any value written explicitly is kept
    exactly as written (`minutes="*/5"`, or an explicit `minutes="*"`
    beside `hours="3"`). The registry holds the resolved values, so
    `run_now` and the heartbeat see what actually fires. Loading the cron
    modules logs a warning for any schedule that names an hour or day yet
    still fires more than once per hour.

    Args:
        minutes (str): The minutes argument for the cron schedule.
        hours (str): The hours argument for the cron schedule.
        days (str): The days of the month argument for the cron schedule.
        months (str): The months argument for the cron schedule.
        weekdays (str): The days of the week argument for the cron schedule.
        per_node (bool): Run on EVERY node of a fleet instead of exactly one.
            Defaults to False, meaning the function is fleet-once: every node
            ticks cron, but they race for a per-minute Redis claim and only the
            winner runs it. Set True only for work that is genuinely local to
            the machine it runs on. A function that merely publishes a job is
            NOT per-node — the job's own delivery decides its fan-out.

    Returns:
        Callable: The decorated function.
    """
    resolved = resolve_fields(minutes=minutes, hours=hours, days=days,
                              months=months, weekdays=weekdays)

    def decorator(func: Callable) -> Callable:
        if not hasattr(schedule, 'scheduled_functions'):
            schedule.scheduled_functions = []
        cron_spec = {'func': func}
        cron_spec.update(resolved)
        cron_spec['per_node'] = per_node
        schedule.scheduled_functions.append(cron_spec)
        return func
    return decorator
