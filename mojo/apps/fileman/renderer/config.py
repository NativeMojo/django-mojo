"""Admin-configurable rendition options.

Three JSON ``Setting`` keys (one per media category) let an admin adjust the
options every renderer uses and which roles run automatically on upload:

    FILEMAN_RENDITIONS_IMAGE / _VIDEO / _DOCUMENT =
        {"<role>": {<options>}, "_automatic": ["<role>", ...]}

Per-role options shallow-merge over the renderer's ``default_renditions``;
``_automatic`` replaces the renderer's automatic-role list. Anything absent
falls back to the class default, so an empty object means "defaults".

This module is the single owner of the option schema: the write-time
validators registered from ``FilemanConfig.ready`` and the read-only
``/api/fileman/renditions/options`` payload are both derived from it, and the
list of valid roles is always the renderer's own ``default_renditions`` keys.
"""
import json
import re

from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger(__name__, "fileman.log")

CATEGORY_KEYS = {
    "image": "FILEMAN_RENDITIONS_IMAGE",
    "video": "FILEMAN_RENDITIONS_VIDEO",
    "document": "FILEMAN_RENDITIONS_DOCUMENT",
}
VIDEO_ENGINE_KEY = "FILEMAN_VIDEO_ENGINE"
VIDEO_ENGINES = ("ffmpeg",)
AUTOMATIC_KEY = "_automatic"

IMAGE_MODES = ("contain", "crop", "stretch")
IMAGE_FORMATS = ("jpeg", "jpg", "png", "webp", "gif")
STILL_FORMATS = ("jpg", "png")
VIDEO_FORMATS = ("mp4", "webm")
VIDEO_CODECS = ("h264", "h265")
# Presets stop at "slow": slower/veryslow/placebo multiply encode time for a
# marginal size gain, and a group admin may write these rows (see LIMITS).
VIDEO_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
PDF_QUALITIES = ("low", "medium", "high")

# Inclusive numeric caps. They bound what one rendition job may cost: a row
# can be written by anyone holding the bare `groups` permission for their
# group, so no option may order an unbounded encode.
LIMITS = {
    "width": (1, 4096),
    "height": (1, 4096),
    "quality": (1, 100),
    "crf": (18, 51),
    "duration": (1, 60),
    "max_pages": (1, 200),
    "page": (1, 500),
}

_BITRATE_RE = re.compile(r"^\d+[kKmM]?$")
_TIME_OFFSET_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?$")

# Roles that produce a still image rather than the category's main artifact.
THUMBNAIL_ROLES = {
    "video": ("thumbnail", "video_thumbnail"),
    "document": ("thumbnail", "document_thumbnail"),
}

# (category, role kind) -> option -> (checker, argument)
OPTION_SCHEMA = {
    ("image", "image"): {
        "width": ("int", "width"),
        "height": ("int", "height"),
        "mode": ("choice", IMAGE_MODES),
        "format": ("choice", IMAGE_FORMATS),
        "quality": ("int", "quality"),
    },
    ("video", "thumbnail"): {
        "width": ("int", "width"),
        "height": ("int", "height"),
        "time_offset": ("time_offset", None),
        "format": ("choice", STILL_FORMATS),
    },
    ("video", "transcode"): {
        "width": ("int", "width"),
        "height": ("int", "height"),
        "bitrate": ("bitrate", None),
        "format": ("choice", VIDEO_FORMATS),
        "audio": ("bool", None),
        "duration": ("int", "duration"),
        "codec": ("choice", VIDEO_CODECS),
        "crf": ("int", "crf"),
        "preset": ("choice", VIDEO_PRESETS),
    },
    ("document", "thumbnail"): {
        "width": ("int", "width"),
        "height": ("int", "height"),
        "page": ("int", "page"),
        "format": ("choice", STILL_FORMATS),
    },
    ("document", "pdf"): {
        "quality": ("choice", PDF_QUALITIES),
        "max_pages": ("int", "max_pages"),
    },
}

# Options that only make sense for an mp4 transcode. "format" wins: on a webm
# role they are refused when present in the override and ignored in defaults.
MP4_ONLY_OPTIONS = ("codec", "crf", "preset")


def role_kind(category, role):
    if category == "image":
        return "image"
    if role in THUMBNAIL_ROLES.get(category, ()):
        return "thumbnail"
    return "transcode" if category == "video" else "pdf"


def renderer_for_category(category):
    # Local imports: the renderer modules import base.py, which imports this
    # module, so a top-level import here would be a cycle.
    from mojo.apps.fileman.renderer.image import ImageRenderer
    from mojo.apps.fileman.renderer.video import VideoRenderer
    from mojo.apps.fileman.renderer.document import DocumentRenderer
    renderers = {
        "image": ImageRenderer,
        "video": VideoRenderer,
        "document": DocumentRenderer,
    }
    if category not in renderers:
        raise ValueError("unknown rendition category: %s" % category)
    return renderers[category]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_int(where, name, value):
    low, high = LIMITS[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s: must be a whole number" % where)
    if value < low or value > high:
        raise ValueError("%s: must be between %d and %d" % (where, low, high))


def _check_option(where, spec, value):
    checker, arg = spec
    if checker == "int":
        _check_int(where, arg, value)
    elif checker == "choice":
        if not isinstance(value, str) or value not in arg:
            raise ValueError("%s: must be one of %s" % (where, ", ".join(arg)))
    elif checker == "bool":
        if not isinstance(value, bool):
            raise ValueError("%s: must be true or false" % where)
    elif checker == "bitrate":
        if not isinstance(value, str) or not _BITRATE_RE.match(value):
            raise ValueError("%s: must look like 2000k or 2M" % where)
    elif checker == "time_offset":
        if not isinstance(value, str) or not _TIME_OFFSET_RE.match(value):
            raise ValueError("%s: must look like HH:MM:SS" % where)


def validate(category, parsed):
    """Raise ValueError naming role.option for anything the renderer would
    not honor. An empty object is valid and means "class defaults"."""
    if not isinstance(parsed, dict):
        raise ValueError("%s must be a JSON object" % CATEGORY_KEYS[category])
    defaults = renderer_for_category(category).default_renditions
    for role, options in parsed.items():
        if role == AUTOMATIC_KEY:
            if (not isinstance(options, list)
                    or any(not isinstance(r, str) for r in options)):
                raise ValueError("%s: must be a list of role names" % AUTOMATIC_KEY)
            unknown = [r for r in options if r not in defaults]
            if unknown:
                raise ValueError("%s: unknown role %s (valid: %s)" % (
                    AUTOMATIC_KEY, unknown[0], ", ".join(sorted(defaults))))
            continue
        if role not in defaults:
            raise ValueError("%s: unknown role (valid: %s)" % (
                role, ", ".join(sorted(defaults))))
        if not isinstance(options, dict):
            raise ValueError("%s: must be an object of options" % role)
        schema = OPTION_SCHEMA[(category, role_kind(category, role))]
        for name, value in options.items():
            where = "%s.%s" % (role, name)
            if name not in schema:
                raise ValueError("%s: unknown option (valid: %s)" % (
                    where, ", ".join(sorted(schema))))
            _check_option(where, schema[name], value)
        if "codec" in schema:
            merged_format = options.get("format", defaults[role].get("format", "mp4"))
            if merged_format != "mp4":
                for name in MP4_ONLY_OPTIONS:
                    if name in options:
                        raise ValueError(
                            "%s.%s: only valid when format is mp4" % (role, name))


def validate_image(key, parsed):
    validate("image", parsed)


def validate_video(key, parsed):
    validate("video", parsed)


def validate_document(key, parsed):
    validate("document", parsed)


def validate_engine(key, parsed):
    if parsed not in VIDEO_ENGINES:
        raise ValueError("%s: must be one of %s" % (key, ", ".join(VIDEO_ENGINES)))


# ---------------------------------------------------------------------------
# Loading and merging
# ---------------------------------------------------------------------------

def _parse(key, raw):
    """A database row arrives as JSON text; a deployment-file value arrives as
    a dict. Anything unusable is logged and treated as "no override" so a
    hand-edited row can never take a worker down."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("%s is not valid JSON; ignoring the override", key)
            return {}
        if isinstance(parsed, dict):
            return parsed
    logger.warning("%s is not a JSON object; ignoring the override", key)
    return {}


def load(category, group=None):
    """The resolved override for a category: group row -> parents -> global
    -> deployment file. ``{}`` when nothing is set."""
    key = CATEGORY_KEYS[category]
    return _parse(key, settings.get(key, None, group=group))


def global_override(category):
    """The global DATABASE row only, parsed, or None. This is what an admin
    can clear; a deployment-file value is deliberately not reported here."""
    from mojo.apps.account.models import Setting
    key = CATEGORY_KEYS[category]
    row = Setting.objects.filter(key=key, group=None).first()
    if row is None or row.is_secret:
        return None
    parsed = _parse(key, row.get_value())
    return parsed if parsed else None


def merge(defaults, automatic_default, override):
    """Return (options_by_role, automatic_roles) with the override applied.

    Roles come from ``defaults``; an override for a role the renderer does not
    declare is ignored (the validator refuses it at write time anyway).
    """
    options = {}
    for role, base in defaults.items():
        merged = dict(base)
        extra = override.get(role)
        if isinstance(extra, dict):
            merged.update(extra)
        options[role] = merged
    automatic = override.get(AUTOMATIC_KEY)
    if isinstance(automatic, list):
        automatic = tuple(r for r in automatic if r in defaults)
    else:
        automatic = tuple(automatic_default)
    return options, automatic


def describe():
    """Everything an admin UI needs to edit the three settings without
    hardcoding roles, defaults, limits or choices."""
    categories = {}
    for category, key in CATEGORY_KEYS.items():
        renderer = renderer_for_category(category)
        defaults = {role: dict(opts) for role, opts in renderer.default_renditions.items()}
        automatic_default = list(renderer.default_automatic_roles())
        effective, automatic = merge(defaults, automatic_default, load(category))
        kinds = {role: role_kind(category, role) for role in defaults}
        choices = {}
        for kind in sorted(set(kinds.values())):
            schema = OPTION_SCHEMA[(category, kind)]
            choices[kind] = {
                name: list(spec[1]) for name, spec in schema.items()
                if spec[0] == "choice"
            }
        fields = {
            kind: sorted(OPTION_SCHEMA[(category, kind)])
            for kind in sorted(set(kinds.values()))
        }
        categories[category] = {
            "key": key,
            "defaults": defaults,
            "automatic_default": automatic_default,
            "override": global_override(category),
            "effective": {"roles": effective, "automatic": list(automatic)},
            "role_kinds": kinds,
            "fields": fields,
            "choices": choices,
            "limits": {
                name: list(bounds) for name, bounds in LIMITS.items()
                if any(name in OPTION_SCHEMA[(category, kind)] for kind in fields)
            },
        }
    engine = settings.get(VIDEO_ENGINE_KEY, None)
    return {
        "categories": categories,
        "engine": {
            "key": VIDEO_ENGINE_KEY,
            "choices": list(VIDEO_ENGINES),
            "effective": engine if engine in VIDEO_ENGINES else VIDEO_ENGINES[0],
        },
    }
