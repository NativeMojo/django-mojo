"""
TOTP service — wraps pyotp to generate and verify TOTP codes.

Secrets are stored via MojoSecrets on the UserTOTP model, never returned
after initial setup.
"""
try:
    import pyotp
except:
    pyotp = None

from mojo.helpers.settings import settings


def generate_secret():
    """Generate a new base32 TOTP secret."""
    return pyotp.random_base32()


def get_provisioning_uri(secret, username):
    """
    Return an otpauth:// URI suitable for QR code generation.
    Compatible with Google Authenticator, Authy, etc.
    """
    totp = pyotp.TOTP(secret)
    totp_issuer = settings.get("TOTP_ISSUER", "MOJO")
    return totp.provisioning_uri(name=username, issuer_name=totp_issuer)


def verify_code(secret, code):
    """
    Verify a 6-digit TOTP code against the secret.
    Allows a ±1 window to account for clock drift.

    Returns True if valid, False otherwise.
    """
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)
