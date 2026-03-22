#!/usr/bin/env python
"""
Publishing script for django-mojo package.

This script handles version bumping, changelog updates, building, publishing to PyPI,
and creating git commits and releases.
"""

import os
import argparse
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load .env if present
_env_file = Path(".env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _val.strip())

# Constants
CHANGELOG_FILE = Path("CHANGELOG.md")
PYPROJECT_FILE = Path("pyproject.toml")
INIT_FILE = Path("mojo/__init__.py")


class PublishError(Exception):
    """Custom exception for publishing errors."""
    pass


def run_command(command: str, capture_output: bool = True) -> Optional[str]:
    """
    Run a shell command with proper error handling.

    Args:
        command: The shell command to run
        capture_output: Whether to capture and return stdout

    Returns:
        Command output if capture_output is True, None otherwise

    Raises:
        PublishError: If the command fails
    """
    logger.info(f"Running command: {command}")

    try:
        result = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=capture_output,
            timeout=300  # 5 minute timeout
        )

        if result.returncode != 0:
            error_msg = f"Command failed: {command}"
            if result.stderr:
                error_msg += f"\nError: {result.stderr}"
            raise PublishError(error_msg)

        if capture_output:
            return result.stdout.strip()
        return None

    except subprocess.TimeoutExpired:
        raise PublishError(f"Command timed out: {command}")
    except Exception as e:
        raise PublishError(f"Failed to execute command '{command}': {str(e)}")


def validate_uv_environment() -> None:
    """Validate that uv is available, pyproject.toml exists, and PyPI token is set."""
    if not os.environ.get("UV_PUBLISH_TOKEN"):
        raise PublishError("UV_PUBLISH_TOKEN is not set. Add it to your .env file.")

    try:
        run_command("uv --version")
    except PublishError:
        raise PublishError("uv is not installed or not in PATH")

    if not PYPROJECT_FILE.exists():
        raise PublishError("pyproject.toml not found")


def validate_files_exist() -> None:
    """Validate that required files exist."""
    required_files = [CHANGELOG_FILE, PYPROJECT_FILE, INIT_FILE]
    missing_files = [f for f in required_files if not f.exists()]

    if missing_files:
        raise PublishError(f"Missing required files: {', '.join(str(f) for f in missing_files)}")


def get_current_version() -> str:
    """
    Extract the current version from pyproject.toml.

    Returns:
        The current version string

    Raises:
        PublishError: If version cannot be found
    """
    if not PYPROJECT_FILE.exists():
        raise PublishError(f"{PYPROJECT_FILE} not found")

    try:
        content = PYPROJECT_FILE.read_text(encoding='utf-8')
        version_match = re.search(r'version\s*=\s*"([^"]+)"', content)

        if not version_match:
            raise PublishError("Version not found in pyproject.toml")

        return version_match.group(1)
    except Exception as e:
        raise PublishError(f"Failed to read version from {PYPROJECT_FILE}: {str(e)}")


def bump_version() -> str:
    """Bump the patch version in pyproject.toml and return the new version string."""
    logger.info("Bumping version...")
    current = get_current_version()
    major, minor, patch = current.split(".")
    new_version = f"{major}.{minor}.{int(patch) + 1}"
    content = PYPROJECT_FILE.read_text(encoding='utf-8')
    new_content = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{new_version}"', content, count=1, flags=re.MULTILINE)
    PYPROJECT_FILE.write_text(new_content, encoding='utf-8')
    logger.info(f"Version bumped: {current} -> {new_version}")
    return new_version


def get_release_notes() -> List[str]:
    """
    Collect release notes from user input.

    Returns:
        List of release note lines
    """
    logger.info("\n======\nPlease enter release notes (press Enter twice when done):")
    notes = []
    empty_lines = 0

    try:
        with open("/dev/tty") as tty:
            while empty_lines < 2:
                note = tty.readline().strip()
                if not note:
                    empty_lines += 1
                else:
                    notes.append(note)
                    empty_lines = 0
    except (KeyboardInterrupt, EOFError):
        logger.info("\nRelease notes collection cancelled")
        sys.exit(1)

    return notes


def update_changelog(version: str, notes: List[str]) -> None:
    """
    Update the changelog with new version notes.

    Args:
        version: The new version string
        notes: List of release note lines
    """
    logger.info("Updating changelog...")

    try:
        if not CHANGELOG_FILE.exists():
            # Create a basic changelog if it doesn't exist
            CHANGELOG_FILE.write_text("# Changelog\n\n", encoding='utf-8')

        lines = CHANGELOG_FILE.read_text(encoding='utf-8').splitlines()

        # Prepare changelog entry
        date_str = datetime.now().strftime("%B %d, %Y")
        changelog_entry = [f"## v{version} - {date_str}", ""]
        changelog_entry.extend(notes)
        changelog_entry.extend(["", ""])

        # Insert after the header (typically line 1)
        insert_position = min(2, len(lines))
        lines[insert_position:insert_position] = changelog_entry

        CHANGELOG_FILE.write_text("\n".join(lines), encoding='utf-8')

    except Exception as e:
        raise PublishError(f"Failed to update changelog: {str(e)}")


def update_init_version(version: str) -> None:
    """
    Update the __version__ in mojo/__init__.py.

    Args:
        version: The new version string
    """
    logger.info("Updating __init__.py version...")

    try:
        if not INIT_FILE.exists():
            raise PublishError(f"{INIT_FILE} not found")

        content = INIT_FILE.read_text(encoding='utf-8')
        new_content = re.sub(
            r'^__version__\s*=\s*".*"$',
            f'__version__ = "{version}"',
            content,
            flags=re.MULTILINE
        )

        if content == new_content:
            logger.warning("No __version__ line found or updated in __init__.py")

        INIT_FILE.write_text(new_content, encoding='utf-8')

    except Exception as e:
        raise PublishError(f"Failed to update {INIT_FILE}: {str(e)}")


def build_and_publish() -> None:
    """Build and publish the package to PyPI."""
    logger.info("Cleaning dist/...")
    run_command("rm -rf dist/", capture_output=False)

    logger.info("Building package...")
    run_command("uv build", capture_output=False)

    logger.info("Publishing to PyPI...")
    token = os.environ.get("UV_PUBLISH_TOKEN")
    try:
        result = subprocess.run(
            ["uv", "publish", "--token", token],
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise PublishError("uv publish failed")
    except subprocess.TimeoutExpired:
        raise PublishError("uv publish timed out")


def commit_changes(version: str, notes: List[str]) -> None:
    """
    Commit changes to git.

    Args:
        version: The version string
        notes: List of release note lines
    """
    logger.info("Committing changes...")

    run_command("git add .", capture_output=False)

    # Create single multi-line commit message
    commit_lines = [f"Release v{version}"]
    commit_lines.extend(note for note in notes if note.strip())

    # Join all lines into single commit message with proper newlines
    commit_message = "\n\n".join(commit_lines)

    # Properly escape the commit message for shell execution
    escaped_message = commit_message.replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')

    # Use single -m flag with the complete message
    commit_command = f'git commit -m "{escaped_message}"'
    run_command(commit_command, capture_output=False)

    logger.info("Pushing to git...")
    run_command("git push", capture_output=False)


def create_git_tag(version: str) -> None:
    """
    Create and push git tag.

    Args:
        version: The version string
    """
    logger.info(f"Creating git tag v{version}...")
    run_command(f"git tag v{version}", capture_output=False)
    run_command("git push --tags", capture_output=False)


def create_github_release(version: str, notes: List[str]) -> None:
    """
    Create GitHub release.

    Args:
        version: The version string
        notes: List of release note lines
    """
    logger.info(f"Creating GitHub release v{version}...")

    release_notes = "\n".join(note for note in notes if note.strip())

    # Escape quotes in release notes for shell command
    escaped_notes = release_notes.replace('"', '\\"')

    gh_command = f'gh release create v{version} --title "v{version}" --notes "{escaped_notes}"'
    run_command(gh_command, capture_output=False)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Publish django-mojo package")
    parser.add_argument(
        "--nobump",
        action="store_true",
        help="Skip version bumping"
    )
    parser.add_argument(
        "--nopypi",
        action="store_true",
        help="Skip PyPI publishing"
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Create GitHub release instead of just tagging"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing"
    )

    return parser.parse_args()


def main() -> None:
    """Main publishing workflow."""
    try:
        args = parse_arguments()

        if args.dry_run:
            logger.info("DRY RUN MODE - No changes will be made")
            return

        # Validate environment
        validate_uv_environment()
        validate_files_exist()

        # Version handling
        if args.nobump:
            version = get_current_version()
            logger.info(f"Using current version: {version}")
        else:
            version = bump_version()
            logger.info(f"Bumped to version: {version}")

        # Get release notes
        notes = get_release_notes()

        # Update files
        update_changelog(version, notes)
        update_init_version(version)

        # Build and publish
        if not args.nopypi:
            build_and_publish()
        else:
            logger.info("Skipping PyPI publishing")

        # Git operations
        commit_changes(version, notes)

        if args.release:
            create_github_release(version, notes)
        else:
            create_git_tag(version)

        logger.info(f"Successfully published version {version}")

    except PublishError as e:
        logger.error(f"Publishing failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nPublishing cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
