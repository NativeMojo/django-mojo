from django.db import models
from django.template import Context
from django.template import Template
from mojo.models import MojoModel


try:
    import css_inline
    inliner = css_inline.CSSInliner(remove_style_tags=True)
except Exception:
    inliner = None


class NotifyTemplate(models.Model, MojoModel):
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True)
    group = models.ForeignKey("account.Group", blank=True, null=True, default=None,
        related_name="templates", on_delete=models.CASCADE)
    name = models.CharField(max_length=255, db_index=True)
    kind = models.CharField(max_length=124, default="email")
    template = models.TextField()

    def render(self, context):
        template = Template(self.template)
        if context is None:
            context = {}
        context = Context(context)
        if inliner is not None:
            return inliner.inline(template.render(context))
        return template.render(context)
