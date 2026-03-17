#!/usr/bin/env python
"""
Test runner bootstrap.
Activates the venv automatically if present, flushes the database,
then hands off to testit.runner.
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent   # django-mojo/bin/
REPO_ROOT = SCRIPT_DIR.parent                  # django-mojo/

# Auto-activate venv if present and not already active
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and sys.prefix == sys.base_prefix:
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)
TESTPROJECT = REPO_ROOT / "testproject"

if not TESTPROJECT.exists():
    print("ERROR: testproject/ not found. Run ./bin/create_testproject first.")
    sys.exit(1)

sys.path.insert(0, str(REPO_ROOT))                      # mojo, testit
sys.path.insert(0, str(TESTPROJECT / "config"))         # settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

from mojo.helpers import paths
paths.configure_paths(str(TESTPROJECT / "config" / "settings" / "__init__.py"), 1)

import django
django.setup()

# Flush database and Redis before running tests so every run starts clean
from django.db import connection
print("==> Flushing database and Redis")
with connection.cursor() as cursor:
    cursor.execute("""
        DO $$ DECLARE r RECORD;
        BEGIN
            FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
                EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' CASCADE';
            END LOOP;
        END $$;
    """)

from mojo.helpers.redis import get_connection
r = get_connection()
if r:
    r.flushdb()

from testit import runner

if __name__ == "__main__":
    runner.main(runner.setup_parser())
