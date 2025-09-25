#!/usr/bin/env python3
"""
WebSocket test client for debugging connection issues on EC2.
Can be run from the command line to test WebSocket connectivity.

Usage:
    python test_websocket.py [--url ws://localhost:9001/ws/echo/] [--verbose]
"""

import asyncio
import websockets
import json
import sys
import argparse
import time
import traceback
from datetime import datetime

class WebSocketTester:
    def __init__(self, url, verbose=False):
        self.url = url
        self.verbose = verbose
        self.connection = None

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        print(f"[{timestamp}] [{level}] {message}")

    async def connect(self):
        """Establish WebSocket connection"""
        try:
            self.log(f"Attempting to connect to: {self.url}")
            self.connection = await websockets.connect(
                self.url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10
            )
            self.log(f"Successfully connected to {self.url}", "SUCCESS")
            return True
        except Exception as e:
            self.log(f"Failed to connect: {e}", "ERROR")
            if self.verbose:
                self.log(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return False

    async def receive_messages(self, timeout=5):
        """Receive messages with timeout"""
        messages = []
        try:
            end_time = time.time() + timeout
            while time.time() < end_time:
                try:
                    # Use wait_for to implement timeout on individual receive
                    message = await asyncio.wait_for(
                        self.connection.recv(),
                        timeout=min(1, end_time - time.time())
                    )
                    self.log(f"Received message: {message}", "RECV")
                    messages.append(message)

                    # Try to parse as JSON
                    try:
                        parsed = json.loads(message)
                        self.log(f"Parsed JSON: {json.dumps(parsed, indent=2)}", "JSON")
                    except json.JSONDecodeError:
                        self.log(f"Message is not valid JSON", "WARN")

                except asyncio.TimeoutError:
                    # This is expected when no messages are available
                    continue
                except websockets.exceptions.ConnectionClosed as e:
                    self.log(f"Connection closed while receiving: {e}", "WARN")
                    break
        except Exception as e:
            self.log(f"Error receiving messages: {e}", "ERROR")
            if self.verbose:
                self.log(f"Traceback: {traceback.format_exc()}", "DEBUG")

        return messages

    async def send_message(self, message):
        """Send a message to the server"""
        try:
            if isinstance(message, dict):
                message_str = json.dumps(message)
            else:
                message_str = str(message)

            self.log(f"Sending message: {message_str}", "SEND")
            await self.connection.send(message_str)
            self.log("Message sent successfully", "SUCCESS")
            return True
        except Exception as e:
            self.log(f"Failed to send message: {e}", "ERROR")
            if self.verbose:
                self.log(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return False

    async def test_echo(self):
        """Test the echo functionality"""
        self.log("Starting echo test...", "TEST")

        # Test 1: Send a simple echo message
        test_message = {
            "type": "test",
            "data": "Hello from test client",
            "timestamp": time.time()
        }

        if await self.send_message(test_message):
            # Wait for echo response
            responses = await self.receive_messages(timeout=3)
            if responses:
                self.log(f"Echo test successful - received {len(responses)} response(s)", "SUCCESS")
            else:
                self.log("No echo response received", "WARN")

    async def test_ping_pong(self):
        """Test ping/pong functionality"""
        self.log("Starting ping/pong test...", "TEST")

        ping_message = {"type": "ping"}
        if await self.send_message(ping_message):
            responses = await self.receive_messages(timeout=3)

            # Check if we got a pong
            for response in responses:
                try:
                    data = json.loads(response)
                    if data.get("type") == "pong":
                        self.log("Ping/pong test successful", "SUCCESS")
                        return True
                except:
                    pass

            self.log("No pong response received", "WARN")
            return False

    async def run_all_tests(self):
        """Run all WebSocket tests"""
        self.log("=" * 60, "INFO")
        self.log(f"WebSocket Test Suite - URL: {self.url}", "INFO")
        self.log("=" * 60, "INFO")

        # Connect
        if not await self.connect():
            self.log("Cannot proceed without connection", "ERROR")
            return False

        try:
            # Wait for and log any initial messages (like the hello message)
            self.log("Waiting for initial server messages...", "INFO")
            initial_messages = await self.receive_messages(timeout=2)
            if initial_messages:
                self.log(f"Received {len(initial_messages)} initial message(s)", "INFO")

            # Run tests
            await self.test_echo()
            await asyncio.sleep(0.5)

            await self.test_ping_pong()
            await asyncio.sleep(0.5)

            # Test close
            self.log("Testing graceful close...", "TEST")
            await self.send_message({"type": "close"})
            await asyncio.sleep(1)

            self.log("All tests completed", "SUCCESS")

        except Exception as e:
            self.log(f"Test suite error: {e}", "ERROR")
            if self.verbose:
                self.log(f"Traceback: {traceback.format_exc()}", "DEBUG")

        finally:
            if self.connection and not self.connection.closed:
                self.log("Closing connection...", "INFO")
                await self.connection.close()

        self.log("=" * 60, "INFO")
        return True

    async def interactive_mode(self):
        """Run in interactive mode for manual testing"""
        if not await self.connect():
            return

        self.log("Interactive mode - type 'help' for commands", "INFO")

        # Start a task to receive messages
        async def receive_loop():
            while self.connection and not self.connection.closed:
                try:
                    message = await self.connection.recv()
                    self.log(f"Received: {message}", "RECV")
                except websockets.exceptions.ConnectionClosed:
                    self.log("Connection closed by server", "WARN")
                    break
                except Exception as e:
                    self.log(f"Receive error: {e}", "ERROR")
                    break

        receive_task = asyncio.create_task(receive_loop())

        commands = {
            'help': 'Show this help message',
            'ping': 'Send a ping message',
            'echo <message>': 'Send an echo test message',
            'json <json>': 'Send raw JSON',
            'close': 'Close connection gracefully',
            'quit': 'Exit interactive mode'
        }

        try:
            while True:
                # Read input (this is blocking, but that's ok for CLI)
                try:
                    cmd = input("> ").strip()
                except EOFError:
                    break

                if not cmd:
                    continue

                if cmd == 'help':
                    for cmd_name, desc in commands.items():
                        print(f"  {cmd_name}: {desc}")

                elif cmd == 'quit':
                    break

                elif cmd == 'ping':
                    await self.send_message({"type": "ping"})

                elif cmd.startswith('echo '):
                    message = cmd[5:]
                    await self.send_message({"type": "echo", "data": message})

                elif cmd.startswith('json '):
                    try:
                        json_data = json.loads(cmd[5:])
                        await self.send_message(json_data)
                    except json.JSONDecodeError as e:
                        self.log(f"Invalid JSON: {e}", "ERROR")

                elif cmd == 'close':
                    await self.send_message({"type": "close"})
                    await asyncio.sleep(1)
                    break

                else:
                    # Send as plain text
                    await self.send_message({"type": "message", "text": cmd})

                # Small delay to allow responses to be received
                await asyncio.sleep(0.1)

        finally:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

            if self.connection and not self.connection.closed:
                await self.connection.close()

async def main():
    parser = argparse.ArgumentParser(description='WebSocket connection tester')
    parser.add_argument('--url', '-u',
                       default='ws://localhost:9001/ws/echo/',
                       help='WebSocket URL to test')
    parser.add_argument('--verbose', '-v',
                       action='store_true',
                       help='Enable verbose output')
    parser.add_argument('--interactive', '-i',
                       action='store_true',
                       help='Run in interactive mode')
    parser.add_argument('--host',
                       help='Override host (useful for testing different servers)')
    parser.add_argument('--port',
                       type=int,
                       help='Override port')
    parser.add_argument('--path',
                       default='/ws/echo/',
                       help='WebSocket path')

    args = parser.parse_args()

    # Build URL if components provided
    if args.host:
        protocol = 'ws'
        if args.port == 443:
            protocol = 'wss'
        url = f"{protocol}://{args.host}"
        if args.port:
            url += f":{args.port}"
        url += args.path
    else:
        url = args.url

    tester = WebSocketTester(url, verbose=args.verbose)

    if args.interactive:
        await tester.interactive_mode()
    else:
        await tester.run_all_tests()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
