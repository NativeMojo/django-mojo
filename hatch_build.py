"""Bind the qualifying wheel to committed source without editing the checkout."""
import hashlib
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        spec = importlib.util.spec_from_file_location(
            "acme_build_identity", root / "mojo/apps/dnsman/acme_hub_feature.py")
        feature = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(feature)
        feature.release(self.metadata.version)
        source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        if not feature.HEX40.fullmatch(source):
            raise RuntimeError("ACME build source identity unavailable")
        # Editable installs need no release identity. They remain explicitly
        # unqualified. Release wheels require the exact committed critical set.
        if version == "editable":
            return
        critical = ["mojo/" + name for name in feature.CRITICAL_FILES]
        subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *critical], cwd=root, check=True)
        identity = dict(epoch=feature.EPOCH, source_sha=source,
                        manifest_sha256=feature.manifest(), release=self.metadata.version)
        self._identity = identity
        self._temporary = tempfile.TemporaryDirectory(prefix="mojo-acme-wheel-")
        path = Path(self._temporary.name) / "build.json"
        path.write_text(json.dumps(identity, sort_keys=True) + "\n")
        build_data.setdefault("force_include", {})[str(path)] = "mojo/apps/dnsman/_acme_hub_build.json"

    def finalize(self, version, build_data, artifact_path):
        if getattr(self, "_temporary", None) is not None:
            try:
                # Wheel hash is an outer release receipt, never self-embedded.
                value = dict(self._identity, wheel_sha256=hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest())
                print("ACME qualifying wheel identity: " + json.dumps(value, sort_keys=True))
            finally:
                self._temporary.cleanup()
