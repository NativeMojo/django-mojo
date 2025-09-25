# django-mojo/testit/ws_client.py
import json
import threading
import time
import queue
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Union

try:
    # websocket-client package
    import websocket  # type: ignore
except Exception as e:  # pragma: no cover
    websocket = None  # Allows import-time even if lib missing; connect() will raise


@dataclass
class WsMessage:
    raw: str
    data: Dict[str, Any]
    received_at: float


def _http_to_ws(url: str, secure: Optional[bool] = None) -> str:
    """
    Convert an http(s) URL to a ws(s) URL. If secure is set, it forces ws/wss.
    """
    url = url.strip()
    if url.startswith("http://"):
        base = url[7:]
        scheme = "wss" if secure is True else "ws"
        return f"{scheme}://{base}"
    if url.startswith("https://"):
        base = url[8:]
        scheme = "wss" if secure is not False else "ws"
        return f"{scheme}://{base}"
    # If it's already ws/wss, leave it alone
    if url.startswith("ws://") or url.startswith("wss://"):
        return url
    # Fallback: assume host with no scheme
    scheme = "wss" if secure else "ws"
    return f"{scheme}://{url}"


def _join_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    path = path.lstrip("/")
    return f"{base}/{path}"


class WsClient:
    """
    A simple test WebSocket client built on websocket-client for testit.

    Features:
    - Connect to a ws:// or wss:// URL
    - Threaded run_forever with callbacks
    - JSON message parsing + queue
    - KISS auth flow: { "type": "authenticate", "token": "<token>", "prefix": "bearer" }
    - subscribe/unsubscribe/ping helpers
    - Wait for message by type with timeout
    - Optional logger compatible with testit logging style

    Example:
        from testit.ws_client import WsClient
        from testit.client import RestClient

        http = RestClient("http://127.0.0.1:8001")
        assert http.login("user", "pass")

        ws_url = WsClient.build_url_from_host(http.host, path="ws/realtime/")
        ws = WsClient(ws_url)
        ws.connect()
        auth = ws.authenticate(http.access_token)  # prefix defaults to "bearer"
        ws.subscribe(f"user:{auth['instance_id']}")
        msg = ws.wait_for_type("subscribed", timeout=5)
        ws.close()
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        subprotocols: Optional[list[str]] = None,
        sslopt: Optional[Dict[str, Any]] = None,
        logger: Optional[Any] = None,
        ping_interval: Optional[float] = None,
        ping_timeout: Optional[float] = None,
        ping_payload: Optional[str] = None,
    ) -> None:
        """
        Create a websocket test client.

        Args:
            url: The ws:// or wss:// URL to connect to.
            headers: Optional headers dict passed to websocket-client.
            subprotocols: Optional list of subprotocols.
            sslopt: Optional ssl options dict (e.g., {"cert_reqs": ssl.CERT_NONE}).
            logger: Optional logger with .info/.error/.exception.
            ping_interval: Seconds between keepalive pings (websocket-client).
            ping_timeout: Seconds to wait for the pong (websocket-client).
            ping_payload: Optional ping payload.
        """
        self.url = url
        self.headers = headers or {}
        self.subprotocols = subprotocols
        self.sslopt = sslopt or {}
        self.logger = logger
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.ping_payload = ping_payload

        self._ws_app: Optional["websocket.WebSocketApp"] = None  # type: ignore[name-defined]
        self._thread: Optional[threading.Thread] = None
        self._open_event = threading.Event()
        self._closed_event = threading.Event()
        self._messages: "queue.Queue[WsMessage]" = queue.Queue()
        self._last_error: Optional[BaseException] = None

    # ----------------------------------------------------------------------------------
    # Static helpers
    # ----------------------------------------------------------------------------------
    @staticmethod
    def build_url_from_host(
        host: str,
        path: str = "ws/realtime/",
        secure: Optional[bool] = None,
    ) -> str:
        """
        Build a WebSocket URL from an HTTP host base and a relative path.

        Args:
            host: Base host URL used by REST client (e.g., "http://127.0.0.1:8001")
            path: Relative websocket path (default "ws/realtime/")
            secure: Optional bool to force ws (False) or wss (True)

        Returns:
            str: ws:// or wss:// URL
        """
        ws_base = _http_to_ws(host, secure=secure)
        return _join_url(ws_base, path)

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        """
        Connect to the WebSocket server and start the I/O thread.

        Raises:
            RuntimeError if websocket-client is not installed
            TimeoutError if connection didn't open within timeout
        """
        if websocket is None:  # pragma: no cover
            raise RuntimeError("websocket-client is required to use WsClient")

        if self._ws_app is not None:
            return

        def _on_open(ws):
            if self.logger:
                self.logger.info(f"[ws] open: {self.url}")
            self._open_event.set()

        def _on_message(ws, message: str):
            now = time.time()
            try:
                data = json.loads(message) if isinstance(message, str) else {}
            except Exception:
                data = {}
            msg = WsMessage(raw=message, data=data, received_at=now)
            self._messages.put(msg)
            if self.logger:
                typ = data.get("type") if isinstance(data, dict) else None
                self.logger.info(f"[ws] recv: type={typ} at={now}")

        def _on_error(ws, error: BaseException):
            self._last_error = error
            if self.logger:
                self.logger.exception(f"[ws] error: {error}")

        def _on_close(ws, status_code, msg):
            if self.logger:
                self.logger.info(f"[ws] close: code={status_code} msg={msg}")
            self._closed_event.set()

        self._ws_app = websocket.WebSocketApp(  # type: ignore[attr-defined]
            self.url,
            header=[f"{k}: {v}" for k, v in self.headers.items()],
            subprotocols=self.subprotocols,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )

        def _runner():
            try:
                self._ws_app.run_forever(  # type: ignore[union-attr]
                    sslopt=self.sslopt,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    ping_payload=self.ping_payload,
                )
            finally:
                self._closed_event.set()

        self._thread = threading.Thread(target=_runner, name="WsClientThread", daemon=True)
        self._thread.start()

        if not self._open_event.wait(timeout=timeout):
            raise TimeoutError(f"WebSocket did not open within {timeout}s")

    def close(self, wait: float = 2.0) -> None:
        """Close the connection and join the thread."""
        if self._ws_app is not None:
            try:
                self._ws_app.close()  # type: ignore[union-attr]
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=wait)

    # ----------------------------------------------------------------------------------
    # Send helpers
    # ----------------------------------------------------------------------------------
    def send_text(self, text: str) -> None:
        if not self._ws_app:
            raise RuntimeError("WebSocket not connected")
        self._ws_app.send(text)  # type: ignore[union-attr]
        if self.logger:
            self.logger.info(f"[ws] send text: {text[:160]}")

    def send_json(self, obj: Dict[str, Any]) -> None:
        self.send_text(json.dumps(obj))

    def authenticate(self, token: str, prefix: str = "bearer", wait: bool = True, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Send the authentication message and optionally wait for auth_success (or error).

        Returns:
            dict: auth_success message data

        Raises:
            TimeoutError if not received within timeout
            RuntimeError if error received
        """
        self.send_json({"type": "authenticate", "token": token, "prefix": prefix})
        if not wait:
            return {}
        msg = self.wait_for_types({"auth_success", "error", "auth_timeout"}, timeout=timeout)
        if msg.data.get("type") != "auth_success":
            raise RuntimeError(f"Auth failed: {msg.data}")
        return msg.data

    def subscribe(self, topic: str, wait: bool = True, timeout: float = 5.0) -> Dict[str, Any]:
        self.send_json({"action": "subscribe", "topic": topic})
        if not wait:
            return {}
        msg = self.wait_for_types({"subscribed", "error"}, timeout=timeout, predicate=lambda d: d.get("topic") == topic)
        if msg.data.get("type") != "subscribed":
            raise RuntimeError(f"Subscribe failed: {msg.data}")
        return msg.data

    def unsubscribe(self, topic: str, wait: bool = True, timeout: float = 5.0) -> Dict[str, Any]:
        self.send_json({"action": "unsubscribe", "topic": topic})
        if not wait:
            return {}
        msg = self.wait_for_types({"unsubscribed", "error"}, timeout=timeout, predicate=lambda d: d.get("topic") == topic)
        if msg.data.get("type") != "unsubscribed":
            raise RuntimeError(f"Unsubscribe failed: {msg.data}")
        return msg.data

    def ping(self, wait: bool = True, timeout: float = 5.0) -> Dict[str, Any]:
        self.send_json({"action": "ping"})
        if not wait:
            return {}
        msg = self.wait_for_type("pong", timeout=timeout)
        return msg.data

    # ----------------------------------------------------------------------------------
    # Receive helpers
    # ----------------------------------------------------------------------------------
    def clear_messages(self) -> None:
        """Drain the message queue."""
        try:
            while True:
                self._messages.get_nowait()
        except queue.Empty:
            return

    def recv_next(self, timeout: float = 5.0) -> WsMessage:
        """
        Receive the next message from the queue with timeout.
        Raises queue.Empty on timeout.
        """
        return self._messages.get(timeout=timeout)

    def wait_for_type(
        self,
        message_type: str,
        timeout: float = 10.0,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> WsMessage:
        """
        Wait for the first message with the given type.

        Args:
            message_type: e.g., 'auth_success', 'error'
            timeout: seconds
            predicate: optional additional filter function taking message data

        Returns:
            WsMessage
        """
        return self.wait_for_types({message_type}, timeout=timeout, predicate=predicate)

    def wait_for_types(
        self,
        types: set[str],
        timeout: float = 10.0,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> WsMessage:
        """
        Wait for the first message whose type is in the provided set.

        Args:
            types: set of acceptable types
            timeout: seconds
            predicate: optional additional filter over message data

        Returns:
            WsMessage

        Raises:
            TimeoutError if no matching message arrives within timeout
        """
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self._messages.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                break
            data = msg.data if isinstance(msg.data, dict) else {}
            t = data.get("type")
            if t in types and (predicate is None or predicate(data)):
                return msg
        raise TimeoutError(f"Timed out waiting for message types {types} within {timeout}s")

    # ----------------------------------------------------------------------------------
    # Context manager
    # ----------------------------------------------------------------------------------
    def __enter__(self) -> "WsClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
