# Django-MOJO Serializer: Suggested Performance Improvements

This document outlines a concrete plan to further optimize serialization and database fetching for the Django-MOJO serializers, with an emphasis on the `OptimizedGraphSerializer`. The goal is to reduce CPU overhead, minimize ORM round-trips, and leverage database features where possible, all while maintaining full backward compatibility.

-------------------------------------------------------------------------------

## Goals

- Reduce per-object overhead during serialization
- Eliminate repeated graph resolution and _meta lookups in hot paths
- Optimize QuerySet fetching patterns (select_related, prefetch_related, only)
- Introduce a `values()`-based fast path for "simple graphs" that need only model fields
- Preserve functional correctness, optional nested graphs, and extras
- Keep improved behaviors behind feature flags where needed

-------------------------------------------------------------------------------

## Summary of Observed Bottlenecks

- Repeated graph config parsing per object
- Frequent `_meta.get_field` calls inside tight loops
- Query optimizations applied ad hoc (per call) without caching the plan
- Attribute access and Python-side transformations dominating CPU time
- Lost opportunities to use `.values()` fast path when no nested graphs/extras are needed
- Repeated serialization of the same related objects across a single request (now improved with request cache)

-------------------------------------------------------------------------------

## Proposed Improvements

### 1) Graph Compilation with LRU Cache

Compile a model’s graph once per `(model_class, graph_name)` and cache the result. The compilation should produce:

- Resolved fields list (fallback to all model fields if not specified)
- Extras normalized to a list of (method_name, alias) tuples
- Related graphs map
- Pre-resolved FK/OneToOne field names set (for fast PK extraction)
- Prebuilt attribute getters for nested dot paths in extras (via `operator.attrgetter`)
- A reusable “query plan” (see #2)

Benefits:
- Eliminates repeated RestMeta/graph lookups
- Eliminates per-field _meta calls in hot loops
- Provides a single source of truth for fetching and serialization

Implementation notes:
- Use `functools.lru_cache(maxsize=512)` for `compile_graph(model_class, graph_name)`
- Normalize cases where graph exists but has no `fields` (serialize all model fields)

Configuration:
- MOJO_OPTIMIZED_COMPILE_CACHE_MAXSIZE (default: 512)


### 2) Query Plan Caching and Application

Attach a query plan to the compiled graph that includes:
- `select_related` fields (FK and O2O discovered in fields and related graphs)
- `prefetch_related` fields (M2M and reverse relations)
- `only()` field list for the main model (minimize columns fetched)
- Optional `Prefetch()` for related sets with `.only()` tuning

Apply the query plan once on the collection queryset before evaluation.

Benefits:
- Minimizes N+1 queries
- Reduces column payload
- Centralizes fetching strategy for consistent performance

Configuration:
- MOJO_OPTIMIZED_APPLY_QUERY_PLAN = True
- MOJO_OPTIMIZED_ONLY_FIELDS = True


### 3) `.values()` Fast Path for Simple Graphs

When a graph has:
- No extras
- No related graphs
- Only plain model fields (no callables)

Then use `QuerySet.values(*fields)` and return rows directly, with minimal post-processing (datetime/decimal handling as needed).

Benefits:
- Skips model instance creation and attribute access
- Significantly faster for large lists

Notes:
- If you later add extras or nested graphs, the fast path must be bypassed automatically

Configuration:
- MOJO_OPTIMIZED_VALUES_FASTPATH = True


### 4) Share a Single Request Cache Across Nested Serialization

Ensure nested serializers share the same request-scoped cache dict, so repeated related objects are serialized once per request.

Benefits:
- Prevents re-serializing the same related object multiple times within a request
- Big wins for graphs with repeated authors/users/tags/etc.


### 5) Hot Loop Optimizations

- Inline FK check via the compiled plan’s `fk_fields` set
- Bind local variables outside loops (e.g., `svf = self._serialize_value_fast`, `data = {}; fields = plan['fields']`) to reduce attribute lookups
- Avoid `_meta.get_field` in loops (rely on the compiled plan)
- Return early when values are None/unset as appropriate

Benefits:
- Small per-iteration savings amplify at 1k+ objects


### 6) Extras Handling and DB Annotations

For simple extras (counts, derived numbers) that can be expressed via annotations:
- Prefer `.annotate()` and include them in `values()` (for the fast path) or in `.only()` plan for instance path
- For method-based extras, keep current behavior but consider caching per object in the request cache if deterministic

Examples:
- `post_count` => `.annotate(post_count=Count('posts'))`
- `full_name` for `User` => annotate `Concat(F('first_name'), Value(' '), F('last_name'))` when appropriate

Configuration:
- MOJO_OPTIMIZED_ALLOW_ANNOTATIONS = True


### 7) Iterator vs List Strategy

- Default: use `list(qs)` which is fastest in typical scenarios when memory allows
- For very large datasets, provide an option to stream via `iterator(chunk_size=...)`
- When using `.values()` fast path, the memory footprint is smaller; streaming remains available

Configuration:
- MOJO_OPTIMIZED_LIST_EVALUATION = True
- MOJO_OPTIMIZED_ITERATOR_CHUNK_SIZE = 2000


### 8) JSON Serialization Path

- Keep `ujson` (orjson if available in future) for hot paths
- Avoid pretty-printing in normal responses
- `ensure_ascii=False`
- Provide a per-response toggle for tooling/debugging

Configuration:
- MOJO_JSON_USE_UJSON = True
- MOJO_JSON_PRETTY_DEBUG = False


### 9) Logging and Metrics

- Keep logging at WARNING or above in hot paths
- Add lightweight counters (per process) for:
  - How many serializations used values fast path
  - Cache hit/miss for request-scoped cache (approximate)
- Expose a `get_performance_info()` snapshot on serializer instances (already present; keep it cheap)

Configuration:
- MOJO_OPTIMIZED_COLLECT_METRICS = True


### 10) Feature Flags and Backwards Compatibility

- All new behavior guarded by settings flags (enabled by default where safe)
- Maintain previous public API
- Fallback paths for missing `RestMeta` or graphs:
  - If graph not found => fallback to `default`
  - If no RestMeta/default => serialize all fields (current behavior, keep)

-------------------------------------------------------------------------------

## Design Sketches (Pseudo-code)

Graph compilation:

    @lru_cache(maxsize=512)
    def compile_graph(model_class, graph_name):
        graphs = getattr(getattr(model_class, 'RestMeta', None), 'GRAPHS', {}) or {}
        gc = graphs.get(graph_name) or graphs.get('default') or {}
        fields = gc.get('fields') or [f.name for f in model_class._meta.fields]
        extras = normalize_extras(gc.get('extra', []))  # list of (method, alias)
        related_graphs = gc.get('graphs', {})
        fk_fields = detect_fk_fields(model_class, fields)
        extra_getters = build_attrgetters_for_dotted_paths(extras)
        plan = build_query_plan(model_class, fields, related_graphs)
        return {
          'fields': fields,
          'extras': extras,
          'related_graphs': related_graphs,
          'fk_fields': fk_fields,
          'extra_getters': extra_getters,
          'plan': plan,
        }

Values fast path:

    def can_use_values_fastpath(compiled):
        return not compiled['extras'] and not compiled['related_graphs']

    def serialize_queryset_fast_values(qs, compiled):
        qs = apply_only(qs, compiled['plan'])
        rows = list(qs.values(*compiled['fields']))
        return postprocess_rows(rows)  # datetime/decimal fixups

Applying query plan:

    def apply_query_plan(qs, compiled):
        plan = compiled['plan']
        if plan['select_related']: qs = qs.select_related(*plan['select_related'])
        if plan['prefetch_related']: qs = qs.prefetch_related(*plan['prefetch_related'])
        if plan['only_fields']: qs = qs.only(*plan['only_fields'])
        return qs

Instance hot loop:

    def serialize_instance_with_compiled(obj, compiled, request_cache):
        data = {}
        svf = self._serialize_value_fast
        for fname in compiled['fields']:
            val = getattr(obj, fname, None)
            if fname in compiled['fk_fields'] and hasattr(val, 'pk'):
                data[fname] = val.pk
            else:
                if callable(val): 
                    try: val = val()
                    except: val = None
                data[fname] = svf(val)
        for (method_name, alias) in compiled['extras']:
            val = resolve_extra_value(obj, method_name, compiled['extra_getters'])
            data[alias] = svf(val)
        for related_name, sub_graph in compiled['related_graphs'].items():
            data[related_name] = serialize_related(obj, related_name, sub_graph, request_cache)
        return data

-------------------------------------------------------------------------------

## Should We Patch the Existing Serializer or Add a New One?

Recommendation: Patch the existing `OptimizedGraphSerializer` incrementally behind feature flags.

Pros:
- Avoids registry complexity and code duplication
- Keeps a single optimized path maintained and tested
- We can toggle new behaviors in production via settings
- Existing API and behavior preserved (with improved performance)

When to consider a new serializer:
- If you want an experimental “v2” without any risk to current behavior
- If dramatic API changes (not planned here) are desired

Compromise approach:
- Keep changes in `OptimizedGraphSerializer`
- Add a “strict fast mode” flag (e.g., `simple_mode=True`) for safe fallbacks during rollout
- If needed, register an alias “optimized_v2” pointing to the same class but with different default flags, selectable via settings/manager

-------------------------------------------------------------------------------

## Rollout Strategy

1) Phase 1 (compile + plan):
- Introduce compile_graph() with LRU cache
- Attach and apply query plan (select_related/prefetch/only)
- Keep behavior identical (no values fast path yet)
- Flag: MOJO_OPTIMIZED_APPLY_QUERY_PLAN = True (default True)

2) Phase 2 (values fast path):
- Implement can_use_values_fastpath() and serialize_queryset_fast_values()
- Enable for limited models/graphs (opt-in list)
- Flag: MOJO_OPTIMIZED_VALUES_FASTPATH = True (default True), with allowlist MOJO_VALUES_FASTPATH_ALLOW = []

3) Phase 3 (extras annotations):
- Add optional annotation hooks
- Maintain correctness: fallback to Python extras if annotation not configured
- Flag: MOJO_OPTIMIZED_ALLOW_ANNOTATIONS = False (default off)

4) Phase 4 (observability):
- Add counters for fast path usage and request cache stats
- Light HTTP headers or log lines during debug windows

-------------------------------------------------------------------------------

## Testing Plan

- Unit tests:
  - Graph compilation correctness (fields, extras, fk_fields, plan)
  - Fallback behaviors when no RestMeta or empty graph fields
  - Request-cache sharing across nested graphs
  - Values fast path returns same data shape as instance path for simple graphs

- Integration tests:
  - Paginated collections with and without nested graphs
  - Sorting, only(), select_related and prefetch coverage
  - Large datasets (e.g. 10k rows) for memory/time regression testing

- Performance tests:
  - Compare simple vs advanced vs optimized across:
    - simple fields-only graphs
    - graphs with extras
    - graphs with nested related graphs
  - Vary dataset sizes (1k, 10k, 100k)

-------------------------------------------------------------------------------

## Metrics and Diagnostics

- Counters (in-memory):
  - optimized.values_fastpath_used (count)
  - optimized.request_cache_hits/misses (approximate)
- Optional headers (debug only):
  - X-Serializer-FastPath: values|instance
  - X-Request-Cache: hits=N;misses=M
- Expose a `get_performance_info()` summary (already exists; keep cheap)

-------------------------------------------------------------------------------

## Risks and Mitigations

- values() fast path may lose custom Python-only transformation:
  - Only enable when no extras or nested graphs are present
  - Optionally support DB annotations if needed
- only() could hide columns needed by extras:
  - Start with fields-only; allow expanding via settings/annotations
- Prefetch can over-fetch if misconfigured:
  - Cache plan per graph; keep minimal defaults, expand conservatively
- Memory pressure:
  - Provide iterator chunk option; defaults to list-based for speed

-------------------------------------------------------------------------------

## Configuration (Proposed)

- MOJO_OPTIMIZED_COMPILE_CACHE_MAXSIZE = 512
- MOJO_OPTIMIZED_APPLY_QUERY_PLAN = True
- MOJO_OPTIMIZED_ONLY_FIELDS = True
- MOJO_OPTIMIZED_VALUES_FASTPATH = True
- MOJO_VALUES_FASTPATH_ALLOW = []  (optional allowlist of model.graph pairs)
- MOJO_OPTIMIZED_ALLOW_ANNOTATIONS = False
- MOJO_OPTIMIZED_LIST_EVALUATION = True
- MOJO_OPTIMIZED_ITERATOR_CHUNK_SIZE = 2000
- MOJO_OPTIMIZED_COLLECT_METRICS = True
- MOJO_JSON_USE_UJSON = True
- MOJO_JSON_PRETTY_DEBUG = False

-------------------------------------------------------------------------------

## Acceptance Criteria

- For simple fields-only graphs, optimized serializer outperforms advanced by at least 30%
- For graphs with common repeated relationships, request-scoped caching yields 2x+ speedups vs simple
- Functional parity with existing behavior (fallbacks still work)
- No regressions in correctness verified by tests
- Feature flags allow safe rollback

-------------------------------------------------------------------------------

## Proposed Timeline

- Week 1:
  - Implement compile_graph() + query plan
  - Unit tests and initial benchmarks
- Week 2:
  - Implement values() fast path + allowlist
  - Add request cache sharing guards + metrics
  - Benchmarks across datasets
- Week 3:
  - Optional DB annotations for extras
  - Documentation and rollout
  - Enable fast path by default for safe graphs

-------------------------------------------------------------------------------

## Action Items

- [ ] Add compile_graph() (LRU-cached) and query plan builder
- [ ] Wire query plan in serialize() for QuerySets
- [ ] Implement values fast path detection + execution
- [ ] Ensure related nested serializations share the request cache
- [ ] Add feature flags and minimal metrics
- [ ] Expand unit tests and integration tests
- [ ] Document configuration and rollout steps

-------------------------------------------------------------------------------

## Conclusion

By moving to a compiled-graph model, caching the fetch plan, and using a `values()` fast path for simple graphs, we can significantly reduce CPU and DB overhead while preserving the current API and functionality. The improvements are incremental, reversible via feature flags, and deliver measurable performance gains across common workloads.
