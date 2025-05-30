from fido2.webauthn import PublicKeyCredentialRpEntity, AttestedCredentialData, ResidentKeyRequirement
from fido2.server import Fido2Server
from fido2.utils import websafe_decode, websafe_encode
from objict import objict
from mojo.helpers.settings import settings


class PasskeyAuthenticator:
    def __init__(self, rp_id=settings.PASSKEYS_RP_ID, rp_name=settings.PASSKEYS_RP_NAME):
        self.server = Fido2Server(PublicKeyCredentialRpEntity(id=rp_id, name=rp_name))

    def register_begin(self, member, attachment="cross-platform"):
        request = {
            "id": member.uuid,
            "name": member.username,
            "displayName": member.display_name
        }
        data, state = self.server.register_begin(
            request,
            authenticator_attachment=attachment,
            resident_key_requirement=ResidentKeyRequirement.PREFERRED)
        response = objict(state=state, data=objict.fromdict(dict(data)), rp=objict(self.server.rp))
        response.excludeCredentials = self.exclude_credentials(member, websafe=True)
        return response

    def register_complete(self, credentials, fido2_state):
        auth_data = self.server.register_complete(
            fido2_state,
            response=credentials
        )
        return websafe_encode(auth_data.credential_data)

    def exclude_credentials(self, member, websafe=False):
        creds = [AttestedCredentialData(websafe_decode(uk.token)) for uk in member.passkeys.all()]
        if websafe:
            return [dict(type="public-key", id=websafe_encode(acd.credential_id)) for acd in creds]
        return creds

    def authenticate_begin(self, member):
        creds = [AttestedCredentialData(websafe_decode(uk.token)) for uk in member.passkeys.all()]
        challenge, state = self.server.authenticate_begin(creds)
        response = objict(state=state, challenge=challenge, rp=objict(self.server.rp))
        return response

    def authenticate_complete(self, credential, public_key, fido2_state):
        stored_credentials = [AttestedCredentialData(websafe_decode(public_key))]
        try:
            self.server.authenticate_complete(
                fido2_state,
                credentials=stored_credentials,
                response=credential)
            return True
        except Exception:
            return False
