"""Fixed feature-capability providers for the built-in Admin portal."""

from importlib import import_module

from mojo.helpers import logit


FEATURE_NAMES = (
    "dashboard", "people", "webapps", "activity", "platform", "advanced",
    "settings", "sms", "email", "assistant")


def _disabled(name):
    return {"id": name, "enabled": False, "capabilities": {}}


def _validated(name, value):
    if not isinstance(value, dict):
        raise ValueError("provider response must be an object")
    if value.get("id") != name or not isinstance(value.get("enabled"), bool):
        raise ValueError("provider id/enabled contract is invalid")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict) or any(
            not isinstance(key, str) or not isinstance(enabled, bool)
            for key, enabled in capabilities.items()):
        raise ValueError("provider capabilities must be named booleans")
    return {
        "id": name,
        "enabled": value["enabled"],
        "capabilities": dict(capabilities),
    }


def bootstrap_features(request, capabilities):
    """Return all fixed feature namespaces, disabling a malformed provider."""
    features = {}
    for name in FEATURE_NAMES:
        try:
            module = import_module(
                f"mojo.apps.account.services.admin_features.{name}")
            features[name] = _validated(
                name, module.describe(request, dict(capabilities)))
        except Exception as err:
            logit.warning(
                f"admin feature {name} disabled: malformed provider "
                f"({err.__class__.__name__})")
            features[name] = _disabled(name)
    return features
