# AWS TURN for WebRTC

Call `mojo.helpers.aws.kinesis_turn.issue(channel_arn, role="VIEWER")`
after the application authorizes a viewer. The result contains
`ice_servers` for `RTCPeerConnection` and the Unix timestamp `expires_at`.
Use `role="MASTER"` for the other peer. Configure a regional Kinesis Video
Streams single-master signaling channel and grant the server role
`kinesisvideo:GetSignalingChannelEndpoint` and
`kinesisvideo:GetIceServerConfig` for that channel. The boto3 credential chain
supplies AWS authentication.

Keep the channel ARN server-controlled. Return the temporary ICE credentials
with cache disabled and exclude them from logs. This helper issues credentials
only. The application owns authorization, signaling, capture and session
termination.
