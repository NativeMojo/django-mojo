NAME = "webapps"


def describe(capabilities):
    values = {"view": capabilities["webapps"],
              "manage": capabilities["manage_webapps"],
              "onboard": capabilities["manage_webapps"]}
    return {"id": NAME, "enabled": values["view"], "capabilities": values,
            "contracts": {"onboarding": 1, "summary": 1}}


def reset(handler, fixtures, *, key_state="active", onboarding_state="idle", **options):
    handler.key_state = key_state
    handler.onboarding_operation = (
        fixtures["webapp_onboarding"](onboarding_state)
        if onboarding_state != "idle" else None)
