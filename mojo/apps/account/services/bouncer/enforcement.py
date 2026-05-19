"""
Gradient enforcement for in-session bouncer scoring.

The framework maps a session-risk score to one of four bands. Each band
sets flags + fires an incident. Apps read the flags and decide what each
one means in their domain (close gameplay sessions, freeze wallet credit,
require step-up auth, etc.). The framework never imports app code beyond
an optional dotted-path freeze handler.

Defaults can be overridden via:
  BOUNCER_SESSION_BANDS = {
      'freeze': 90, 'shadow_ban': 70, 'require_step_up': 50, 'monitor': 30,
  }
  BOUNCER_SESSION_FREEZE_HANDLER = 'apps.foo.bar.freeze_user'
"""
from mojo.apps import incident
from mojo.helpers import logit
from mojo.helpers.modules import load_function
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')

_DEFAULT_BANDS = {
    'freeze': 90,
    'shadow_ban': 70,
    'require_step_up': 50,
    'monitor': 30,
}


def _bands():
    return settings.get_static('BOUNCER_SESSION_BANDS') or _DEFAULT_BANDS


def _set_user_flag(user, key, value):
    if user is None:
        return
    try:
        user.set_protected_metadata(key, value)
    except Exception:
        logger.exception(
            "bouncer: failed to set user flag %s for user=%s", key, user.pk
        )


def _fire(category, level, **fields):
    try:
        incident.report_event(
            fields.pop('details', f"bouncer session enforcement: {category}"),
            category=category,
            scope='account',
            level=level,
            **fields,
        )
    except Exception:
        logger.exception("bouncer: failed to fire %s", category)


def _call_freeze_handler(user, device, risk_score):
    dotted = settings.get_static('BOUNCER_SESSION_FREEZE_HANDLER', '')
    if not dotted:
        return
    try:
        fn = load_function(dotted)
    except ImportError:
        logger.exception("bouncer: could not resolve freeze handler %s", dotted)
        return
    try:
        fn(user, device, risk_score)
    except Exception:
        logger.exception(
            "bouncer: freeze handler %s raised for muid=%s",
            dotted, getattr(device, 'muid', '?'),
        )


def apply_session_response(device, risk_score, user=None, triggered=None):
    """
    Map score → enforcement band. Idempotent: re-running with the same score
    re-asserts the same flags but doesn't repeat incidents we already raised
    on the device for this band (best-effort dedup via device.risk_tier).

    Score precedence is high-to-low. Side effects:

      ≥ freeze  → device.risk_tier=blocked, block_count++, freeze handler,
                  fire security:bouncer:session_freeze (level 9)
      ≥ shadow  → user.bouncer_shadow_banned=True,
                  fire security:bouncer:session_shadow_ban (level 8)
      ≥ step_up → user.bouncer_require_step_up=True,
                  fire security:bouncer:session_step_up (level 6)
      ≥ monitor → fire security:bouncer:session_suspect (level 6), no flag
      else      → noop
    """
    bands = _bands()
    muid = getattr(device, 'muid', '') if device else ''
    triggered = triggered or []
    common = dict(
        muid=muid,
        risk_score=risk_score,
        triggered_signals=triggered,
    )

    if risk_score >= bands.get('freeze', 90):
        if device and device.risk_tier != 'blocked':
            device.risk_tier = 'blocked'
            device.block_count = (device.block_count or 0) + 1
            try:
                device.save(update_fields=['risk_tier', 'block_count', 'modified'])
            except Exception:
                logger.exception("bouncer: device save failed for muid=%s", muid)
        _call_freeze_handler(user, device, risk_score)
        _fire(
            'security:bouncer:session_freeze', 9,
            details=f"Bouncer session freeze muid={muid} score={risk_score}",
            **common,
        )
        return 'freeze'

    if risk_score >= bands.get('shadow_ban', 70):
        _set_user_flag(user, 'bouncer_shadow_banned', True)
        _fire(
            'security:bouncer:session_shadow_ban', 8,
            details=f"Bouncer session shadow_ban muid={muid} score={risk_score}",
            **common,
        )
        return 'shadow_ban'

    if risk_score >= bands.get('require_step_up', 50):
        _set_user_flag(user, 'bouncer_require_step_up', True)
        _fire(
            'security:bouncer:session_step_up', 6,
            details=f"Bouncer session step_up muid={muid} score={risk_score}",
            **common,
        )
        return 'require_step_up'

    if risk_score >= bands.get('monitor', 30):
        _fire(
            'security:bouncer:session_suspect', 6,
            details=f"Bouncer session suspect muid={muid} score={risk_score}",
            **common,
        )
        return 'monitor'

    return 'noop'
