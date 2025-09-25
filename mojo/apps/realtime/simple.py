from channels.generic.websocket import AsyncWebsocketConsumer
import json
import time
import sys
import traceback
from mojo.helpers import logit

logger = logit.get_logger("realtime", "realtime.log")

class SimpleEchoConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("=== SimpleEchoConsumer.connect() called ===")
        try:
            # Log the connection details
            logger.info(f"Scope type: {type(self.scope)}")
            logger.info(f"Channel name: {getattr(self, 'channel_name', 'NOT SET')}")
            logger.info(f"Channel layer: {getattr(self, 'channel_layer', 'NOT SET')}")

            # Accept the connection
            logger.info("About to call self.accept()")
            accept_result = await self.accept()
            logger.info(f"self.accept() returned: {accept_result} (type: {type(accept_result)})")

            # Prepare the hello message
            hello_msg = {
                "type": "hello",
                "message": "connected",
                "ts": time.time(),
            }
            hello_json = json.dumps(hello_msg)
            logger.info(f"About to send hello message: {hello_json}")

            # Send a greeting
            send_result = await self.send(text_data=hello_json)
            logger.info(f"self.send() returned: {send_result} (type: {type(send_result)})")

            logger.info("=== SimpleEchoConsumer.connect() completed successfully ===")

        except Exception as e:
            logger.error(f"Exception in connect(): {e}")
            logger.error(f"Exception type: {type(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def receive(self, text_data=None, bytes_data=None):
        logger.info("=== SimpleEchoConsumer.receive() called ===")
        logger.info(f"text_data: {text_data} (type: {type(text_data)})")
        logger.info(f"bytes_data: {bytes_data} (type: {type(bytes_data)})")

        try:
            # Parse JSON if text_data provided
            if text_data:
                try:
                    data = json.loads(text_data)
                    logger.info(f"Parsed JSON data: {data}")
                except json.JSONDecodeError as je:
                    logger.error(f"JSON decode error: {je}")
                    error_msg = json.dumps({"type": "error", "message": "invalid json"})
                    await self.send(text_data=error_msg)
                    return
            else:
                data = {}
                logger.info("No text_data, using empty dict")

            msg_type = data.get("type")
            logger.info(f"Message type: {msg_type}")

            if msg_type == "ping":
                logger.info("Handling ping request")
                pong_msg = json.dumps({"type": "pong", "ts": time.time()})
                send_result = await self.send(text_data=pong_msg)
                logger.info(f"Sent pong, result: {send_result}")
                return

            if msg_type == "close":
                logger.info("Handling close request")
                await self.close(code=1000)
                return

            # Echo back whatever was sent
            logger.info("Echoing message back")
            echo_msg = json.dumps({"type": "echo", "data": data, "ts": time.time()})
            send_result = await self.send(text_data=echo_msg)
            logger.info(f"Sent echo, result: {send_result}")

            logger.info("=== SimpleEchoConsumer.receive() completed successfully ===")

        except Exception as e:
            logger.error(f"Exception in receive(): {e}")
            logger.error(f"Exception type: {type(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    async def disconnect(self, close_code):
        logger.info(f"=== SimpleEchoConsumer.disconnect() called with close_code: {close_code} ===")
        # No cleanup; deliberately minimal
        pass

    def __getattribute__(self, name):
        """Override to log any attribute access that might be problematic"""
        try:
            attr = object.__getattribute__(self, name)
            # Only log method calls, not every attribute access
            if name in ['send', 'accept', 'close'] and callable(attr):
                logger.info(f"Accessing method: {name}, type: {type(attr)}")
            return attr
        except AttributeError as e:
            logger.error(f"AttributeError accessing {name}: {e}")
            raise
