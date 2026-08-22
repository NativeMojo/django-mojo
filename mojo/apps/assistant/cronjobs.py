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


# Nightly approval sweep — persists lapsed pending/executing states and prunes
# terminal rows. Not a correctness dependency: effective_state() answers lazily.
@schedule(minutes="15", hours="3")
def approval_sweep(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.assistant.jobs.assistant_approval_sweep",
        channel="cleanup",
        payload={},
    )
