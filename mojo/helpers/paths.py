import sys
import socket
from pathlib import Path


def configure_paths(base_dir, depth=2):
    """
    Dynamically configure paths based on the provided BASE_DIR.
    Injects computed paths into the global namespace.
    """
    global HOSTNAME, PROJECT_ROOT, VAR_ROOT, BIN_ROOT, LOG_ROOT, CONFIG_ROOT
    global MEDIA_ROOT, MEDIA_URL, STATIC_ROOT, SITE_STATIC_ROOT, STATIC_DATA_ROOT
    global STATIC_URL, STATIC_URL_SECURE, APPS_ROOT, APPS_CONFIG_FILE, STATICFILES_DIRS

    # System Info
    HOSTNAME = socket.gethostname()

    # Define Paths
    base_path = Path(base_dir).resolve().parents[depth]
    PROJECT_ROOT = base_path.parent
    VAR_ROOT = PROJECT_ROOT / "var"
    BIN_ROOT = PROJECT_ROOT / "bin"
    APPS_ROOT = PROJECT_ROOT / "apps"
    CONFIG_ROOT = PROJECT_ROOT / "config"
    APPS_CONFIG_FILE = APPS_ROOT / "apps.json"
    LOG_ROOT = VAR_ROOT / "logs"

    # Media Settings
    MEDIA_ROOT = base_path / "media"
    MEDIA_URL = "/media/"

    # Static Files Settings
    STATIC_ROOT = base_path / "static"
    SITE_STATIC_ROOT = base_path / "site_static"
    STATIC_DATA_ROOT = SITE_STATIC_ROOT / "json"
    STATIC_URL = "/static/"
    STATIC_URL_SECURE = "/static/"
    STATICFILES_DIRS = [SITE_STATIC_ROOT]

    MEDIA_URL = '/media/'
    MEDIA_ROOT = base_path / "media"


    # Load additional site-packages paths from .site_packages file
    site_packages_file = PROJECT_ROOT / ".site_packages"

    if site_packages_file.exists():
        with site_packages_file.open("r") as f:
            site_packages_paths = [path.strip() for path in f.readlines() if path.strip()]

        for path in site_packages_paths:
            site_path = Path(path)
            if site_path.exists() and str(site_path) not in sys.path:
                sys.path.insert(0, str(site_path))

# To use this, call configure_paths(BASE_DIR) from your Django settings


def configure_apps():
    from objict import objict
    global INSTALLED_APPS, APPS_CONFIG
    APPS_CONFIG = objict.fromFile(APPS_CONFIG_FILE)
    INSTALLED_APPS = APPS_CONFIG.installed
