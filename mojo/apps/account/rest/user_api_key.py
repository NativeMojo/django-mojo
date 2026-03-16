import mojo.decorators as md
from mojo.apps.account.models.user_api_key import UserAPIKey
from mojo.apps.account.models import User
import mojo.errors as merrors


@md.URL("account/api_keys")
@md.URL("account/api_keys/<int:pk>")
@md.uses_model_security(UserAPIKey)
def on_user_api_keys(request, pk=None):
    return UserAPIKey.on_rest_request(request, pk)


@md.POST("auth/generate_api_key")
@md.requires_auth()
def generate_api_key(request):
    allowed_ips = request.DATA.get_typed("allowed_ips", [], typed=list)
    expire_days = request.DATA.get_typed("expire_days", 360, typed=int)
    label = request.DATA.get("label", "")
    if expire_days > 360:
        raise merrors.ValueException("Invalid expire_days")
    token = UserAPIKey.create_for_user(request.user, allowed_ips=allowed_ips, expire_days=expire_days, label=label)
    request.user.log(f"API Key Generated {token.jti} expire {expire_days} days", "api_key:generated")
    return dict(status=True, data=token)
