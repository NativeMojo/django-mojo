import jwt
import datetime
import time
from objict import objict

from mojo.helpers.settings import settings

JWT_TOKEN_EXPIRY = settings.get("JWT_TOKEN_EXPIRY", 21600)
JWT_REFRESH_TOKEN_EXPIRY = settings.get("JWT_REFRESH_TOKEN_EXPIRY", 604800)
JWT_ALGORITHM = settings.get("JWT_ALGORITHM", "HS256")


class JWToken:
    def __init__(self, key=f"{time.time()}", access_token_expiry=JWT_TOKEN_EXPIRY, refresh_token_expiry=JWT_REFRESH_TOKEN_EXPIRY, alg=JWT_ALGORITHM, token=None):
        self.key = key
        self.access_token_expiry = access_token_expiry
        self.refresh_token_expiry = refresh_token_expiry
        self.alg = alg
        self.is_expired = False
        self.invalid_sig = False
        self.is_valid = False
        self.payload = None
        if token is not None:
            self.is_valid, self.payload = self.decode(token)

    def decode(self, token, validate=True):
        payload = objict.fromdict(jwt.decode(token, self.key, algorithms=self.alg, options={"verify_signature":False}))
        if not validate:
            return payload
        is_valid = self.is_token_valid(token)
        return is_valid, payload

    def create(self, **kwargs):
        package = objict()
        package.access_token = self.create_access_token(**kwargs)
        package.refresh_token = self.create_access_token(**kwargs)
        return package

    def create_access_token(self, **kwargs):
        payload = dict(kwargs)
        payload['exp'] = self._get_exp_time(self.access_token_expiry)
        payload['token_type'] = "access"
        payload["iat"] = int(time.time())
        token = jwt.encode(payload, self.key, algorithm=self.alg)
        return token

    def create_refresh_token(self, **kwargs):
        payload = dict(kwargs)
        payload['exp'] = self._get_exp_time(self.refresh_token_expiry)
        payload['token_type'] = "refresh"
        payload["iat"] = int(time.time())
        token = jwt.encode(payload, self.key, algorithm=self.alg)
        return token

    def refresh_access_token(self, refresh_token):
        try:
            decoded = jwt.decode(refresh_token, self.key, algorithms=[self.alg])
            new_access_token = self.create_access_token(**decoded)
            return new_access_token
        except jwt.ExpiredSignatureError:
            raise Exception("Refresh token has expired.")
        except jwt.InvalidTokenError:
            raise Exception("Invalid refresh token.")

    def _get_exp_time(self, expiry_seconds):
        return datetime.datetime.utcnow() + datetime.timedelta(seconds=expiry_seconds)

    def is_token_valid(self, token):
        try:
            self.is_expired = False
            self.invalid_sig = False
            jwt.decode(token, self.key, algorithms=['HS256'])
            return True
        except jwt.ExpiredSignatureError:
            self.is_expired = True
            return False
        except jwt.InvalidTokenError:
            self.invalid_sig = True
            return False
