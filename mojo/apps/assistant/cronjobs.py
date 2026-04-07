from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers.settings import settings


# Nightly memory cleanup — runs mechanical cleanup + optional LLM dreaming
@schedule(minutes="0", hours="3")
def memory_cleanup(force=False, verbose=False, now=None):
    if not settings.get("LLM_ADMIN_MEMORY_ENABLED", True, kind="bool"):
        return
    jobs.publish(
        func="mojo.apps.assistant.jobs.assistant_memory_cleanup",
        channel="cleanup",
        payload={},
    )
