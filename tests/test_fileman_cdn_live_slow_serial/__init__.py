TESTIT = {
    # Live CloudFront proof for CDN delivery (maestro #8250): provisions a real
    # distribution on a real SSE-KMS bucket, fetches through it, re-runs to
    # prove idempotency, rotates and revokes the key, then tears down what it
    # created. Needs AWS credentials, FILEMAN_CDN_LIVE_BUCKET,
    # FILEMAN_CDN_LIVE_KMS_KEY and KMS_KEY_ID; auto-skips without them.
    # Serial: it is one account-level conversation, minutes long.
    "requires_extra": ["slow"],
    "serial": True,
}
