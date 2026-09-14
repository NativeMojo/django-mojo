"""
Simple FCM v1 API client.

FCM v1 uses OAuth 2.0 with service account credentials (JSON file from Firebase Console).
This replaces pyfcm which only supports the legacy API.
"""

import json
import time
import requests
from datetime import datetime, timedelta
from mojo.helpers.settings import settings

class FCMv1Client:
    """
    Simple Firebase Cloud Messaging v1 API client.

    Usage:
        client = FCMv1Client(service_account_json)
        result = client.send(
            token="device_fcm_token",
            title="Hello",
            body="World",
            data={"key": "value"}
        )
    """

    FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    OAUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
    SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

    def __init__(self, service_account_json, http=None):
        """
        Initialize FCM client with service account credentials.

        Args:
            service_account_json: Dict or JSON string with service account credentials
        """
        if isinstance(service_account_json, str):
            self.credentials = json.loads(service_account_json)
        else:
            self.credentials = service_account_json

        if not isinstance(self.credentials, dict):
            raise ValueError("Service account JSON must be an object")
        self.http = http or requests
        self.project_id = self.credentials.get('project_id')
        if not self.project_id:
            raise ValueError("Service account JSON missing 'project_id'")

        self._access_token = None
        self._token_expiry = None

    def _get_access_token(self):
        """Get OAuth 2.0 access token for FCM API."""
        # Use cached token if still valid
        if self._access_token and self._token_expiry:
            if datetime.utcnow() < self._token_expiry - timedelta(minutes=5):
                return self._access_token

        # Create JWT for token request
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend

        now = int(time.time())
        payload = {
            "iss": self.credentials.get('client_email'),
            "sub": self.credentials.get('client_email'),
            "aud": self.OAUTH_ENDPOINT,
            "iat": now,
            "exp": now + 3600,
            "scope": " ".join(self.SCOPES)
        }

        # Load private key
        private_key = serialization.load_pem_private_key(
            self.credentials.get('private_key').encode('utf-8'),
            password=None,
            backend=default_backend()
        )

        # Create signed JWT
        signed_jwt = jwt.encode(payload, private_key, algorithm='RS256')

        # Exchange JWT for access token
        response = self.http.post(
            self.OAUTH_ENDPOINT,
            data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': signed_jwt
            },
            timeout=(5, 20),
        )

        if response.status_code != 200:
            raise ValueError("FCM authentication failed")

        token_data = response.json()
        self._access_token = token_data['access_token']
        self._token_expiry = datetime.utcnow() + timedelta(seconds=token_data.get('expires_in', 3600))

        return self._access_token

    def send(self, token, title=None, body=None, data=None, sound=None, badge=None, priority="high"):
        """
        Send notification via FCM v1 API.

        Args:
            token: FCM device token
            title: Notification title (optional for silent notifications)
            body: Notification body (optional for silent notifications)
            data: Custom data payload dict
            sound: Notification sound (default: "default")
            badge: Badge count for iOS

        Returns:
            dict with success status and response details
        """
        # Build message payload
        message = {
            "token": token
        }

        # Add notification if title or body provided
        if title or body:
            notification = {}
            if title:
                notification['title'] = title
            if body:
                notification['body'] = body
            message['notification'] = notification

        # Add data payload
        if data:
            # FCM v1 requires data values to be strings
            message['data'] = {k: str(v) for k, v in data.items()}

        # Platform-specific options
        android_config = {}
        apns_config = {}

        if title or body:
            # Visible notification
            if sound:
                android_config['notification'] = {'sound': sound}
                apns_config['payload'] = {
                    'aps': {
                        'sound': sound
                    }
                }
                if badge is not None:
                    apns_config['payload']['aps']['badge'] = badge
        else:
            # Silent notification (data-only)
            android_config['priority'] = priority
            apns_config['headers'] = {'apns-priority': '5'}
            apns_config['payload'] = {'aps': {'content-available': 1}}

        if android_config:
            message['android'] = android_config
        if apns_config:
            message['apns'] = apns_config

        return self._post_message(message)

    def validate(self):
        """Authenticate and validate project send permission without delivery."""
        return self._post_message({
            'topic': 'mojo_configuration_check',
            'notification': {'title': 'Configuration check', 'body': 'Validation only'},
        }, validate_only=True)

    def _post_message(self, message, validate_only=False):
        try:
            access_token = self._get_access_token()
        except Exception:
            return {'success': False, 'outcome': 'blocked', 'error_code': 'authentication_failed'}

        url = self.FCM_ENDPOINT.format(project_id=self.project_id)
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        payload = {'message': message}
        if validate_only:
            payload['validate_only'] = True
        try:
            response = self.http.post(url, headers=headers, json=payload, timeout=(5, 20))
        except requests.RequestException:
            return {'success': False, 'outcome': 'blocked' if validate_only else 'unknown',
                    'error_code': 'provider_unavailable' if validate_only else 'acceptance_unknown'}
        try:
            resp_msg = response.json() if response.text else {}
        except ValueError:
            resp_msg = {}
        if not isinstance(resp_msg, dict):
            resp_msg = {}
        if settings.LOG_PUSH_MESSAGES:
            from mojo.helpers import logit
            logit.info("FCM PUSH", "HTTP status:", response.status_code, "validation:", validate_only)
        if response.status_code == 200:
            message_id = resp_msg.get('name')
            if (not isinstance(message_id, str) or len(message_id) > 500
                    or not message_id.startswith(f'projects/{self.project_id}/messages/')
                    or not message_id.rsplit('/', 1)[-1]):
                return {'success': False, 'outcome': 'blocked' if validate_only else 'unknown',
                        'error_code': 'invalid_provider_response', 'status_code': 200}
            return {
                'success': True,
                'outcome': 'validated' if validate_only else 'accepted',
                'message_id': message_id,
                'status_code': response.status_code,
            }
        else:
            error = resp_msg.get('error')
            error = error if isinstance(error, dict) else {}
            code = error.get('status')
            for detail in error.get('details', []) if isinstance(error.get('details'), list) else []:
                if isinstance(detail, dict) and detail.get('@type') == 'type.googleapis.com/google.firebase.fcm.v1.FcmError':
                    code = detail.get('errorCode', code)
            allowed = {'INVALID_ARGUMENT', 'UNREGISTERED', 'SENDER_ID_MISMATCH', 'QUOTA_EXCEEDED',
                       'UNAVAILABLE', 'INTERNAL', 'THIRD_PARTY_AUTH_ERROR', 'PERMISSION_DENIED',
                       'UNAUTHENTICATED', 'NOT_FOUND'}
            code = code if isinstance(code, str) and code in allowed else 'provider_rejected'
            return {
                'success': False,
                'outcome': 'rejected',
                'status_code': response.status_code,
                'error_code': code,
                'error': {'code': code},
            }

    def send_multicast(self, tokens, title=None, body=None, data=None, sound=None, badge=None):
        """
        Send notification to multiple devices (sends individually).

        Args:
            tokens: List of FCM device tokens
            title: Notification title
            body: Notification body
            data: Custom data payload
            sound: Notification sound
            badge: Badge count for iOS

        Returns:
            dict with success/failure counts and individual results
        """
        results = []
        success_count = 0
        failure_count = 0

        for token in tokens:
            result = self.send(token, title, body, data, sound, badge)
            results.append({
                'token': token,
                'success': result['success'],
                'message_id': result.get('message_id'),
                'error': result.get('error')
            })

            if result['success']:
                success_count += 1
            else:
                failure_count += 1

        return {
            'success': success_count,
            'failure': failure_count,
            'results': results
        }
