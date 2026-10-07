import json
from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers import logit

REDIS_GLOBAL_KEY = "settings:global"
REDIS_GROUP_PREFIX = "settings:g:"
MAX_PARENT_DEPTH = 10

# A scope's hash field holds this when the scope has NO row for the key, so an
# unset key costs one Redis read instead of a SELECT on every request. A real
# value can never equal it: PostgreSQL text cannot store a NUL byte.
CACHE_MISS = "\x00unset"
# Backstop for any write that bypasses push_to_cache/remove_from_cache (a
# queryset update, raw SQL): the whole hash expires this long after it was
# first written and is rebuilt from the database on demand. Set only when the
# hash has no TTL, so a busy hash cannot keep postponing it forever.
CACHE_TTL = 3600
_POOL = object()


class Setting(MojoSecrets, MojoModel):
    """
    Database-backed settings with optional encryption and group scoping.

    Lookup chain (via SettingsHelper):
        Redis cache -> DB (group -> parent chain -> global) -> django.conf.settings
    A scope with no row is cached as CACHE_MISS, so an unset key costs no SQL
    after its first read (see resolve).

    Secret values are stored encrypted in mojo_secrets (via MojoSecrets mixin).
    Non-secret values are stored in the plain `value` field.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    key = models.CharField(max_length=255, db_index=True)
    value = models.TextField(blank=True, default="")
    is_secret = models.BooleanField(default=False, db_index=True)
    group = models.ForeignKey(
        "account.Group", null=True, blank=True, default=None,
        on_delete=models.CASCADE, related_name="settings",
    )

    class Meta:
        unique_together = [("key", "group")]
        ordering = ["key"]

    class RestMeta:
        VIEW_PERMS = ["manage_settings", "groups"]
        SAVE_PERMS = ["manage_settings", "groups"]
        SEARCH_FIELDS = ["key"]
        GRAPHS = {
            "default": {
                "exclude": ["mojo_secrets"],
                "extra": ["display_value"],
                "graphs": {
                    "group": "basic",
                },
            },
        }

    def __str__(self):
        scope = f"group:{self.group_id}" if self.group_id else "global"
        return f"{self.key} ({scope})"

    # ------------------------------------------------------------------
    # Value read/write
    # ------------------------------------------------------------------

    def get_value(self):
        """Return the setting value, decrypting if secret."""
        if self.is_secret:
            return self.get_secret("value")
        return self.value

    def set_value(self, raw_value):
        """Set the setting value, encrypting if secret."""
        if self.is_secret:
            self.value = ""
            self.set_secret("value", raw_value)
        else:
            self.value = raw_value if isinstance(raw_value, str) else json.dumps(raw_value)
            self.set_secret("value", None)

    @property
    def display_value(self):
        if self.is_secret:
            return "******"
        return self.value

    # ------------------------------------------------------------------
    # REST hooks
    # ------------------------------------------------------------------

    # Per-key write validation. A registered key is validated on EVERY write
    # path — the generic /api/settings REST (on_rest_pre_save, readable 400)
    # AND Django save() (Setting.set, shell) — so a typo'd value can never
    # persist and surface only at request time (a geofence rule_invalid deny,
    # a truthy-coerced posture flag, nonsense fail-closed scopes).
    # key -> {"func": callable(key, parsed) raising ValueError, "global_only": bool}
    VALIDATORS = {}

    # Global settings the geofence engine consumes. The family tuple wires the
    # decision-cache invalidation on save/delete for every key; the per-key
    # validators are registered at the bottom of this module.
    GEOFENCE_KEYS = (
        "GEOFENCE_SYSTEM_RULES", "GEOFENCE_ALLOWLIST", "GEOFENCE_STRICT_POSTURE",
        "GEOFENCE_ENABLED", "GEOFENCE_FAIL_CLOSED", "GEOFENCE_FAIL_CLOSED_SCOPES",
        "GEOFENCE_ALLOW_PRIVATE_IPS", "GEOFENCE_CACHE_TTL")

    @classmethod
    def register_validator(cls, key, func, global_only=True):
        """Register a write-time validator for a Setting key.

        func(key, parsed_value) raises ValueError on a bad value; parsed_value
        is the JSON-decoded value (a registered key's value must be valid
        JSON). global_only keys reject group-scoped rows. Registered keys also
        reject is_secret rows (validators need plaintext; a masked value would
        hide enforcement config). Downstream apps register their own
        enforcement-bearing keys at import time (e.g. mverify's
        PAYMENTS_GEOFENCE_RULES).
        """
        cls.VALIDATORS[key] = {"func": func, "global_only": global_only}

    def on_rest_pre_save(self, changed_fields, created):
        """Encrypt secret values before saving via REST."""
        self._reject_protected_write(rest=True)
        self._reject_scope_change()
        if created:
            self._reject_create_outside_request_group()
        if self.is_secret and "value" in changed_fields:
            raw = self.value
            self.value = ""
            self.set_secret("value", raw)
        self._validate_value()

    def on_rest_pre_delete(self):
        self._reject_protected_write(rest=True)

    def _reject_scope_change(self):
        """A setting's group is fixed when the row is created, for every writer.

        The generic REST save authorizes an update against the group the row is
        LEAVING, so a member holding `manage_settings` in one group could clear
        `group` (a platform-wide row, which overrides the deployment's own
        configuration for every tenant) or point it at a group they can only
        view. Compared against the stored row rather than changed_fields:
        `group`, `group_id`, null, blank and zero all end as a different
        group_id, so one comparison covers every spelling. Runs in the REST
        pre-save hook (readable 400 before side effects) AND in save() (so
        Setting.set / programmatic / shell writes cannot move a row either).
        """
        if not self.pk:
            return
        stored = Setting.objects.filter(pk=self.pk).values_list(
            "group_id", flat=True)
        if not stored:
            # An insert with an explicit pk: no stored row to move.
            return
        if stored[0] != self.group_id:
            from mojo import errors as merrors
            raise merrors.ValueException(
                "a setting's group cannot be changed; "
                "create it in the new scope instead")

    def _reject_create_outside_request_group(self):
        """A REST create authorized through a group lands in that group.

        The create permission check runs against request.group, but the body
        can name a second group (`{"group": A, "group_id": B}`) that is attached
        after only a VIEW check on it. When request.group is None the generic
        check already required the platform-wide permission, so the row may
        land anywhere. No ambient request (an in-process create_from_dict):
        nothing to compare with.
        """
        request = self.active_request
        if request is None:
            return
        group = getattr(request, "group", None)
        if group is not None and self.group_id != group.pk:
            from mojo import errors as merrors
            raise merrors.PermissionDeniedException()

    def _protected_keys_involved(self):
        from mojo.apps.account.services import system_settings
        from mojo.apps.account.services import admin_settings
        scopes = {(self.key, self.group_id)}
        if self.pk:
            original = Setting.objects.filter(pk=self.pk).values_list(
                "key", "group_id").first()
            if original:
                scopes.add(original)
        protected = []
        for key, group_id in scopes:
            # Preserve the existing system-settings protection exactly: those
            # owner-only keys are protected in every scope.  The new Admin
            # catalog protection is deliberately narrower and owns only the
            # global row, so compatible group-scoped rows remain available.
            if system_settings.is_protected_setting(key):
                protected.append(key)
            elif group_id is None and admin_settings.is_catalog_protected(key):
                protected.append(key)
        return protected

    def _reject_protected_write(self, rest=False):
        protected = self._protected_keys_involved()
        if not protected:
            return
        from mojo import errors as merrors
        source = "generic settings API" if rest else "generic Setting writer"
        raise merrors.PermissionDeniedException(
            f"{protected[0]} is protected and cannot be changed through the {source}")

    def _validate_value(self):
        """Reject a malformed value for any registered key. Runs in the REST
        pre-save hook (readable 400 before side effects) AND in save() (so
        Setting.set / programmatic / shell writes have no unvalidated path)."""
        entry = self.VALIDATORS.get(self.key)
        if entry is None:
            return
        from mojo import errors as merrors
        if entry["global_only"] and self.group_id is not None:
            # The consumers only ever resolve these keys globally — a group-
            # scoped row would be dead, unvalidated config. Reject loudly
            # instead of silently accepting it.
            raise merrors.ValueException(
                f"{self.key} is a global-only setting; group-scoped rows are not supported")
        if self.is_secret:
            # A validated key can never be secret: validators need the
            # plaintext, and is_secret would both skip validation and mask the
            # enforcement value ("******") from every other admin.
            raise merrors.ValueException(
                f"{self.key} is a validated setting and cannot be secret")
        parsed = self.value
        if isinstance(parsed, str):
            if not parsed.strip():
                return
            try:
                parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                raise merrors.ValueException(f"{self.key} must be valid JSON")
        try:
            entry["func"](self.key, parsed)
        except ValueError as exc:
            raise merrors.ValueException(str(exc))

    # ------------------------------------------------------------------
    # Redis cache helpers
    # ------------------------------------------------------------------

    @classmethod
    def _redis(cls):
        try:
            from mojo.helpers.redis import get_connection
            return get_connection()
        except Exception:
            return None

    @classmethod
    def _redis_key(cls, group_id=None):
        if group_id:
            return f"{REDIS_GROUP_PREFIX}{group_id}"
        return REDIS_GLOBAL_KEY

    @staticmethod
    def _cache_text(val):
        return val if isinstance(val, str) else json.dumps(val)

    @staticmethod
    def _cache_write(r, rkey, name, text, only_if_absent=False):
        """HSET (or HSETNX) one field and start the hash's TTL if it has none."""
        pipe = r.pipeline(transaction=False)
        if only_if_absent:
            pipe.hsetnx(rkey, name, text)
        else:
            pipe.hset(rkey, name, text)
        pipe.ttl(rkey)
        if pipe.execute()[-1] == -1:
            r.expire(rkey, CACHE_TTL)

    def push_to_cache(self):
        """Write this setting into the Redis hash for its scope.

        Overwrites a cached miss, so a key set after it was read as unset is
        visible on the next read.
        """
        r = self._redis()
        if not r:
            return
        rkey = self._redis_key(self.group_id)
        val = self.get_value()
        if val is None:
            r.hdel(rkey, self.key)
        else:
            self._cache_write(r, rkey, self.key, self._cache_text(val))

    def remove_from_cache(self):
        """Remove this setting from the Redis hash."""
        r = self._redis()
        if not r:
            return
        r.hdel(self._redis_key(self.group_id), self.key)

    @classmethod
    def warm_cache(cls, group_id=None):
        """Load all settings for a scope into Redis."""
        r = cls._redis()
        if not r:
            return
        rkey = cls._redis_key(group_id)
        r.delete(rkey)
        qs = cls.objects.filter(group_id=group_id)
        pipe = r.pipeline(transaction=False)
        for s in qs:
            val = s.get_value()
            if val is not None:
                pipe.hset(rkey, s.key, cls._cache_text(val))
        pipe.expire(rkey, CACHE_TTL)
        pipe.execute()

    @staticmethod
    def _cache_read(r, rkey, name):
        """HGET one field: the cached text, CACHE_MISS, or None. Raises when
        Redis does."""
        val = r.hget(rkey, name)
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        return val

    @classmethod
    def get_cached(cls, name, group_id=None):
        """Read a single key from Redis cache. Returns (value, found).

        A cached miss reads as not found, like an uncached key.
        """
        r = cls._redis()
        if not r:
            return None, False
        try:
            val = cls._cache_read(r, cls._redis_key(group_id), name)
        except Exception:
            return None, False
        if val is None or val == CACHE_MISS:
            return None, False
        return val, True

    @classmethod
    def _query_db(cls, name, group_id=None):
        """Read a single key from DB. Returns (value, found); raises on error."""
        s = cls.objects.filter(key=name, group_id=group_id).first()
        if s is None:
            return None, False
        return s.get_value(), True

    @classmethod
    def get_from_db(cls, name, group_id=None):
        """Read a single key from DB. Returns (value, found)."""
        try:
            return cls._query_db(name, group_id=group_id)
        except Exception:
            return None, False

    @classmethod
    def resolve(cls, name, group=None, default=None, *, redis=_POOL):
        """
        Full lookup chain: group -> parent chain -> global. Each scope is read
        from Redis first and from the database only when Redis holds nothing.
        Returns the resolved value or default.

        The database answer is cached either way — the value, or CACHE_MISS
        when the scope has no row — so an unset key costs zero SQL after its
        first read. Three rules keep that correct:

        - The miss is cached per scope ("this scope has no row"), never as
          "the whole chain resolved to nothing". Setting a key on a parent
          after a child cached its miss therefore needs no descendant
          invalidation: the child's miss is still true, and the walk goes on
          to the parent's new value. (The alternative — hdel the name from
          every descendant hash — is unbounded and has nothing to fix.)
        - Reader writes use HSETNX. push_to_cache (every Setting.save) uses
          HSET, so a reader that raced a writer can never overwrite the
          value the writer just pushed with its stale miss.
        - A database error is not a miss and is never cached.

        Redis down (no client, or a command raises) means the database
        answers every scope, uncached — never an exception.

        `redis` is a test seam: pass a client, or None for "Redis is down".
        """
        r = cls._redis() if redis is _POOL else redis
        scopes = []
        if group is not None:
            try:
                from mojo.apps.account.services import group_hierarchy
                chain = group_hierarchy.ancestors(
                    group, include_self=True, max_depth=MAX_PARENT_DEPTH)
            except Exception:
                return default
            scopes = [current.pk for current in chain]
        scopes.append(None)
        for group_id in scopes:
            rkey = cls._redis_key(group_id)
            cached = None
            if r:
                try:
                    cached = cls._cache_read(r, rkey, name)
                except Exception:
                    # Stop asking a Redis that failed; the rest of the walk
                    # reads the database only.
                    r = None
            if cached == CACHE_MISS:
                continue
            if cached is not None:
                return cached
            try:
                val, found = cls._query_db(name, group_id=group_id)
            except Exception:
                continue
            if r:
                text = cls._cache_text(val) if found else CACHE_MISS
                try:
                    cls._cache_write(r, rkey, name, text, only_if_absent=True)
                except Exception:
                    pass  # uncached; the next read asks the database again
            if found:
                return val
        return default

    # ------------------------------------------------------------------
    # Class-level convenience
    # ------------------------------------------------------------------

    @classmethod
    def set(cls, key, value, is_secret=False, group=None):
        """Create or update a setting and push to Redis."""
        s, created = cls.objects.get_or_create(
            key=key, group=group,
            defaults={"is_secret": is_secret},
        )
        s.is_secret = is_secret
        s.set_value(value)
        s.save()
        s.push_to_cache()
        return s

    @classmethod
    def remove(cls, key, group=None):
        """Delete a setting and remove from Redis."""
        s = cls.objects.filter(key=key, group=group).first()
        if s:
            s._reject_protected_write()
            s.remove_from_cache()
            s.delete()
            return True
        return False

    # ------------------------------------------------------------------
    # Save / delete hooks
    # ------------------------------------------------------------------

    def _dedicated_writer_owns_row(self, protected_writer):
        """True when the caller proved it is THE writer for this exact key.

        The escape is deliberately per-key and stated twice: the writer names
        the key it believes it is saving, and the row must carry that same key.
        A shared helper handed a different protected row therefore cannot reuse
        somebody else's escape, and every other path — Setting.set, the generic
        REST surface, a shell save — still fails closed.
        """
        if not protected_writer or protected_writer != self.key:
            return False
        from mojo.apps.account.services import admin_settings
        return self.key in admin_settings.PROTECTED_WRITER_KEYS

    def save(self, *args, **kwargs):
        protected_writer = kwargs.pop("_protected_writer", None)
        skip_cache = kwargs.pop("_skip_cache", False)
        if not self._dedicated_writer_owns_row(protected_writer):
            self._reject_protected_write()
        self._reject_scope_change()
        self._validate_value()
        super().save(*args, **kwargs)
        if not skip_cache:
            self.push_to_cache()
        self._invalidate_geofence_decisions()

    def delete(self, *args, **kwargs):
        self._reject_protected_write()
        self.remove_from_cache()
        self._invalidate_geofence_decisions()
        super().delete(*args, **kwargs)

    def _invalidate_geofence_decisions(self):
        """A geofence rule/allowlist/posture edit must take effect immediately —
        a stale cached allow must not outlive an emergency block (e.g. a
        cached private_ip allow after an ALLOW_PRIVATE_IPS flip). Hooked at
        the model layer so Setting.set(), REST saves, and shell writes all
        count."""
        if self.group_id is not None or self.key not in self.GEOFENCE_KEYS:
            return
        try:
            from mojo.apps.account.services.geofence import cache as gf_cache
            gf_cache.invalidate_all()
        except Exception as exc:
            logit.error("geofence", f"decision-cache invalidation failed: {exc}")


# ---------------------------------------------------------------------------
# Built-in validators — the geofence-consumed keys. A typo'd value otherwise
# surfaces only at request time (rule_invalid denies, truthy-coerced posture
# booleans, nonsense fail-closed scopes). Heavy validators import lazily.
# ---------------------------------------------------------------------------

def _validate_geofence_rule(key, parsed):
    from mojo.apps.account.services.geofence.dsl import validate_rule
    validate_rule(parsed)


def _validate_geofence_allowlist(key, parsed):
    from mojo.apps.account.services.geofence.engine import validate_allowlist
    validate_allowlist(parsed)


def _validate_json_bool(key, parsed):
    # Strict parse only — kind="bool" would otherwise absorb an unrecognized
    # string at read time, and a posture flag must never be ambiguous.
    if not isinstance(parsed, bool):
        raise ValueError(f"{key} must be a JSON boolean (true/false)")


def _validate_cache_ttl(key, parsed):
    # NB: isinstance(True, int) is True — exclude bools explicitly.
    if isinstance(parsed, bool) or not isinstance(parsed, int) or parsed < 0:
        raise ValueError(f"{key} must be a non-negative JSON integer")


def _validate_scope_list(key, parsed):
    if not isinstance(parsed, list) or not all(
            isinstance(s, str) and s.strip() for s in parsed):
        raise ValueError(f"{key} must be a JSON list of non-empty strings")


Setting.register_validator("GEOFENCE_SYSTEM_RULES", _validate_geofence_rule)
Setting.register_validator("GEOFENCE_ALLOWLIST", _validate_geofence_allowlist)
Setting.register_validator("GEOFENCE_STRICT_POSTURE", _validate_json_bool)
Setting.register_validator("GEOFENCE_ENABLED", _validate_json_bool)
Setting.register_validator("GEOFENCE_FAIL_CLOSED", _validate_json_bool)
Setting.register_validator("GEOFENCE_ALLOW_PRIVATE_IPS", _validate_json_bool)
Setting.register_validator("GEOFENCE_CACHE_TTL", _validate_cache_ttl)
Setting.register_validator("GEOFENCE_FAIL_CLOSED_SCOPES", _validate_scope_list)
