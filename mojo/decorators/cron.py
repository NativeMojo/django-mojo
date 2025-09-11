from typing import Callable

def schedule(minutes: str = '*', hours: str = '*', days: str = '*',
                   months: str = '*', weekdays: str = '*') -> Callable:
    """
    A decorator to schedule functions based on cron syntax.

    Args:
        minutes (str): The minutes argument for the cron schedule (default is '*').
        hours (str): The hours argument for the cron schedule (default is '*').
        days (str): The days of the month argument for the cron schedule (default is '*').
        months (str): The months argument for the cron schedule (default is '*').
        weekdays (str): The days of the week argument for the cron schedule (default is '*').

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
            'weekdays': weekdays
        }
        schedule.scheduled_functions.append(cron_spec)
        return func
    return decorator
