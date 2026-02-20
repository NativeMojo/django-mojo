# dates — Django Developer Reference

## Import

```python
from mojo.helpers import dates
```

## Core Functions

### `dates.utcnow()`
Returns the current UTC time as a timezone-aware `datetime`.

```python
now = dates.utcnow()
```

### `dates.parse_datetime(value)` / `dates.parse(value)`
Parses a string into a timezone-aware `datetime`. Accepts ISO 8601 and common formats.

```python
dt = dates.parse_datetime("2024-01-15")
dt = dates.parse_datetime("2024-01-15T10:30:00Z")
dt = dates.parse_datetime("2024-01-15 10:30:00")
```

### `dates.has_time_elsapsed(when, seconds=None, minutes=None, hours=None, days=None)`
Returns `True` if the given amount of time has passed since `when`.

```python
if dates.has_time_elsapsed(user.last_login, hours=24):
    send_reminder(user)
```

### `dates.add(dt, seconds=0, minutes=0, hours=0, days=0)`
Returns a new datetime with the interval added.

```python
expires = dates.add(dates.utcnow(), days=30)
```

### `dates.subtract(dt, seconds=0, minutes=0, hours=0, days=0)`
Returns a new datetime with the interval subtracted.

### `dates.is_today(dt, timezone="UTC")`
Returns `True` if `dt` falls on today's date in the given timezone.

```python
if dates.is_today(event.start_time, timezone="America/New_York"):
    ...
```

### `dates.get_local_time(dt, timezone="UTC")`
Converts a UTC datetime to the given timezone.

### `dates.get_local_day(timezone="UTC")`
Returns `(start_of_day, end_of_day)` in UTC for the current local day.

```python
start, end = dates.get_local_day("America/Chicago")
```

### `dates.get_utc_operating_day(timezone, open_hour=8, close_hour=18)`
Returns UTC time range for a business operating day.

## Usage in Models

```python
from mojo.helpers import dates

class Subscription(models.Model, MojoModel):
    def is_expired(self):
        return dates.has_time_elsapsed(self.expires_at, seconds=0)

    def extend(self, days=30):
        self.expires_at = dates.add(dates.utcnow(), days=days)
        self.save()
```
