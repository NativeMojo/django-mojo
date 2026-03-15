# Planning Note: User Registration Patterns

## Status
Documentation task — no new endpoints

## Decision
Self-service user registration is **left to downstream apps**, not implemented
as a framework endpoint.

---

## Rationale

"Registration" means different things per project:

- Open public signup
- Email-domain restriction (e.g. only `@acme.com`)
- Invite-code gating
- Waitlist / approval queue
- Age verification
- CAPTCHA requirement
- Admin-only account creation

The framework has no way to know which model applies. Adding a generic
`POST /api/auth/register` with `ALLOW_REGISTRATION=False` default sounds
simple, but immediately raises questions every project will answer differently:

- Which fields are required at registration?
- Is email verification required before the first login?
- Is the user auto-logged in after registering?
- What `permissions` / `org` should the new user get?
- How is abuse (bot signup, throwaway addresses) handled?

These are product decisions, not framework primitives. Any "generic"
implementation would either be so opaque projects couldn't customise it, or
so thin it added no value over what already exists.

---

## What Already Exists

The framework provides every building block a project needs:

| Need | Framework primitive |
|---|---|
| Create a user | `User(email=..., username=...).save()` + `user.save_password()` |
| Send invite link | `POST /api/auth/invite/send` — generates an `iv:` token, sends email |
| Accept invite + set password | `POST /api/auth/invite/accept` — consumes token, marks email verified, issues JWT |
| Verify email after self-signup | `POST /api/auth/verify/email/send` + `POST /api/auth/verify/email/confirm` |
| Require verified email before login | `REQUIRE_VERIFIED_EMAIL = True` setting |
| Password strength enforcement | Built into `user.set_new_password()` |
| Rate limiting on any endpoint | `@md.strict_rate_limit(...)` decorator |
| Block duplicate email | Uniqueness enforced on `User.email` field |
| Audit registration events | `user.report_incident(...)` |

---

## Action: Add Documentation

Add a **"Registration / Onboarding Patterns"** section to
`docs/django_developer/account/auth.md` covering the two most common
approaches projects use.

### Pattern A — Invite-only

The project creates accounts server-side (via admin, import, or custom
endpoint) and sends invite links. The user sets their password on first visit.

```python
# In your project's admin or management command:
from mojo.apps.account.models import User
from mojo.apps.account.utils.tokens import generate_invite_token

user = User(email="alice@example.com", username="alice")
user.set_unusable_password()
user.save()
user.send_template_email("invite", {"token": generate_invite_token(user)})
```

User clicks the link → `POST /api/auth/invite/accept` with the token → JWT
issued, email verified, ready to set password via `POST /api/user/me`.

### Pattern B — Open registration (project-level endpoint)

The project adds its own registration endpoint that wraps user creation:

```python
# In your project's REST layer (not the framework):
@md.POST("auth/register")
@md.public_endpoint()
@md.strict_rate_limit("register", ip_limit=5, ip_window=300)
@md.requires_params("email", "password")
def on_register(request):
    email = request.DATA.email.lower().strip()
    if User.objects.filter(email=email).exists():
        raise merrors.ValueException("Email already registered")
    user = User(email=email)
    user.username = user.generate_username_from_email()
    user.set_new_password(request.DATA.password)
    user.save()
    # Send verification email
    from mojo.apps.account.utils.tokens import generate_email_verify_token
    user.send_template_email("email_verify", {"token": generate_email_verify_token(user)})
    return JsonResponse({"status": True, "message": "Check your email to verify your account."})
```

If `REQUIRE_VERIFIED_EMAIL = True` is set, the user cannot log in until they
click the verification link — no additional gate logic needed.

---

## Files to Update

| File | Change |
|---|---|
| `docs/django_developer/account/auth.md` | Add "Registration / Onboarding Patterns" section with Pattern A and Pattern B examples |
| `docs/django_developer/account/README.md` | No change needed — auth.md is already indexed |

---

## Out of Scope

- A generic `POST /api/auth/register` endpoint in the framework
- `ALLOW_REGISTRATION` setting (too opaque — each project has different
  registration requirements)
- CAPTCHA integration (third-party, project-level)
- Social / OAuth registration (already handled by the OAuth flow in
  `auth/oauth/<provider>/complete` — auto-creates users on first login)

---

## Notes

- The invite flow (`iv:` token) is already fully implemented and documented.
  It is the recommended default for most B2B or controlled-access apps.
- OAuth registration (user signs up via Google etc.) requires zero project code —
  `_find_or_create_user` handles it automatically in the framework.
- The doc examples above are illustrative. Projects should adapt them to their
  own field requirements, permission assignments, and notification preferences.