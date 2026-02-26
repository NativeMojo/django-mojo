# Extending Authentication to Custom Models

Django-Mojo's authentication systems are not tied to `account.User`. You can add JWT auth, OAuth, and passkeys to any model — for example a `game.Player` that is separate from your system users.

---

## When to Use This

Use a custom auth model when you want an identity that is distinct from `account.User`. Common scenarios:

- **Game players** — lightweight identity (email + password or OAuth), separate from admin/system users
- **API consumers** — service accounts with their own JWT lifecycle
- **Multi-tenant members** — members of an organisation who should not be system users

---

## 1. The Model — `MojoAuthMixin`

Your model needs:

- `uuid` — `UUIDField` used as the JWT subject
- `is_active` — `BooleanField` gates authentication
- `MojoSecrets` — the signing key is stored in `mojo_secrets`

Add `MojoAuthMixin` to the inheritance chain:

```python
# game/models/player.py
import uuid
from django.db import models
from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets
from mojo.models.auth import MojoAuthMixin


class Player(MojoSecrets, MojoAuthMixin, MojoModel):
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    username = models.CharField(max_length=64, unique=True)
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["owner"]
        SAVE_PERMS = ["owner"]
        GRAPHS = {
            "default": {
                "fields": ["id", "uuid", "username", "display_name", "email"],
            }
        }

    def __str__(self):
        return self.username
```

`MojoAuthMixin` provides three methods:

| Method | Description |
|--------|-------------|
| `get_auth_key()` | Returns the per-player JWT signing key (auto-generated, stored in `mojo_secrets`) |
| `generate_jwt(request, extra)` | Issues an `access_token` / `refresh_token` package |
| `validate_jwt(token, request)` | Classmethod — validates a token and returns `(player, error)` |

---

## 2. Register a Bearer Token Type

Tell the auth middleware how to validate `player` tokens by adding to your Django settings:

```python
AUTH_BEARER_HANDLERS = {
    "player": "game.models.Player.validate_jwt",
}

AUTH_BEARER_NAME_MAP = {
    "bearer": "user",    # default — sets request.user
    "apikey": "user",    # default — sets request.user
    "player": "player",  # sets request.player
}
```

Now a request with `Authorization: player <token>` will:
1. Call `Player.validate_jwt(token, request)`
2. On success, set `request.player` to the `Player` instance
3. On failure, return `401`

---

## 3. Login Endpoint

Write a login view that issues a player JWT:

```python
# game/rest/player.py
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.account import errors as aerrors
from game.models import Player


@md.POST("game/player/login")
@md.requires_params("email", "password")
@md.public_endpoint()
def on_player_login(request):
    email = request.DATA.get("email")
    password = request.DATA.get("password")

    player = Player.objects.filter(email=email, is_active=True).first()
    if not player or not player.check_password(password):
        raise aerrors.AuthException("Invalid credentials")

    tokens = player.generate_jwt(request)
    return JsonResponse({
        "status": True,
        "data": {
            **tokens,
            "player": player.rest_serialize(),
        }
    })
```

The `tokens` dict from `generate_jwt()` contains `access_token`, `refresh_token`, and `expires_in`.

---

## 4. Protecting Player Endpoints

Use `@md.requires_auth` as normal — it checks that `request.bearer` is set. For player-specific endpoints, check `request.player` yourself:

```python
@md.GET("game/player/me")
@md.requires_auth
def on_player_me(request):
    # AuthMiddleware has already verified the player token
    # and set request.player
    if not getattr(request, "player", None):
        from mojo.helpers.response import JsonResponse
        return JsonResponse({"error": "Player auth required"}, status=403)
    return request.player.rest_get(request)
```

Or use `AUTH_BEARER_NAME_MAP` so the middleware always puts the instance in a known attribute, and write a small decorator:

```python
# game/decorators.py
from functools import wraps
from mojo.helpers.response import JsonResponse


def requires_player(fn):
    @wraps(fn)
    def wrapper(request, *args, **kwargs):
        if not getattr(request, "player", None):
            return JsonResponse({"error": "Player auth required"}, status=401)
        return fn(request, *args, **kwargs)
    return wrapper
```

---

## 5. OAuth for Players

Create a player-specific OAuth connection model with a concrete FK (not `GenericForeignKey`):

```python
# game/models/player_oauth.py
from django.db import models
from mojo.models import MojoModel
from mojo.models.secrets import MojoSecrets


class PlayerOAuthConnection(MojoSecrets, MojoModel):
    player = models.ForeignKey(
        "game.Player",
        related_name="oauth_connections",
        on_delete=models.CASCADE,
    )
    provider = models.CharField(max_length=32, db_index=True)
    provider_uid = models.CharField(max_length=255, db_index=True)
    email = models.EmailField(blank=True, null=True, default=None)
    is_active = models.BooleanField(default=True, db_index=True)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        unique_together = [("provider", "provider_uid")]
```

Then write your own `_find_or_create_player()` following the same pattern as `_find_or_create_user()` in `mojo/apps/account/rest/oauth.py`:

```python
# game/rest/player_oauth.py
from mojo import decorators as md
from mojo.apps.account.services.oauth import get_provider
from mojo.helpers.response import JsonResponse
from mojo.helpers import logit
from game.models import Player
from game.models.player_oauth import PlayerOAuthConnection


def _find_or_create_player(provider_name, profile):
    uid = profile["uid"]
    email = profile["email"]
    display_name = profile.get("display_name", "")

    # 1. Existing connection
    conn = PlayerOAuthConnection.objects.filter(
        provider=provider_name, provider_uid=uid
    ).select_related("player").first()
    if conn:
        return conn.player, conn

    # 2. Existing player by email
    player = Player.objects.filter(email=email).first() if email else None

    # 3. Create new player
    if not player:
        player = Player(email=email)
        player.username = email.split("@")[0]  # or your own logic
        player.display_name = display_name
        player.save()
        logit.info("oauth", f"Created new player {player.username} via {provider_name}")

    conn = PlayerOAuthConnection.objects.create(
        player=player,
        provider=provider_name,
        provider_uid=uid,
        email=email,
    )
    return player, conn


@md.GET("game/player/oauth/<str:provider>/begin")
@md.public_endpoint()
def on_player_oauth_begin(request, provider):
    svc = get_provider(provider)
    redirect_uri = f"{request.DATA.get('origin', '')}/game/oauth/{provider}/complete"
    state = svc.create_state()
    auth_url = svc.get_auth_url(state=state, redirect_uri=redirect_uri)
    return JsonResponse({"status": True, "data": {"auth_url": auth_url, "state": state}})


@md.POST("game/player/oauth/<str:provider>/complete")
@md.requires_params("code", "state")
@md.public_endpoint()
def on_player_oauth_complete(request, provider):
    svc = get_provider(provider)
    state_data = svc.consume_state(request.DATA.get("state"))
    if state_data is None:
        return JsonResponse({"error": "Invalid or expired state"}, status=401)

    redirect_uri = f"{request.DATA.get('origin', '')}/game/oauth/{provider}/complete"
    tokens = svc.exchange_code(code=request.DATA.get("code"), redirect_uri=redirect_uri)
    profile = svc.get_profile(tokens)

    player, conn = _find_or_create_player(provider, profile)
    if not player.is_active:
        return JsonResponse({"error": "Account disabled"}, status=403)

    conn.set_secret("access_token", tokens.get("access_token"))
    conn.save()

    token_package = player.generate_jwt(request)
    return JsonResponse({
        "status": True,
        "data": {
            **token_package,
            "player": player.rest_serialize(),
        }
    })
```

The OAuth provider layer (`OAuthProvider`, `get_provider()`, Google implementation) is fully reused — only the user-resolution and connection model are specific to your app.

---

## 6. Passkeys for Players

Create a `PlayerPasskey` model mirroring `account.Passkey` but with a FK to `Player`:

```python
# game/models/player_passkey.py
from django.db import models
from mojo.models import MojoModel


class PlayerPasskey(MojoModel):
    player = models.ForeignKey(
        "game.Player",
        related_name="passkeys",
        on_delete=models.CASCADE,
    )
    credential_id = models.TextField(unique=True, db_index=True)
    public_key = models.TextField()
    sign_count = models.PositiveIntegerField(default=0)
    device_name = models.CharField(max_length=128, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)
```

Then use `PasskeyService` from `mojo.apps.account.utils.passkeys`. It is fully model-agnostic — pass your `Player` instance wherever a `user` argument is expected. The service accesses `player.uuid`, `player.username`, `player.display_name`, and `player.passkeys` (the reverse relation), so as long as your model has those attributes it works without modification.

---

## Summary

| Capability | What to do |
|------------|------------|
| **JWT auth** | Add `MojoAuthMixin` to model; requires `uuid`, `is_active`, `MojoSecrets` |
| **Register token type** | Add `"player": "game.models.Player.validate_jwt"` to `AUTH_BEARER_HANDLERS` |
| **OAuth login** | Create `PlayerOAuthConnection` (concrete FK); reuse `OAuthProvider` / `get_provider()` |
| **Passkeys** | Create `PlayerPasskey` (concrete FK); reuse `PasskeyService` directly |
| **Protect endpoints** | Check `request.player` (or whichever attribute `AUTH_BEARER_NAME_MAP` maps to) |

Concrete FKs are preferred over `GenericForeignKey` — they preserve DB integrity, allow efficient joins, and keep security checks simple (`conn.player != request.player` is unambiguous).
