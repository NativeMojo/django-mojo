"""
Assistant model serialization — the server chooses the graph, never the caller.

Every generic assistant path that turns a model row into data for the LLM goes
through this module: ``query_model``, ``export_data``, ``describe_model``, the
generic context builder, and a tool result that happens to contain a model
instance or a queryset.

The rule: a model is serialized through ``RestMeta.GRAPHS["ai"]`` when it
declares one, otherwise through ``RestMeta.GRAPHS["default"]``. Nothing a
caller sends can name another graph. ``GRAPH_PERMISSIONS`` is still honored,
on the graph the server selected.

``ai`` is a selection convention, not a private graph: an ordinary REST caller
with the model's view permission can still ask for ``?graph=ai``. Do not put
anything on it that those callers may not read.

This module imports nothing from the tool registry, so ``agent.py`` and
``context.py`` can use it without a registration cycle.
"""
from django.db.models import QuerySet
from django.db.models.query import ModelIterable

from mojo import errors as me

AI_GRAPH = "ai"
DEFAULT_GRAPH = "default"


class AssistantGraphError(me.MojoException):
    """The assistant has no usable graph for this model. Fail closed."""

    def __init__(self, reason):
        super().__init__(reason, 400, 400)


def model_label(model):
    meta = getattr(model, "_meta", None)
    if meta is None:
        return model.__name__
    return f"{meta.app_label}.{model.__name__}"


def is_mojo_model(obj):
    from mojo.models import MojoModel
    return isinstance(obj, MojoModel)


def is_mojo_queryset(obj):
    from mojo.models import MojoModel
    if not isinstance(obj, QuerySet):
        return False
    model = getattr(obj, "model", None)
    return isinstance(model, type) and issubclass(model, MojoModel)


def _graphs(model):
    return getattr(getattr(model, "RestMeta", None), "GRAPHS", None)


def select_graph(model, graph_override=None):
    """Return the name of the graph the assistant serializes ``model`` through.

    ``ai`` when the model declares it, otherwise ``default``. The selected
    entry must be an explicit mapping: a present-but-malformed ``ai`` (None, a
    scalar, a list) is an error, not permission to fall back, and a missing or
    malformed ``default`` is an error too. An explicit ``{}`` is valid and
    means the framework's all-fields graph.

    ``graph_override`` is for SERVER code only — the seam an instance
    permission hook uses when it downgrades a caller to a narrower graph. It
    must never be filled from tool parameters. It names the graph outright and
    gets the same validation.
    """
    label = model_label(model)
    graphs = _graphs(model)
    if not isinstance(graphs, dict) or not graphs:
        raise AssistantGraphError(
            f"{label} declares no graphs the assistant can read")
    if graph_override is not None:
        name = graph_override
        if not isinstance(name, str) or name not in graphs:
            raise AssistantGraphError(
                f"{label} has no '{name}' graph for the assistant")
    elif AI_GRAPH in graphs:
        name = AI_GRAPH
    elif DEFAULT_GRAPH in graphs:
        name = DEFAULT_GRAPH
    else:
        raise AssistantGraphError(
            f"{label} has no '{DEFAULT_GRAPH}' graph for the assistant")
    if not isinstance(graphs[name], dict):
        raise AssistantGraphError(
            f"{label} has a malformed '{name}' graph")
    return name


def authorize_graph(model, name, request=None, instance=None):
    """Enforce ``GRAPH_PERMISSIONS`` on the server-selected graph.

    Reuses the REST choke point rather than re-implementing it. With no
    request there is no caller to check, so a gated graph is refused. The
    permission check may re-point ``request.group`` at the row's group; that is
    put back, so serializing a row never changes what the request says.
    """
    if request is None:
        gated = model.get_rest_meta_prop("GRAPH_PERMISSIONS", None) or {}
        if gated.get(name):
            raise AssistantGraphError(
                f"{model_label(model)} gates its '{name}' graph and no caller "
                "is available to check")
        return
    group = getattr(request, "group", None)
    try:
        model.rest_resolve_graph_or_raise(request, name, instance=instance)
    finally:
        request.group = group


def resolve_graph(model, request=None, graph_override=None, instance=None):
    """Select the assistant graph for ``model`` and check the caller may see it.

    Raises ``AssistantGraphError`` when no usable graph exists and
    ``PermissionDeniedException`` when ``GRAPH_PERMISSIONS`` refuses it.
    """
    name = select_graph(model, graph_override=graph_override)
    authorize_graph(model, name, request=request, instance=instance)
    return name


def output_fields(model, name):
    """The ordered public keys ``name`` serializes for ``model``.

    Mirrors the graph serializer: the graph's ``fields`` (every model field
    when it lists none), minus ``exclude``, ``NO_SHOW_FIELDS`` and
    ``mojo_secrets``; then each ``extra`` under its alias; then each nested
    graph key. A key keeps the position of its first appearance.
    """
    config = _graphs(model)[name]
    rest_meta = model.RestMeta
    fields = list(config.get("fields") or [])
    if not fields:
        fields = [field.name for field in model._meta.fields]
    excluded = set(config.get("exclude") or [])
    excluded.add("mojo_secrets")
    excluded.update(getattr(rest_meta, "NO_SHOW_FIELDS", None) or [])

    names = [f for f in fields if f not in excluded and hasattr(model, f)]
    for spec in config.get("extra") or []:
        if isinstance(spec, (tuple, list)):
            attr, alias = spec
        else:
            attr, alias = spec, spec
        if hasattr(model, attr):
            names.append(alias)
    names.extend((config.get("graphs") or {}).keys())
    return list(dict.fromkeys(names))


def serialize_instance(instance, request=None, graph_override=None):
    """Serialize one MojoModel row through the server-selected graph."""
    name = resolve_graph(
        instance.__class__, request=request, graph_override=graph_override,
        instance=instance)
    return instance.to_dict(graph=name)


def serialize_queryset(queryset, request=None, graph_override=None):
    """Serialize a MojoModel queryset through the server-selected graph.

    Only a queryset of model instances is accepted. A ``.values()`` or
    ``.values_list()`` queryset yields raw column rows the graph never sees, so
    it is refused rather than passed through.
    """
    model = queryset.model
    if getattr(queryset, "_iterable_class", None) is not ModelIterable:
        raise AssistantGraphError(
            f"{model_label(model)} rows were selected as raw values and "
            "cannot be serialized for the assistant")
    name = resolve_graph(
        model, request=request, graph_override=graph_override)
    return model.queryset_to_dict(queryset, graph=name)
