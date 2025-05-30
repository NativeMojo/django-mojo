from django.db import models

class Passkey(models.Model):
    user = models.ForeignKey(
        "account.User",related_name="members",
        on_delete=models.CASCADE)
    token = models.TextField()

    credential_id = models.CharField(max_length=255, unique=True)
    rp_id = models.CharField(max_length=255, null=False, db_index=True)
    is_enabled = models.BooleanField(default=True, db_index=True)

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)
    last_used = models.DateTimeField(null=True, blank=True, default=None)

    def __str__(self):
        return f"{self.user.username} - {self.credential_id} - {self.rp_id}"
