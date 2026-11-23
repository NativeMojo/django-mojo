"""The runner's URL conf warm-up before parallel threads (#11810).

Two parallel threads making the first import of two submodules of one app's
`rest` package could deadlock on CPython's module locks, because each
`rest/__init__.py` star-imports its children. The runner now imports the URL
conf in the main thread first. The import check runs in a fresh interpreter:
this test process has already imported every rest package.
"""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout

from testit import helpers as th

TESTIT_TIER = "core"


def _repo_root():
    import testit
    return os.path.dirname(os.path.dirname(os.path.abspath(testit.__file__)))


@th.unit_test("warm-up imports every installed app's rest package")
def test_warm_imports_loads_rest_packages(opts):
    from mojo.helpers import paths

    config_root = str(paths.CONFIG_ROOT)
    script = (
        "import importlib.util, json, os, sys\n"
        f"sys.path.insert(0, {_repo_root()!r})\n"
        f"sys.path.insert(0, {config_root!r})\n"
        f"os.environ['DJANGO_SETTINGS_MODULE'] = {os.environ.get('DJANGO_SETTINGS_MODULE', 'settings')!r}\n"
        "from mojo.helpers import paths\n"
        f"paths.configure_paths(os.path.join({config_root!r}, 'settings', '__init__.py'), 1)\n"
        "import django\n"
        "django.setup()\n"
        "from django.apps import apps\n"
        "from testit import runner\n"
        "before = 'mojo.apps.account.rest' in sys.modules\n"
        "runner._warm_imports(None)\n"
        "expected = [c.name + '.rest' for c in apps.get_app_configs()\n"
        "            if importlib.util.find_spec(c.name + '.rest') is not None]\n"
        "print(json.dumps({'before': before, 'expected': expected,\n"
        "                  'missing': [m for m in expected + ['mojo.apps.account.rest.oauth',\n"
        "                              'mojo.apps.account.rest.bouncer.views']\n"
        "                              if m not in sys.modules]}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, \
        f"the warm-up subprocess failed (rc={proc.returncode}): {proc.stderr[-2000:]}"
    assert "warm-up failed" not in proc.stdout, \
        f"the warm-up warned instead of importing: {proc.stdout[-2000:]}"

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["before"] is False, \
        "control: account rest was already imported before the warm-up, so the test proves nothing"
    assert "mojo.apps.account.rest" in payload["expected"], \
        f"control: account should be an installed app with a rest package, got {payload['expected']}"
    assert not payload["missing"], \
        f"the warm-up left these rest modules unimported: {payload['missing']}"


@th.unit_test("a failing warm-up warns and returns")
def test_warm_imports_failure_only_warns(opts):
    from testit import runner

    def broken_resolver():
        raise ImportError("TESTIT_WARM_PROBE no module named nowhere")

    out = io.StringIO()
    with redirect_stdout(out):
        result = runner._warm_imports(None, resolver=broken_resolver)

    assert result is None, f"the warm-up must return normally, got {result!r}"
    text = out.getvalue()
    assert "warm-up failed" in text and "ImportError" in text \
        and "TESTIT_WARM_PROBE" in text, \
        f"the warm-up must print one warning naming the error, printed {text!r}"
    warnings = [line for line in text.splitlines() if "warm-up failed" in line]
    assert len(warnings) == 1, \
        f"the warm-up must print exactly one warning line, printed {text!r}"
