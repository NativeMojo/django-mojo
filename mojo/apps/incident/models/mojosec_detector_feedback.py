from django.db import models

from mojo.models import MojoModel


class MojoSecDetectorFeedback(models.Model, MojoModel):
    """Append-only human disposition for one MojoSec learning subject."""

    CONFIRMED_THREAT = "confirmed_threat"
    EXPECTED_ADMINISTRATIVE = "expected_administrative"
    BENIGN_NOISE = "benign_noise"
    OPERATIONAL_FAILURE = "operational_failure"
    UNKNOWN = "unknown"
    MISSED_INCOMPLETE = "missed_incomplete"
    DISPOSITIONS = (
        (CONFIRMED_THREAT, "Confirmed threat"),
        (EXPECTED_ADMINISTRATIVE, "Expected administrative activity"),
        (BENIGN_NOISE, "Benign noise"),
        (OPERATIONAL_FAILURE, "Operational failure"),
        (UNKNOWN, "Unknown"),
        (MISSED_INCOMPLETE, "Missed or incomplete detection"),
    )

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    author = models.ForeignKey(
        "account.User", null=True, blank=True, default=None,
        related_name="+", on_delete=models.SET_NULL)
    receipt = models.ForeignKey(
        "incident.MojoSecReceipt", null=True, blank=True, default=None,
        related_name="detector_feedback", on_delete=models.SET_NULL)
    incident = models.ForeignKey(
        "incident.Incident", null=True, blank=True, default=None,
        related_name="mojosec_detector_feedback", on_delete=models.SET_NULL)
    reverses = models.OneToOneField(
        "self", null=True, blank=True, default=None,
        related_name="reversed_by", on_delete=models.PROTECT)

    disposition = models.CharField(max_length=32, choices=DISPOSITIONS, db_index=True)
    note = models.CharField(max_length=1000, blank=True, default="")
    subject_type = models.CharField(
        max_length=16,
        choices=(("receipt", "Receipt"), ("incident", "Incident"), ("manual", "Manual")),
        db_index=True)
    subject_key = models.CharField(max_length=160, db_index=True)
    subject_id_snapshot = models.CharField(max_length=128)
    manual_exemplar = models.JSONField(default=dict, blank=True)

    # Immutable scalar snapshots survive later Event/Incident lifecycle changes.
    author_id_snapshot = models.PositiveBigIntegerField()
    installation_key_id_snapshot = models.PositiveBigIntegerField(null=True, default=None)
    detector_kind = models.CharField(max_length=64, db_index=True)
    detector_category = models.CharField(max_length=124, blank=True, default="")
    detector_level = models.PositiveSmallIntegerField(default=0)
    sensor_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    sensor_policy_revision_sha256 = models.CharField(max_length=64, blank=True, default="")
    observed_count = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (models.Q(subject_type="receipt") & models.Q(incident__isnull=True)
                     & models.Q(manual_exemplar={}))
                    | (models.Q(subject_type="incident") & models.Q(receipt__isnull=True)
                       & models.Q(manual_exemplar={}))
                    | (models.Q(subject_type="manual") & models.Q(receipt__isnull=True)
                       & models.Q(incident__isnull=True) & ~models.Q(manual_exemplar={}))
                ),
                name="incident_msf_exact_subject",
            ),
        ]
        indexes = [
            models.Index(
                fields=("detector_kind", "created"),
                name="incident_msf_kind_created_idx"),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_security", "manage_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        CAN_CREATE = False
        CAN_UPDATE = False
        CAN_DELETE = False
        DENY_AI = True
        SENSITIVE_FIELDS = ["note", "manual_exemplar"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("MojoSec detector feedback is append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("MojoSec detector feedback is append-only")
