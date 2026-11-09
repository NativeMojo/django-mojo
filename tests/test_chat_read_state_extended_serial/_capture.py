"""Room-scoped cross-process evidence for the configured read hook."""
import json
import tempfile
from pathlib import Path


def _path(room_id):
    return Path(tempfile.gettempdir()) / f"mojo-chat-read-hook-{room_id}.json"


def capture(room_id, user_id, position, changed):
    path = _path(room_id)
    calls = read(room_id)
    calls.append({"room_id": room_id, "user_id": user_id,
                  "position": dict(position), "changed": changed})
    path.write_text(json.dumps(calls))


def read(room_id):
    path = _path(room_id)
    return json.loads(path.read_text()) if path.exists() else []


def clear(room_id):
    _path(room_id).unlink(missing_ok=True)
