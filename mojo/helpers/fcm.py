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

    def __init__(self, service_account_json):
        """
        Initialize FCM client with service account credentials.

        Args:
            service_account_json: Dict or JSON string with service account credentials
        """
        if isinstance(service_account_json, str):
            self.credentials = json.loads(service_account_json)
        else:
            self.credentials = service_account_json

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
        response = requests.post(
            self.OAUTH_ENDPOINT,
            data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': signed_jwt
            }
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get access token: {response.text}")

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
        access_token = self._get_access_token()

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

        # Send request
        url = self.FCM_ENDPOINT.format(project_id=self.project_id)
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response = requests.post(
            url,
            headers=headers,
            json={'message': message}
        )
        resp_msg = response.json() if response.text else {}
        if settings.LOG_PUSH_MESSAGES:
            from mojo.helpers import logit
            logit.info("FCM PUSH", "sending:", message, "received:", response.json())
        # Parse response
        if response.status_code == 200:
            return {
                'success': True,
                'message_id': resp_msg.get('name'),
                'status_code': response.status_code
            }
        else:
            return {
                'success': False,
                'status_code': response.status_code,
                'error': resp_msg.get('error', {}),
                'message': resp_msg.get('error', {}).get('message', 'Unknown error')
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
