---
globs: mojo/**/*.py
---

# Performance Patterns

Watch for and avoid these patterns:

- **N+1 queries**: Use `select_related()` / `prefetch_related()` in REST list endpoints and any loop that accesses related objects
- **Missing indexes**: Add `db_index=True` on fields used in filters, ordering, or lookups
- **Unbounded querysets**: Always paginate list endpoints. Never `.all()` without a limit in REST context
- **Over-fetching**: Use `.values_list('id', flat=True)` when only IDs are needed. Avoid loading full objects for existence checks
- **Redis round-trips**: Use pipelines for batch operations instead of individual calls in loops
- **Redundant queries**: Use `exists()` instead of `count() > 0`. Avoid `count()` followed by the same query
