from typing import Callable

def schedule(minutes: str = '*', hours: str = '*', days: str = '*',
                   months: str = '*', weekdays: str = '*', per_node=False):
    """
    A decorator to schedule functions based on cron syntax.

    Args:
        minutes (str): The minutes argument for the cron schedule (default is '*').
        hours (str): The hours argument for the cron schedule (default is '*').
        days (str): The days of the month argument for the cron schedule (default is '*').
        months (str): The months argument for the cron schedule (default is '*').
        weekdays (str): The days of the week argument for the cron schedule (default is '*').
        per_node (bool): Run on EVERY node of a fleet instead of exactly one.
            Defaults to False, meaning the function is fleet-once: every node
            ticks cron, but they race for a per-minute Redis claim and only the
            winner runs it. Set True only for work that is genuinely local to
            the machine it runs on. A function that merely publishes a job is
            NOT per-node — the job's own delivery decides its fan-out.

    Returns:
        Callable: The decorated function.
    """
    def decorator(func: Callable) -> Callable:
        if not hasattr(schedule, 'scheduled_functions'):
            schedule.scheduled_functions = []
        cron_spec = {
            'func': func,
            'minutes': minutes,
            'hours': hours,
            'days': days,
            'months': months,
            'weekdays': weekdays,
            'per_node': per_node
        }
        schedule.scheduled_functions.append(cron_spec)
        return func
    return decorator
