from django.db import models

from mojo.models import MojoModel


KIND_IP = "ip"
KIND_UA = "ua"

KINDS = [
    (KIND_IP, "IP address or CIDR network"),
    (KIND_UA, "User-agent regex pattern"),
]

MODE_ALLOW = "allow"
MODE_OFF = "off"
MODE_LOG = "log"
MODE_ENFORCE = "enforce"

MODES = [
    (MODE_ALLOW, "Exempt from blocking AND watching"),
    (MODE_OFF, "Kept, rendered nowhere"),
    (MODE_LOG, "Watched: logged to the edge watch log, never blocked"),
    (MODE_ENFORCE, "Blocked with 444"),
]


class BlocklistEntry(models.Model, MojoModel):
    """
    One fleet-wide blocklist rule, rendered into every generation's http base.

    **Log-first.** The migration seeds the skeleton's sec.d content in `log`
    mode: rows observe (the edge watch log) until a human flips them to
    `enforce`. `allow` rows are exemptions and render FIRST in their maps —
    nginx `map` regexes match in order of appearance, so an allow beats any
    later pattern that would also match (the canonical case: Lynx UAs contain
    `libwww-FM`, which the unanchored `libwww` token matches).

    **Fleet-scoped, deliberately group-less.** Blocking a client at the edge
    protects every tenant behind it; there is no per-tenant axis here, so
    permissions resolve on the caller's GLOBAL grants only — the same
    precedent as incident.IPSet. A `?group=` param buys nothing (see the
    tests).

    Values are data, never syntax: `ip` rows are stored as normalized
    networks, `ua` rows pass a whitelist that keeps every nginx and quoting
    metacharacter unspellable, and the renderer re-asserts both at
    substitution.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    kind = models.CharField(
        max_length=8, choices=KINDS, default=KIND_IP,
        help_text="ip | ua")

    value = models.CharField(
        max_length=512,
        help_text="ip: address or CIDR, stored normalized. "
                  "ua: whitelisted regex fragment, matched case-insensitively.")

    mode = models.CharField(
        max_length=8, choices=MODES, default=MODE_LOG, db_index=True,
        help_text="allow | off | log | enforce")

    note = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Why this row exists. The seed migration marks its rows here.")

    class Meta:
        db_table = "edge_blocklist_entry"
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "value"],
                name="edge_blocklist_kind_value_uniq"),
        ]
        ordering = ["kind", "value"]

    class RestMeta:
        CAN_DELETE = True
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security", "security"]
        LOG_CHANGES = True
        SEARCH_FIELDS = ["value", "note"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "kind", "value", "mode"],
            },
            "default": {
                "fields": [
                    "id", "created", "modified", "kind", "value", "mode",
                    "note",
                ],
            },
        }

    def __str__(self):
        return f"{self.kind}:{self.value} [{self.mode}]"

    def save(self, *args, **kwargs):
        """Validate on EVERY write path — see Upstream.save for the reasoning."""
        from mojo.apps.edge import validators

        validators.validate_blocklist_entry(self)
        return super().save(*args, **kwargs)
