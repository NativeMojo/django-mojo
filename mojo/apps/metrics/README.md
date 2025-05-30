# Redis Metrics

The `redis_metrics` module is designed to record and retrieve metrics using Redis as the data store. This package helps in maintaining metrics at various time granularities (e.g., hours, days, months, etc.) and organizing them into categories for better management.

Ensure that Redis is running, and your Django project is configured to connect to it.

## Usage

### Recording Metrics

The `record_metrics` function is used to record events or occurrences by incrementing counters in Redis.

#### Parameters

- **slug (str)**: The base identifier for the metric.
- **when (datetime)**: The timestamp when the event occurred.
- **count (int, optional)**: The number by which to increment the metric. Default is 0.
- **group (optional)**: Reserved for future use.
- **category (optional)**: Used to group slugs into categories.
- **min_granulariy (str, optional)**: The smallest time unit for metric tracking (e.g., "hours"). Default is "hours".
- **max_granularity (str, optional)**: The largest time unit for metric tracking (e.g., "years"). Default is "years".
- **args (*args)**: Additional arguments used in slug generation.

#### Example

```python
from datetime import datetime
from metrics.redis_metrics import record_metrics

# Record a metric 'page_views' for current time, incremented by 1.
record_metrics(slug="page_views", when=datetime.now(), count=1)

# Record a metric 'user_signups' with category 'activity', incremented by 5 at specific time.
record_metrics(slug="user_signups", when=datetime(2023,10,10), count=5, category="activity")

# Record metrics with different granularities
record_metrics(slug="app_usage", when=datetime.now(), count=10, min_granulariy="minutes", max_granularity="days")
```

### Retrieving Metrics

Retrieve the recorded metrics using a variety of retrieval methods provided by the module.

#### Get Metrics from Slug

Fetch the metrics for a specific slug between two datetime ranges at a given granularity.

#### Example

```python
from datetime import datetime
from metrics.redis_metrics import get_metrics

# Fetch 'page_views' metrics from October 1, 2023, to October 10, 2023, with hourly granularity.
metrics = get_metrics(slug="page_views", dt_start=datetime(2023, 10, 1), dt_end=datetime(2023, 10, 10), granularity="hours")
print(metrics)
```

### Categories

You can organize your metrics into categories for more structured management.

#### Example

```python
from metrics.redis_metrics import get_category_slugs, get_categories

# Get all the slugs in the 'activity' category.
activity_slugs = get_category_slugs(category="activity")
print(activity_slugs)

# Get all categories.
categories = get_categories()
print(categories)
```

## Conclusion

Redis Metrics is a robust utility for tracking application-specific metrics over various time periods with ease. The flexibility provided through time granularity and categorization makes it ideal for applications requiring detailed metric tracking and analysis.
