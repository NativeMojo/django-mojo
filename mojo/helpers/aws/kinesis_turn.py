"""Issue short-lived WebRTC ICE configuration from an AWS signaling channel."""

import re
import time
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError

from .client import get_client


_CHANNEL = re.compile(r"arn:aws:kinesisvideo:([a-z0-9-]+):[0-9]{12}:channel/[A-Za-z0-9_.-]+/[0-9]+")


class TurnUnavailable(RuntimeError):
    """The configured TURN provider could not issue credentials."""


def issue(channel_arn, *, role, session=None, client_factory=get_client):
    """Return RTCIceServer dictionaries and Unix expiry after app authorization.

    The server role needs GetSignalingChannelEndpoint and GetIceServerConfig on
    this channel. This helper creates neither signaling sessions nor recordings.
    """
    match = _CHANNEL.fullmatch(channel_arn) if isinstance(channel_arn, str) else None
    if not match or role not in ("MASTER", "VIEWER"):
        raise ValueError("Invalid Kinesis TURN channel or role")
    region = match[1]
    started = int(time.time())
    try:
        control = client_factory("kinesisvideo", region=region, session=session,
                                 timeout=3, max_attempts=0)
        endpoints = control.get_signaling_channel_endpoint(
            ChannelARN=channel_arn,
            SingleMasterChannelEndpointConfiguration={"Protocols": ["HTTPS"], "Role": role},
        )["ResourceEndpointList"]
        endpoint = next(row["ResourceEndpoint"] for row in endpoints if row["Protocol"] == "HTTPS")
        url = urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or not url.hostname.endswith(f".kinesisvideo.{region}.amazonaws.com")
                or url.query or url.fragment or url.port not in (None, 443)):
            raise ValueError("invalid endpoint")
        signaling = client_factory("kinesis-video-signaling", region=region, session=session,
                                   endpoint_url=endpoint, timeout=3, max_attempts=0)
        rows = signaling.get_ice_server_config(ChannelARN=channel_arn)["IceServerList"]
        servers = []
        ttl = 3600
        for row in rows:
            urls, username, credential, lifetime = (
                row["Uris"], row["Username"], row["Password"], row["Ttl"],
            )
            if (not isinstance(urls, list) or not urls
                    or not all(isinstance(uri, str) and uri.startswith(("turn:", "turns:")) for uri in urls)
                    or not all(isinstance(value, str) and value for value in (username, credential))
                    or type(lifetime) is not int or lifetime <= 0):
                raise ValueError("invalid ICE credentials")
            servers.append({"urls": urls, "username": username, "credential": credential})
            ttl = min(ttl, lifetime)
        if not servers or started + ttl <= time.time():
            raise ValueError("missing ICE credentials")
        return {"ice_servers": servers, "expires_at": started + ttl}
    except (BotoCoreError, ClientError, KeyError, StopIteration, TypeError, ValueError):
        raise TurnUnavailable("Kinesis TURN credentials unavailable") from None
