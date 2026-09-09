"""App-owned runner capabilities with background probes and expiring proofs."""
import threading
import time

from mojo.helpers import logit

_providers = {}


def validate_firewall_runner(channels=None, direct=None):
    """Candidate contract for an enrolled deployment's normal API engine."""
    from mojo.apps.jobs import JOB_CHANNELS
    from mojo.helpers.settings import settings
    channels = JOB_CHANNELS if channels is None else channels
    direct = settings.get_static("JOBS_HOSTNAME_CHANNEL", True) if direct is None else direct
    if not isinstance(channels, (list, tuple)) or "firewall" not in channels or direct is not True:
        raise ValueError("enrolled firewall runner requires firewall and its box-direct channel")
    return True


def register(name, probe, on_ready=None):
    if name == "execute_checked":
        raise ValueError("execute_checked is owned by the jobs protocol")
    _providers[name] = (probe, on_ready)


class CapabilityCache:
    def __init__(self, engine, providers=None, clock=None, ttl=60):
        self.engine = engine
        self.providers = dict(_providers if providers is None else providers)
        self.clock = clock or time.monotonic
        self.ttl = ttl
        self.values = {}
        self.lock = threading.Lock()
        self.thread = None

    def snapshot(self):
        now = self.clock()
        with self.lock:
            return {name: 1 for name, expires in self.values.items() if expires > now}

    def refresh(self):
        for name, (probe, callback) in self.providers.items():
            try:
                result = probe(self.engine)
                ready = isinstance(result, dict) and result.get("ready") is True
            except Exception:
                ready = False
                logit.exception("runner capability probe failed: %s", name)
            now = self.clock()
            with self.lock:
                was_ready = self.values.get(name, 0) > now
                if ready:
                    self.values[name] = now + self.ttl
                else:
                    self.values.pop(name, None)
            if ready and not was_ready and callback:
                try:
                    callback(self.engine)
                except Exception:
                    # Retry the recovery callback on the next probe. Its own
                    # durable marker prevents duplicate work after a lost reply.
                    with self.lock:
                        self.values.pop(name, None)
                    logit.exception("runner capability recovery failed: %s", name)

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.engine.stop_event.is_set():
                self.refresh()
                self.engine.stop_event.wait(min(15, self.ttl / 2))
        self.thread = threading.Thread(target=run, name="RunnerCapabilities", daemon=True)
        self.thread.start()
