from channels.generic.websocket import AsyncWebsocketConsumer
import json
import time

class SimpleEchoConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        # Send a small greeting so you can confirm server->client frames are flowing
        await self.send(text_data=json.dumps({
            "type": "hello",
            "message": "connected",
            "ts": time.time(),
        }))

    async def receive(self, text_data=None, bytes_data=None):
        # Minimal JSON handling; no auth, no channel layer, no DB
        try:
            data = json.loads(text_data) if text_data else {}
        except Exception:
            await self.send(text_data=json.dumps({"type": "error", "message": "invalid json"}))
            return

        msg_type = data.get("type")

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong", "ts": time.time()}))
            return

        if msg_type == "close":
            await self.close(code=1000)
            return

        # Echo back whatever was sent
        await self.send(text_data=json.dumps({"type": "echo", "data": data, "ts": time.time()}))

    async def disconnect(self, close_code):
        # No cleanup; deliberately minimal
        pass
