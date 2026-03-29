import json
from django.db import models
from mojo.models import MojoModel, MojoSecrets
from mojo.helpers import logit

REDIS_GLOBAL_KEY = "settings:global"
REDIS_GROUP_PREFIX = "settings:g:"
MAX_PARENT_DEPTH = 10


class Setting(MojoSecrets, MojoModel):
    """
    Database-backed settings with optional encryption and group scoping.

    Lookup chain (via SettingsHelper):
        Redis cache -> DB (group -> parent chain -> global) -> django.conf.settings

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

    def on_rest_pre_save(self, changed_fields, created):
        """Encrypt secret values before saving via REST."""
        if self.is_secret and "value" in changed_fields:
            raw = self.value
            self.value = ""
            self.set_secret("value", raw)

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

    def push_to_cache(self):
        """Write this setting into the Redis hash for its scope."""
        r = self._redis()
        if not r:
            return
        rkey = self._redis_key(self.group_id)
        val = self.get_value()
        if val is None:
            r.hdel(rkey, self.key)
        else:
            r.hset(rkey, self.key, val if isinstance(val, str) else json.dumps(val))

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
                pipe.hset(rkey, s.key, val if isinstance(val, str) else json.dumps(val))
        pipe.execute()

    @classmethod
    def get_cached(cls, name, group_id=None):
        """Read a single key from Redis cache. Returns (value, found)."""
        r = cls._redis()
        if not r:
            return None, False
        val = r.hget(cls._redis_key(group_id), name)
        if val is None:
            return None, False
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        return val, True

    @classmethod
    def get_from_db(cls, name, group_id=None):
        """Read a single key from DB. Returns (value, found)."""
        try:
            s = cls.objects.filter(key=name, group_id=group_id).first()
            if s is None:
                return None, False
            return s.get_value(), True
        except Exception:
            return None, False

    @classmethod
    def resolve(cls, name, group=None, default=None):
        """
        Full lookup chain: Redis -> DB -> parent chain -> global.
        Returns the resolved value or default.
        """
        # Walk group + parent chain
        if group is not None:
            seen = set()
            current = group
            depth = 0
            while current and depth < MAX_PARENT_DEPTH:
                if current.pk in seen:
                    break
                seen.add(current.pk)

                val, found = cls.get_cached(name, group_id=current.pk)
                if found:
                    return val
                val, found = cls.get_from_db(name, group_id=current.pk)
                if found:
                    # Backfill cache
                    r = cls._redis()
                    if r:
                        r.hset(cls._redis_key(current.pk), name, val if isinstance(val, str) else json.dumps(val))
                    return val

                current = getattr(current, 'parent', None)
                if current and not hasattr(current, 'pk'):
                    # parent is a deferred FK — fetch it
                    try:
                        from mojo.apps.account.models import Group
                        current = Group.objects.filter(pk=current).first()
                    except Exception:
                        break
                depth += 1

        # Global scope
        val, found = cls.get_cached(name)
        if found:
            return val
        val, found = cls.get_from_db(name)
        if found:
            # Backfill global cache
            r = cls._redis()
            if r:
                r.hset(REDIS_GLOBAL_KEY, name, val if isinstance(val, str) else json.dumps(val))
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
            s.remove_from_cache()
            s.delete()
            return True
        return False

    # ------------------------------------------------------------------
    # Save / delete hooks
    # ------------------------------------------------------------------

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.push_to_cache()

    def delete(self, *args, **kwargs):
        self.remove_from_cache()
        super().delete(*args, **kwargs)
