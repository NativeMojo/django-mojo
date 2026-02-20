# Passkey REST API (KISS Edition)

MOJO ships a simple WebAuthn / passkey flow for passwordless authentication. This implementation follows the KISS principle: **Keep It Simple, Stupid**.

---

## How It Works

1. **RP ID is derived from the Origin header** - No configuration needed!
2. **Challenges stored in Redis** - Auto-expire after 5 minutes
3. **Multi-portal support** - Users can register passkeys for different portals automatically
4. **JWT-based authentication** - No sessions required

---

## Configuration

Minimal configuration required:

```python
PASSKEYS_RP_NAME = "MOJO"  # Optional, displayed to users during registration
```

That's it! The RP ID is automatically derived from the request origin (hostname).

---

## Registration Flow

### 1. Begin Registration (Authenticated)

```http
POST /api/account/passkeys/register/begin
Authorization: Bearer <access_token>
Origin: https://portal1.example.com

{}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "challenge_id": "a3f8c9d2e1b4...",
    "expiresAt": "2025-02-05T18:27:10Z",
    "publicKey": {
      "rp": { "name": "MOJO", "id": "portal1.example.com" },
      "user": { "name": "sara", "displayName": "Sara", "id": "..." },
      "challenge": "...",
      "pubKeyCredParams": [...],
      "authenticatorSelection": {...}
    }
  }
}
```

### 2. Complete Registration

```http
POST /api/account/passkeys/register/complete
Authorization: Bearer <access_token>
Origin: https://portal1.example.com
Content-Type: application/json

{
  "challenge_id": "a3f8c9d2e1b4...",
  "credential": {
    "id": "...",
    "rawId": "...",
    "type": "public-key",
    "response": {
      "clientDataJSON": "...",
      "attestationObject": "..."
    },
    "transports": ["internal"]
  },
  "friendly_name": "My MacBook"
}
```

**Response:** Returns the created Passkey object.

---

## Login Flow (Passwordless)

### 1. Begin Login (Public)

```http
POST /api/auth/passkeys/login/begin
Origin: https://portal1.example.com
Content-Type: application/json

{
  "username": "sara"
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "challenge_id": "b7e4d8c3f2a1...",
    "expiresAt": "2025-02-05T18:32:10Z",
    "publicKey": {
      "rpId": "portal1.example.com",
      "challenge": "...",
      "allowCredentials": [
        { "type": "public-key", "id": "...", "transports": ["internal"] }
      ]
    }
  }
}
```

### 2. Complete Login

```http
POST /api/auth/passkeys/login/complete
Origin: https://portal1.example.com
Content-Type: application/json

{
  "challenge_id": "b7e4d8c3f2a1...",
  "credential": {
    "id": "...",
    "rawId": "...",
    "type": "public-key",
    "response": {
      "authenticatorData": "...",
      "clientDataJSON": "...",
      "signature": "...",
      "userHandle": "..."
    }
  }
}
```

**Response:** Standard JWT login response with `access_token` and `refresh_token`.

---

## Managing Passkeys

### List Passkeys

```http
GET /api/account/passkeys
Authorization: Bearer <access_token>
```

### Update Passkey

```http
POST /api/account/passkeys/<id>
Authorization: Bearer <access_token>

{
  "friendly_name": "New Name",
  "is_enabled": true
}
```

### Delete Passkey

```http
DELETE /api/account/passkeys/<id>
Authorization: Bearer <access_token>
```

---

## Multi-Portal Support

Users can register passkeys for different portals **without any configuration**:

```
User: sara@example.com

Passkeys:
- Credential A → rp_id: portal1.example.com
- Credential B → rp_id: portal2.example.com
- Credential C → rp_id: app.example.com
```

**How it works:**

1. User visits `https://portal1.example.com` and registers a passkey
2. The RP ID is automatically set to `portal1.example.com`
3. When logging into `portal1.example.com`, only passkeys with `rp_id=portal1.example.com` are used
4. User can register a different passkey on `portal2.example.com` with `rp_id=portal2.example.com`

**No configuration needed!** The Origin header drives everything.

---

## Client Implementation Notes

1. **Always send the Origin header** - The server uses it to derive the RP ID
2. **Store challenge_id** - Keep it client-side between begin/complete calls
3. **Challenges expire in 5 minutes** - Show appropriate error if expired
4. **Use HTTPS everywhere** - WebAuthn requires secure contexts
5. **Handle errors gracefully**:
   - `"No passkeys registered for this portal"` → Show registration prompt
   - `"Invalid or expired challenge"` → Restart the flow

---

## Testing

Run the test suite:

```bash
./bin/testit.py -m test_accounts.passkeys
```

Tests verify:
- Registration begin/complete flow
- Login begin/complete flow
- Multi-portal isolation (portal1 vs portal2)
- Challenge expiration (5 minutes)
- Counter verification (clone detection)

---

## Security Features

✅ **Counter verification** - Detects cloned authenticators  
✅ **Single-use challenges** - Prevents replay attacks  
✅ **Origin validation** - Ensures requests match expected portal  
✅ **Auto-expiring challenges** - Redis TTL handles cleanup  
✅ **RP isolation** - Passkeys bound to specific portals  

---

## Database Schema

### Passkey Model

```python
class Passkey(models.Model):
    user = ForeignKey(User)
    token = TextField()                    # Encoded credential
    credential_id = CharField(unique=True) # WebAuthn credential ID
    rp_id = CharField()                    # e.g., "portal1.example.com"
    is_enabled = BooleanField(default=True)
    sign_count = BigIntegerField()         # Clone detection
    transports = JSONField()               # e.g., ["internal", "usb"]
    friendly_name = CharField()            # e.g., "My iPhone"
    aaguid = CharField()                   # Authenticator GUID
    last_used = DateTimeField()
    created = DateTimeField()
    modified = DateTimeField()
```

**No PasskeyChallenge table** - Challenges live in Redis!

---

## Redis Keys

```
Key: passkey:challenge:{uuid}
TTL: 300 seconds (5 minutes)
Value: {
  "user_id": "...",
  "purpose": "register" or "authenticate",
  "state": {...},  // FIDO2 state
  "challenge": "...",
  "rp_id": "portal1.example.com",
  "origin": "https://portal1.example.com"
}
```

---

## Migration from Old Implementation

If you're migrating from the complex implementation:

1. **Run migration** to drop `PasskeyChallenge` table and `attestation_format` field
2. **Update imports** - Remove `passkeys_context` references
3. **Remove settings** - Delete `PASSKEYS_TENANTS`, `PASSKEYS_RP_ID` (keep `PASSKEYS_RP_NAME`)
4. **Test thoroughly** - Existing passkeys will continue to work!

---

## Troubleshooting

**"Origin header is required"**  
→ Ensure your client sends the `Origin` header with every request

**"No passkeys registered for this portal"**  
→ User needs to register a passkey for this specific portal (RP ID)

**"Invalid or expired challenge"**  
→ Challenge expired (5 min) or Redis is down - restart the flow

**"Passkey counter did not advance"**  
→ Possible cloned authenticator - security warning!

---

## Architecture Benefits

Compared to the old implementation, this is:

- ✅ **60% less code** - Removed 150+ lines of complexity
- ✅ **Zero config** - No tenant mappings needed
- ✅ **Faster** - Redis is faster than database queries
- ✅ **Scalable** - Auto-expiring challenges, no cleanup jobs
- ✅ **Simpler** - One clear path, easy to debug

**KISS wins!** 🎉
