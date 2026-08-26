import json
from pathlib import Path
import subprocess
import sys
import tempfile

from testit import helpers as th


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build_mojoland_lab_wheel.py"
APPROVED_BASE = "3b9763b327fed7a5081eb08211df6ea618fbf74a"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


@th.django_unit_test()
def test_builds_exact_private_wheel(opts):
    source_sha = _git(REPO, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory() as output_dir:
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--source-sha", source_sha,
                "--base-sha", APPROVED_BASE, "--output-dir", output_dir,
                "--builder-identity", "test-builder",
            ],
            cwd=REPO, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, "exact revision build failed: {}".format(result.stderr)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        manifest = json.loads(Path(output["manifest"]).read_text())
        assert manifest["source"]["commit"] == source_sha, "manifest lost the exact source SHA"
        assert manifest["source"]["base_commit"] == APPROVED_BASE, "manifest lost the approved base"
        assert manifest["source"]["migration_check"] == "passed", "manifest did not prove migration exclusion"
        assert manifest["version"].endswith("+mojoland.g{}".format(source_sha)), "wheel lacks the exact local version"
        assert Path(output["wheel"]).is_file(), "builder did not emit the declared wheel"


@th.django_unit_test()
def test_refuses_abbreviated_source_sha(opts):
    source_sha = _git(REPO, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory() as output_dir:
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--source-sha", source_sha[:12],
                "--base-sha", APPROVED_BASE, "--output-dir", output_dir,
            ],
            cwd=REPO, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert result.returncode != 0, "builder accepted an abbreviated source SHA"
        assert "exact 40-character" in result.stderr, "abbreviated-SHA rejection was not explicit"


@th.django_unit_test()
def test_refuses_migration_diff(opts):
    with tempfile.TemporaryDirectory() as repo_name, tempfile.TemporaryDirectory() as output_dir:
        repo = Path(repo_name)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Test Builder")
        (repo / "README.md").write_text("base\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-qm", "base")
        base_sha = _git(repo, "rev-parse", "HEAD")
        migration = repo / "mojo" / "apps" / "sample" / "migrations" / "0001_initial.py"
        migration.parent.mkdir(parents=True)
        migration.write_text("# forbidden\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "migration")
        source_sha = _git(repo, "rev-parse", "HEAD")
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--repo", str(repo),
                "--source-sha", source_sha, "--base-sha", base_sha,
                "--output-dir", output_dir,
            ],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert result.returncode != 0, "builder accepted a migration-bearing candidate"
        assert "migrations" in result.stderr, "migration refusal did not name the rejected path"
