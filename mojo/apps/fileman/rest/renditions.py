"""Read-only description of the admin-configurable rendition options.

An admin UI reads this once to learn which rendition roles exist per media
category, their class defaults, the current global override, the numeric
caps and the allowed values for every enum option. Writes do not happen
here: the three FILEMAN_RENDITIONS_* keys are ordinary Setting rows written
through the generic settings API, where renderer.config's validators run.
"""
from mojo import decorators as md
from mojo.apps.fileman.renderer import config


@md.GET("renditions/options")
@md.requires_perms("manage_settings", "manage_files", "files", "groups")
def on_rendition_options(request):
    return {"status": True, "data": config.describe()}
