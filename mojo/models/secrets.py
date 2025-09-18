from django.db import models
from mojo.helpers import crypto
from mojo.helpers.settings import settings
from objict import objict, merge_dicts


class MojoSecrets(models.Model):
    """Base model class for adding secrets to a model"""
    class Meta:
        abstract = True

    mojo_secrets = models.TextField(blank=True, null=True, default=None)
    _exposed_secrets = None
    _secrets_changed = False

    def set_secrets(self, value):
        self.debug("Setting secrets", repr(value))
        if isinstance(value, str):
            value = objict.from_json(value)
        self._exposed_secrets = merge_dicts(self.secrets, value)
        self._secrets_changed = True

    def set_secret(self, key, value):
        if value is None:
            self.secrets.pop(key, None)
        else:
            self.secrets[key] = value
        self._secrets_changed = True

    def get_secret(self, key, default=None):
        return self.secrets.get(key, default)

    def clear_secrets(self):
        self.mojo_secrets = None
        self._exposed_secrets = objict()
        self._secrets_changed = True

    @property
    def secrets(self):
        if self._exposed_secrets is not None:
            return self._exposed_secrets
        if self.mojo_secrets is None:
            self._exposed_secrets = objict()
            return self._exposed_secrets
        if self._exposed_secrets is None:
            self._exposed_secrets = crypto.decrypt(self.mojo_secrets, self._get_secrets_password(), False)
        return self._exposed_secrets

    def _get_secrets_password(self):
        # override this to create your own secrets password
        salt = f"{self.pk}{self.__class__.__name__}"
        if hasattr(self, 'created'):
            return f"{self.created}{salt}"
        return salt

    def save_secrets(self):
        if self._secrets_changed:
            if self._exposed_secrets:
                self.mojo_secrets = crypto.encrypt( self._exposed_secrets, self._get_secrets_password())
            else:
                self.mojo_secrets = None
            self._secrets_changed = False

    def save(self, *args, **kwargs):
        if self.pk is not None:
            self.save_secrets()
            super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)
            self.save_secrets()
            super().save()



class KSMSecrets(models.Model):
    """Base model class for adding secrets to a model using AWS KMS (envelope encryption)."""
    class Meta:
        abstract = True

    mojo_secrets = models.TextField(blank=True, null=True, default=None)
    _exposed_secrets = None
    _secrets_changed = False
    _kms_cache = {}

    def set_secrets(self, value):
        self.debug("Setting secrets", repr(value))
        if isinstance(value, str):
            value = objict.from_json(value)
        self._exposed_secrets = merge_dicts(self.secrets, value)
        self._secrets_changed = True

    def set_secret(self, key, value):
        self.secrets[key] = value
        self._secrets_changed = True

    def get_secret(self, key, default=None):
        return self.secrets.get(key, default)

    def clear_secrets(self):
        self.mojo_secrets = None
        self._exposed_secrets = objict()
        self._secrets_changed = True

    @property
    def secrets(self):
        if self._exposed_secrets is not None:
            return self._exposed_secrets
        if self.mojo_secrets is None or self.pk is None:
            self._exposed_secrets = objict()
            return self._exposed_secrets
        if self._exposed_secrets is None:
            try:
                data = self._get_kms().decrypt_dict_field(self._kms_context(), self.mojo_secrets)
                # convert to objict mapping
                self._exposed_secrets = objict.from_dict(data)
            except Exception:
                # On failure, expose empty to avoid leaking details
                self._exposed_secrets = objict()
        return self._exposed_secrets

    def save_secrets(self):
        if self._secrets_changed:
            if self._exposed_secrets:
                # objict behaves like a dict; KMSHelper accepts dict
                blob = self._get_kms().encrypt_field(self._kms_context(), self._exposed_secrets)
                self.mojo_secrets = blob
            else:
                self.mojo_secrets = None
            self._secrets_changed = False

    def save(self, *args, **kwargs):
        if self.pk is not None:
            self.save_secrets()
            super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)
            self.save_secrets()
            super().save()

    @classmethod
    def _get_kms(cls):
        kms_key_id = settings.get("KMS_KEY_ID", None)
        region = settings.get("AWS_REGION", settings.get("AWS_DEFAULT_REGION", "us-east-1"))
        if not kms_key_id:
            raise RuntimeError("KMS_KEY_ID must be configured to use KSMSecrets")
        cache_key = (kms_key_id, region)
        helper = cls._kms_cache.get(cache_key)
        if helper is None:
            from mojo.helpers.aws.kms import KMSHelper
            helper = KMSHelper(kms_key_id=kms_key_id, region_name=region)
            cls._kms_cache[cache_key] = helper
        return helper

    def _kms_context(self):
        # Bind to app_label.Model.<pk>.mojo_secrets for contextual integrity
        app_label = getattr(self._meta, "app_label", self.__class__.__module__.split(".")[0])
        return f"{app_label}.{self.__class__.__name__}.{self.pk}.mojo_secrets"
