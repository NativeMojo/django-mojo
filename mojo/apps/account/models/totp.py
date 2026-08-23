from django.db import models

from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets


class UserTOTP(MojoSecrets, MojoModel):
    """
    TOTP (Time-based One-Time Password) credential for a user.

    Secret stored in mojo_secrets — never exposed via API.
    One record per user; re-setup overwrites the existing record.
    """

    user = models.OneToOneField(
        "account.User",
        related_name="totp",
        on_delete=models.CASCADE,
    )
    is_enabled = models.BooleanField(default=False, db_index=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["owner", "manage_users", "users"]
        SAVE_PERMS = ["owner", "manage_users", "users"]
        OWNER_FIELD = "user"
        # A TOTP record must always bind to request.user — never body-settable.
        # Framework auto-stamps user from request via CREATED_BY_OWNER_FIELD.
        NO_SAVE_FIELDS = ["user"]
        NO_SHOW_FIELDS = ["mojo_secrets"]
        GRAPHS = {
            "default": {
                "fields": ["id", "is_enabled", "created", "modified"],
            }
        }

    def __str__(self):
        return f"{self.user.username} TOTP ({'enabled' if self.is_enabled else 'disabled'})"

    def generate_recovery_codes(self):
        """Generate 8 recovery codes, store bcrypt hashes in mojo_secrets, return plaintext list."""
        import secrets
        import bcrypt
        from mojo.helpers.settings import settings
        # Cost is tunable for test projects (maestro #2789); production keeps
        # bcrypt's default 12. Clamped to bcrypt's own valid range (4..31) so
        # a typo'd value can neither raise from gensalt() nor burn minutes of
        # CPU per enrollment.
        try:
            rounds = min(31, max(4, int(settings.get_static("TOTP_RECOVERY_BCRYPT_ROUNDS", 12))))
        except Exception:
            rounds = 12
        codes = []
        stored = []
        for _ in range(8):
            raw = secrets.token_hex(6)  # 12 hex chars
            code = f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}"
            hashed = bcrypt.hashpw(code.encode(), bcrypt.gensalt(rounds)).decode()
            codes.append(code)
            stored.append({"hash": hashed, "hint": raw[:4]})
        self.set_secret("recovery_codes", stored)
        self.save(update_fields=["mojo_secrets", "modified"])
        return codes

    def get_masked_recovery_codes(self):
        """Return masked codes and remaining count."""
        stored = self.get_secret("recovery_codes") or []
        masked = [f"{entry['hint']}-xxxx-xxxx" for entry in stored]
        return {"remaining": len(stored), "codes": masked}

    def verify_and_consume_recovery_code(self, code):
        """Check code against stored hashes, consume atomically if valid. Returns True/False."""
        import bcrypt
        stored = self.get_secret("recovery_codes") or []
        if not stored:
            return False
        code_clean = code.strip().lower()
        for i, entry in enumerate(stored):
            if bcrypt.checkpw(code_clean.encode(), entry["hash"].encode()):
                stored.pop(i)
                self.set_secret("recovery_codes", stored)
                self.save(update_fields=["mojo_secrets", "modified"])
                return True
        return False
