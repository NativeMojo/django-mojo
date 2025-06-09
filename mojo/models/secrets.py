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
