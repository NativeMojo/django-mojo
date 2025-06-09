import ujson
from django.db.models import ForeignKey, OneToOneField, ManyToOneRel
from django.db.models.query import QuerySet
from django.core.exceptions import FieldDoesNotExist
from django.http import HttpResponse
import datetime
from mojo.helpers import logit
from distutils.log import info

logger = logit.get_logger("serializer", "serializer.log")

class GraphSerializer:
    """
    Custom serializer for Django models and QuerySets that applies `RestMeta.GRAPHS` dynamically.
    Supports nested relationships and different serialization graphs.
    """

    def __init__(self, instance, graph="default", many=False):
        """
        :param instance: Model instance or QuerySet.
        :param graph: The graph type to use (e.g., "default", "list").
        :param many: Boolean, if `True`, serializes a QuerySet.
        """
        self.graph = graph
        self.qset = None
        # If it's a QuerySet, mark `many=True`
        if isinstance(instance, QuerySet):
            self.many = True
            self.qset = instance
            self.instance = list(instance)  # Convert QuerySet to list for iteration
        else:
            self.many = many
            self.instance = instance

    def serialize(self):
        """
        Serializes a single model instance or a QuerySet.
        """
        if self.many:
            return [self._serialize_instance(obj) for obj in self.instance]
        return self._serialize_instance(self.instance)

    def _serialize_instance(self, obj):
        """
        Serializes a single model instance based on `RestMeta.GRAPHS`.
        """
        if not hasattr(obj, "RestMeta") or not hasattr(obj.RestMeta, "GRAPHS"):
            logger.warning("RestMeta not found")
            return self._model_to_dict_custom(obj, fields=[field.name for field in obj._meta.fields])

        graph_config = obj.RestMeta.GRAPHS.get(self.graph)
        if graph_config is None and self.graph != "default":
            self.graph = "default"
            graph_config = obj.RestMeta.GRAPHS.get(self.graph)

        # If graph is not defined or None, assume all fields should be included
        if graph_config is None:
            logger.warning(f"graph '{self.graph}' not found for {obj.__class__.__name__}")
            return self._model_to_dict_custom(obj, fields=[field.name for field in obj._meta.fields])
        else:
            logger.info(f"{obj.__class__.__name__}:{self.graph}", graph_config)
        data = self._model_to_dict_custom(obj, fields=graph_config.get("fields", None))  # Convert normal fields

        # Process extra fields (methods, metadata, etc.)
        extra_fields = graph_config.get("extra", [])
        for field in extra_fields:
            if isinstance(field, tuple):  # Handle renamed method serialization
                method_name, alias = field
            else:
                method_name, alias = field, field
            logger.info(f"Processing extra field {method_name} for {obj.__class__.__name__}")
            if hasattr(obj, method_name):
                attr = getattr(obj, method_name)
                data[alias] = attr() if callable(attr) else attr
                logger.info(f"Extra field {method_name} processed successfully", data[alias])
            else:
                logger.warning(f"Extra field {method_name} not found for {obj.__class__.__name__}")

        # Process related model graphs (ForeignKeys, OneToOneFields, ManyToManyFields)
        related_graphs = graph_config.get("graphs", {})
        for related_field, sub_graph in related_graphs.items():
            related_obj = getattr(obj, related_field, None)
            if related_obj is not None:
                # Determine if the field is a ForeignKey, OneToOneField, or ManyToManyField
                field_obj = obj._meta.get_field(related_field)
                if isinstance(field_obj, (ForeignKey, OneToOneField)):
                    # Serialize related model using its corresponding graph
                    logger.warning(f"graph '{sub_graph}' for {related_obj.__class__.__name__}")
                    data[related_field] = GraphSerializer(related_obj, graph=sub_graph).serialize()
                elif isinstance(field_obj, ManyToOneRel):
                    # Serialize related models in ManyToManyField
                    logger.warning(f"graph '{sub_graph}' for many to many relation {related_obj.model.__name__}")
                    m2m_qset = related_obj.all()
                    data[related_field] = GraphSerializer(m2m_qset, graph=sub_graph).serialize()
                else:
                    logger.warning(f"Unsupported field type for {related_field}: {type(field_obj)}")
        return data

    def get_model_field(self, model_class, field_name):
        try:
            return model_class._meta.get_field(field_name)
        except FieldDoesNotExist:
            return None

    def _model_to_dict_custom(self, obj, fields=None):
        """
        Custom serialization method for Django model instances.
        """
        data = {}
        if fields is None:
            fields = [field.name for field in obj._meta.fields]
        for name in fields:
            field_value = getattr(obj, name)
            field = self.get_model_field(obj, name)
            # Check if the field_value is callable and call it to get the value
            if callable(field_value):
                field_value = field_value()

            # Handle DateTimeField serialization to epoch
            if isinstance(field_value, datetime.datetime):
                data[name] = int(field_value.timestamp())
            # Handle date serialization to ISO format
            elif isinstance(field_value, datetime.date):
                data[name] = field_value.isoformat()
            elif field_value is not None and isinstance(field, (ForeignKey, OneToOneField)):
                data[name] = field_value.id
            else:
                data[name] = field_value
        # logger.info(data)
        return data

    def to_json(self, **kwargs):
        """Returns JSON output of the serialized data."""
        data = self.serialize()
        if self.many:
            data = dict(data=data, status=True,
                size=len(data), graph=self.graph)
        else:
            data = dict(data=data, status=True, graph=self.graph)
        data.update(dict(kwargs))
        try:
            out = ujson.dumps(data)
        except Exception as e:
            logger.exception(data)
        return out

    def to_response(self, request, **kwargs):
        """
        Determines the response format based on the client's Accept header.
        """
        # accept_header = request.headers.get('Accept', '')
        # if 'text/html' in accept_header or 'text/plain' in accept_header:
        #     json_data = self.to_json()
        #     # Wrap JSON in HTML with basic formatting for color
        #     response_data = f"""
        #     <html>
        #     <head>
        #     <style>
        #         body {{ font-family: monospace; }}
        #         .string {{ color: green; }}
        #         .number {{ color: blue; }}
        #         .boolean {{ color: purple; }}
        #         .null {{ color: red; }}
        #         .key {{ color: brown; font-weight: bold; }}
        #     </style>
        #     </head>
        #     <body>
        #     <pre>{self._colorize_json(json_data)}</pre>
        #     </body>
        #     </html>
        #     """
        #     return HttpResponse(response_data, content_type='text/html')
        # else:
        return HttpResponse(self.to_json(**kwargs), content_type='application/json')

    def _colorize_json(self, json_data):
        """Returns JSON data with HTML span wrappers for colors."""
        import re

        # Match string values and wrap them in span
        json_data = re.sub(r'(".*?")', r'<span class="string">\1</span>', json_data)
        # Match numbers and wrap them in span
        json_data = re.sub(r'\b(-?\d+\.\d+|-?\d+)\b', r'<span class="number">\1</span>', json_data)
        # Match boolean and null values
        json_data = re.sub(r'\b(true|false|null)\b', r'<span class="\1">\1</span>', json_data)
        # Match key strings
        json_data = re.sub(r'(\s*".*?")\s*:', r'<span class="key">\1</span>:', json_data)
        return json_data
