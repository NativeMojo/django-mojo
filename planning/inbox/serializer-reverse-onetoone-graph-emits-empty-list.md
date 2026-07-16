---
id:
type: bug
title: Graph serializers emit [] for a reverse OneToOne listed in GRAPHS "graphs" (OneToOneRel not matched as single object)
priority: P3
effort: S
owner: backend
opened: 2026-07-17
depends_on: []
related: []
links: []
---

# Reverse OneToOne in a graph serializes as an empty list

## What & Why
Listing a **reverse** OneToOne relation in a RestMeta graph's `"graphs"`
silently serializes as `[]` (and never the related object). The single-object
branch checks `isinstance(field, (ForeignKey, OneToOneField))`
(`mojo/serializers/core/serializer.py:260`; same shape in `simple.py`), but
`model._meta.get_field(<reverse related_name>)` returns a **`OneToOneRel`**,
which matches neither — so the relation falls through to the many-relation
handling and comes out as an empty list even when the related row exists.

Hit in wmx_api (WMX-API-132): `ReportExport.run = OneToOneField(...,
related_name="export")`; putting `"export": "default"` in `ReportRun`'s
default graph produced `"export": []`. Worked around downstream with a model
method + `extra` (`("export_info", "export")`), which is fine — but the graph
declaration failing silently (no error, plausible-looking `[]`) is the trap.

## Acceptance Criteria
- [ ] `OneToOneRel` handled as a single related object in both serializers
      (`getattr` already returns the instance or raises
      `RelatedObjectDoesNotExist`, which the existing
      `getattr(obj, name, None)` absorbs → `null`).
- [ ] Regression test: model pair with a reverse OneToOne in a graph —
      related row present → nested dict; absent → `null` (not `[]`).
- [ ] Queryset optimizer treats it as `select_related`, not
      `prefetch_related` (`core/serializer.py:311-320` has the same
      isinstance gap).

## Repro — bugs only
1. Any model A with `B.a = OneToOneField(A, related_name="b")`; add
   `"graphs": {"b": "default"}` to A's RestMeta graph; create A + B.
2. Serialize A with that graph.
- Expected: `"b": {…}` (or `null` when absent).
- Actual: `"b": []` always.
