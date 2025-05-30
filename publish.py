#!/usr/bin/env python

import subprocess
import re
import sys
import json
from datetime import datetime
CHANGELOG_FILE = "CHANGELOG.md"


def run_command(command, text=True, capture_output=True):
    result = subprocess.run(command, shell=True, text=text, capture_output=capture_output)
    if result.returncode != 0:
        raise Exception(f"Command failed: {command}\n{result.stderr}")
    if capture_output:
        return result.stdout.strip()
    return result


def update_changelog(notes):
    lines = []
    with open(CHANGELOG_FILE, 'r') as f:
        lines = f.read().split('\n')

    lines.insert(2, notes)
    with open(CHANGELOG_FILE, 'w') as f:
        f.write("\n".join(lines))

if '--nobump' not in sys.argv:
    # Update the version using poetry
    run_command("poetry version patch")

# Extract the new version from pyproject.toml
with open("pyproject.toml", "r") as file:
    content = file.read()

version_match = re.search(r'version\s*=\s*"([^"]+)"', content)
if version_match:
    version = version_match.group(1)
else:
    raise Exception("Version not found in pyproject.toml")

str_date = datetime.now().strftime("%B %d, %Y")
notes = [f"## v{version} - {str_date}", ""]
empty = 0
print("Enter Release Notes (double return)")
while empty < 2:
    note = input()
    if note == "":
        empty += 1
    else:
        notes.append(note)
notes.append("")
notes.append("")

update_changelog("\n".join(notes))

print(f"publishing: {version}")
# Update the __version__ in mojo/__init__.py
init_file = "mojo/__init__.py"
with open(init_file, "r") as file:
    init_content = file.read()

new_init_content = re.sub(r'^__version__\s*=\s*".*"$', f'__version__ = "{version}"', init_content, flags=re.MULTILINE)

with open(init_file, "w") as file:
    file.write(new_init_content)

# Build and publish using poetry
if '--nopypi' not in sys.argv:
    run_command("poetry build", capture_output=False)
    run_command("poetry publish", capture_output=False)

run_command("git add .", capture_output=False)
git_command = ["git", "commit"]
for line in notes:
    if line and len(line) > 1:
        git_command.extend(["-m", f'"{line}"'])
run_command(" ".join(git_command), capture_output=False)
run_command("git push", capture_output=False)

if '--norelease' not in sys.argv:
    # Create GitHub release
    release_notes = "\n".join([note for note in notes if note and len(note) > 1])
    release_data = {
        "tag_name": f"v{version}",
        "name": f"v{version}",
        "body": release_notes,
        "draft": False,
        "prerelease": False
    }

    # Use GitHub CLI to create release
    gh_command = f'gh release create v{version} --title "v{version}" --notes "{release_notes}"'
    run_command(gh_command, capture_output=False)
