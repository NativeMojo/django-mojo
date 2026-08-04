from mojo.helpers import logit
from mojo.helpers.settings import settings


logger = logit.get_logger(__name__, "incident.log")


def resolve_incident(incident, status="resolved", note=None, kind="status_changed"):
    """Apply the complete programmatic incident-resolution contract."""
    if status not in ("resolved", "closed"):
        status = "resolved"
    if incident.status in ("resolved", "closed"):
        return True

    old_status = incident.status
    incident.status = status
    incident.save(update_fields=["status"])
    incident.add_history(
        kind,
        note=note or f"Status changed from {old_status} to {status}",
    )

    if status == "resolved":
        try:
            from mojo.apps import metrics
            if settings.INCIDENT_EVENT_METRICS:
                metrics.record(
                    "incidents:resolved",
                    account="incident",
                    min_granularity=settings.get_static(
                        "INCIDENT_METRICS_MIN_GRANULARITY", "hours"
                    ),
                )
        except Exception:
            logger.exception("Failed to record resolved metric for incident %s", incident.pk)

    from mojo.apps.incident.services import maestro_sync
    for link_id in incident.maestro_links.values_list("id", flat=True):
        maestro_sync.enqueue_sync(link_id, ["status"])

    incident.check_delete_on_resolution()
    return True

