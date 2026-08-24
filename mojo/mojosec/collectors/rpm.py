"""Command-free system-Python integrity under the legacy ``rpm`` tier key."""

import os
import site
import sys
import sysconfig

from .fim import FimCollector


_APPROVED_PREFIXES = (
    "/usr/lib/", "/usr/lib64/", "/usr/local/lib/", "/usr/local/lib64/",
)
_APPROVED_SUFFIXES = ("/site-packages", "/dist-packages")


class SystemPythonError(RuntimeError):
    """A fixed readiness or collection failure for the compatibility tier."""


def _running_python_roots(config):
    configured = os.path.realpath(config["interpreter"])
    running = os.path.realpath(sys.executable)
    if configured != running:
        raise SystemPythonError(
            "configured system Python does not match the running interpreter")
    candidates = set()
    candidates.update(
        value for value in sysconfig.get_paths().values()
        if isinstance(value, str))
    candidates.update(
        value for value in site.getsitepackages()
        if isinstance(value, str))
    candidates.update(value for value in sys.path if isinstance(value, str))
    return sorted(candidates)


def discover_system_python_roots(config, roots_provider=None):
    """Return a bounded, strictly filtered in-process site-package root set."""
    provider = roots_provider or (lambda: _running_python_roots(config))
    try:
        candidates = provider()
    except (AttributeError, OSError, TypeError, ValueError) as err:
        raise SystemPythonError("system Python root discovery failed") from err
    if not isinstance(candidates, (list, tuple, set)) or len(candidates) > 128:
        raise SystemPythonError("system Python returned an unbounded site-root set")
    roots = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not os.path.isabs(candidate):
            continue
        path = os.path.realpath(candidate)
        if (not path.startswith(_APPROVED_PREFIXES) or
                not path.endswith(_APPROVED_SUFFIXES)):
            continue
        if path not in roots:
            roots.append(path)
    if not roots:
        raise SystemPythonError("system Python has no approved site-packages root")
    return sorted(roots)


def probe_system_python_capability(config, roots_provider=None):
    """Prove the configured running interpreter exposes approved roots."""
    discover_system_python_roots(config, roots_provider=roots_provider)
    return True


class SystemPythonCollector:
    """Descriptor-safe FIM for system-Python roots; persisted as tier ``rpm``."""

    name = "rpm"

    def __init__(self, config, identity, expected_changes_path=None,
                 roots_provider=None, fim_factory=None):
        self.config = config
        self.identity = dict(identity)
        self.expected_changes_path = expected_changes_path
        self.roots_provider = roots_provider
        self.fim_factory = fim_factory or FimCollector
        self.site_roots = []
        self.baseline_key = ":".join((identity["name"], identity["digest"], "rpm"))

    def discover_site_roots(self):
        self.site_roots = discover_system_python_roots(
            self.config, roots_provider=self.roots_provider)
        return list(self.site_roots)

    def scan(self, previous=None):
        roots = self.discover_site_roots()
        walker = self.fim_factory(
            {
                "targets": [
                    {"path": root, "recursive": True, "optional": False}
                    for root in roots
                ],
                "max_entries": self.config["max_entries"],
                "max_file_bytes": self.config["max_file_bytes"],
                "max_depth": self.config["max_depth"],
            },
            self.expected_changes_path,
            self.identity,
            "rpm",
        )
        scan = walker.scan(previous)
        scan["baseline_key"] = self.baseline_key
        scan["profile"] = self.identity["digest"]
        scan["tier"] = "rpm"
        scan["site_roots"] = roots
        scan["packages"] = 0
        scan["anomalies"] = sum(
            bool(entry.get("anomaly")) for entry in scan["snapshot"].values())
        return scan

    def diff(self, baseline, scan):
        helper = FimCollector(
            {"targets": [], "max_entries": 1, "max_file_bytes": 0, "max_depth": 1},
            self.expected_changes_path, self.identity, "rpm",
        )
        return helper.diff(baseline, scan)
