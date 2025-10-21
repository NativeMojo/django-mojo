from mojo.helpers import dates, crypto
from mojo import errors as merrors
from mojo.helpers.settings import settings
from mojo.apps.account.models.user import User

PASSWORD_RESET_TOKEN_TTL = settings.get("PASSWORD_RESET_TOKEN_TTL", 3600)
PASSWORD_RESET_CODE_TTL = settings.get("PASSWORD_RESET_CODE_TTL", 600)


def generate_token(user):
    return generate_password_reset_token(user)

def generate_password_reset_token(user):
    now_ts = int(dates.utcnow().timestamp())
    jti = crypto.random_string(12, True, True, False)
    token = crypto.b64_encode({"uid": user.pk, "ts": now_ts, "jti": jti})
    sig = crypto.sign(token, user.get_auth_key())
    user.set_secret("password_reset_jti", jti)
    user.set_secret("password_reset_ts", now_ts)
    user.save(update_fields=["mojo_secrets", "modified"])
    hex_token = token.encode("utf-8").hex() + sig[-6:]
    return hex_token


def validate_token(hex_token):
    return verify_password_reset_token(hex_token)


def verify_password_reset_token(hex_token):
    orig_token = hex_token
    user = None
    try:
        tsig = hex_token[-6:]
        hex_token = hex_token[:-6]
        token = bytes.fromhex(hex_token).decode("utf-8")
        obj = crypto.b64_decode(token)
        if not isinstance(obj, dict) or "uid" not in obj or "ts" not in obj or "jti" not in obj:
            raise merrors.ValueException("Invalid token")
        user = User.objects.get(pk=obj["uid"])
        sig = crypto.sign(token, user.get_auth_key())
        if sig[-6:] != tsig:
            user.report_incident(
                details=f"{user.username} invalid reset token (signature)",
                event_type="invalid_reset_token")
            raise merrors.ValueException("Invalid token")
        now_ts = int(dates.utcnow().timestamp())
        if now_ts - int(obj["ts"]) > int(PASSWORD_RESET_TOKEN_TTL):
            user.report_incident(
                details=f"{user.username} expired reset token",
                event_type="expired_reset_token")
            raise merrors.ValueException("Expired token")
        expected_jti = user.get_secret("password_reset_jti")
        if not expected_jti or expected_jti != obj["jti"]:
            user.report_incident(
                details=f"{user.username} reset token jti mismatch or reused",
                event_type="invalid_reset_token")
            raise merrors.ValueException("Invalid token")
        user.set_secret("password_reset_jti", None)
        user.save(update_fields=["mojo_secrets", "modified"])
        return user
    except Exception as err:
        if user:
            user.report_incident(
                details=f"{user.username} invalid reset token",
                event_type="reset:unknown",
                error=str(err),
                level=8, token=orig_token)
        else:
            User.class_report_incident(
                details="reset token error",
                event_type="reset:unknown",
                error=str(err),
                level=8, token=orig_token)
    raise merrors.ValueException("Invalid token")
