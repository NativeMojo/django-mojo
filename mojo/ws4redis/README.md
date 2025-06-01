# Websocket HOWTO

## Authentication

### JWT

Requires an existing JWT token that has gone through authentication process via rest

```json
{
    "action": "auth",
    "kind": "jwt",
    "token": "..."
}
```



### Model Authentication

You can implement custom authentication flows via a model by using the WS4REDIS_AUTHENTICATORS in your django settings.py.

##### WS4REDIS_AUTHENTICATORS

```python
WS4REDIS_AUTHENTICATORS = {
    "mymodel": "myapp.MyModel"
}
```

In your Model you will need to add the following class methods.

This method is used by the async/websocket service to authenticate.
If the model can authenticate the connection it should return dict with kind and pk of the model that is authenticaed.



##### authWS4RedisConnection

This method will authenticate the model, or return None if authentication failed.

```python
@classmethod
def authWS4RedisConnection(cls, auth_data):
    if auth_data and auth_data.token:
        terminal = cls.objects.filter(token=auth_data.token).last()
        if terminal is not None:
            # we now return the terminal credentials to the framework
            return UberDict(
                kind="terminal",
                pk=terminal.id,
                uuid=terminal.tid,
                token=auth_data.token,
                only_one=True,  # only allows one connection at a time
                instance=terminal)
    return None
```



##### canPublishTo

Add this to your Model to validate messages from this connection to be sent to this channel.

```python
@classmethod
def canPublishTo(cls, credentials, msg):
    if credentials:
        return True
    return False
```



##### WS4REDIS_CHANNELS

Map channels to models

```python
WS4REDIS_CHANNELS = {
    "group": "account.Group",
    "chat": "chat.Room",
}
```



##### onWS4RedisMessage

Add this to your Model to allow for handling of messages sent to this channel.

```python
@classmethod
def onWS4RedisMessage(cls, credentials, msg):
    if msg.action == "status":
      cls.createStatusRecord(msg)

```



### URL Params

You can also use params in the url of the websocket.  

**THIS IS NOT RECOMMENDED as the url params are not encrypted and can be easily snooped.**

Include something like the follow in your django settings.py:

```python
def URL_AUTHENTICATOR(ws_con):
    from objict import objict
    token = ws_con.request.GET.get("token", None)
    session_key = ws_con.request.GET.get("session_key", None)
    if token is not None:
      # this example assume the token is used for terminal auth
      # you will still need to implement the Custom Auth flows to handle this
    	ws_con.on_auth(objict(kind="terminal", token=token))
		elif session_key is not None:
      # or alternative is a session
    	ws_con.on_auth(objict(kind="session", token=session_key))

```



## Subscribe

```json
{
    "action": "subscribe",
    "channel": "group",
    "pk": 3,
}
```

### Security

In settins WS4REDIS_CHANNELS, map your channel to a model.
The model should have a classmethod for canSubscribeTo that returns a list of pk they can subscribe to.


## UnSubscribe

```json
{
    "action": "unsubscribe",
    "channel": "group",
    "pk": 3,
}
```


## Publish / Send To

```json
{
    "action": "publish",
    "channel": "group",
    "pk": 3,
    "message": "..."
}
```

### Security

In settins WS4REDIS_CHANNELS, map your channel to a model.
The model should have a classmethod for canPublishTo that returns a list of pk they can publish to.


## Custom Messages

If an unknown action is sent with a channel then the framework will call onWS4RedisMessage on the channel model.

