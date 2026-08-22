"""JSON-RPC 2.0 framing for the MCP resource server.

Deliberately dependency-free — no Django, no models, no registry — so the wire
format is testable on its own and cannot grow a hidden dependency on request
state. Everything here is pure: bytes in, plain dicts out.

Two shapes of failure exist and they are NOT the same thing:

* a body that is not a JSON-RPC message at all (unparsable, a scalar, an empty
  or oversized array) is a TRANSPORT failure — the caller is not speaking the
  protocol, and the resource server answers HTTP 400 carrying the JSON-RPC
  error object;
* a well-formed message whose method or params are wrong is a PROTOCOL answer —
  HTTP 200 carrying a JSON-RPC error response.

``handle`` in ``server.py`` makes that call; this module only classifies.
"""
import ujson

# The revision this server implements, and everything it will accept from a
# client. The tools surface is identical across the two, which is why the older
# revision is still negotiable rather than refused.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A batch is answered element by element in one request, so its size is a
# work-amplification budget, not a convenience limit.
MAX_BATCH = 20

PARSE_ERROR_MESSAGE = "Parse error"
INVALID_REQUEST_MESSAGE = "Invalid Request"
INVALID_PARAMS_MESSAGE = "Invalid params"
INTERNAL_ERROR_MESSAGE = "Internal error"


class JsonRpcError(Exception):
    """One JSON-RPC error object, raised where it is detected."""

    def __init__(self, code, message, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


def parse_body(raw):
    """``(messages, is_batch)`` for a raw request body, or raise.

    An empty body is a parse error rather than an empty batch: a client that
    sends nothing has not sent a JSON-RPC message.
    """
    try:
        payload = ujson.loads(raw)
    except Exception:
        raise JsonRpcError(PARSE_ERROR, PARSE_ERROR_MESSAGE)

    if isinstance(payload, dict):
        return [payload], False
    if isinstance(payload, list):
        if not payload or len(payload) > MAX_BATCH:
            raise JsonRpcError(INVALID_REQUEST, INVALID_REQUEST_MESSAGE)
        return payload, True
    raise JsonRpcError(INVALID_REQUEST, INVALID_REQUEST_MESSAGE)


def _valid_id(value):
    """JSON-RPC ids this server will echo: a string or a non-bool integer.

    ``null`` is refused rather than accepted-and-echoed. MCP forbids a null
    request id, and a null-id response is indistinguishable from the envelope
    this server uses to report a body it could not parse at all.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int))


def message_id(msg):
    """The id safe to echo back on an error, or ``None``."""
    if not isinstance(msg, dict):
        return None
    value = msg.get("id", None)
    return value if _valid_id(value) else None


def classify(msg):
    """``"request"``, ``"notification"`` or ``"response"``, or raise.

    A client RESPONSE (a ``result``/``error`` object with no ``method``) is
    accepted and ignored: this server initiates nothing, so nothing it receives
    can be an answer to it, but refusing one would be noisier than dropping it.
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, INVALID_REQUEST_MESSAGE)

    if "method" in msg:
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            raise JsonRpcError(INVALID_REQUEST, INVALID_REQUEST_MESSAGE)
        is_request = "id" in msg
        if is_request and not _valid_id(msg.get("id")):
            raise JsonRpcError(INVALID_REQUEST, INVALID_REQUEST_MESSAGE)
        params = msg.get("params", None)
        if params is not None and not isinstance(params, dict):
            # Only a request can be told; a notification has no response slot.
            if is_request:
                raise JsonRpcError(INVALID_PARAMS, INVALID_PARAMS_MESSAGE)
            return "notification"
        return "request" if is_request else "notification"

    if "result" in msg or "error" in msg:
        return "response"
    raise JsonRpcError(INVALID_REQUEST, INVALID_REQUEST_MESSAGE)


def result_message(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_message(msg_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}
