"""
Embeddings helper — provider-agnostic text embeddings.

Usage:
    from mojo.helpers import embeddings

    vectors = embeddings.embed_texts(["first doc", "second doc"])
    vector = embeddings.embed_query("search terms")
    if embeddings.is_available():
        ...

Settings used:
    EMBEDDINGS_PROVIDER    # "bedrock" (default) or "mock"
    EMBEDDINGS_DIM         # vector dimensions (default 1024)
    BEDROCK_REGION         # falls back to AWS_REGION
    BEDROCK_EMBED_MODEL    # default "amazon.titan-embed-text-v2:0"
    AWS_KEY / AWS_SECRET   # shared AWS credentials (mojo.helpers.aws)

The "mock" provider produces deterministic hash-derived unit vectors —
identical text always embeds identically. It exists for tests and offline
development; it has no semantic knowledge.
"""
import hashlib
import json
import math
import struct

from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger(__name__, "embeddings.log")

DEFAULT_DIM = 1024
DEFAULT_BEDROCK_MODEL = "amazon.titan-embed-text-v2:0"
# Titan V2 caps input at 8k tokens; cap characters well below that so a
# pathological input fails soft (truncated) instead of erroring the batch.
MAX_INPUT_CHARS = 20000


def get_dim():
    return settings.get("EMBEDDINGS_DIM", DEFAULT_DIM, kind="int")


def get_provider():
    """Return the configured provider instance, or None if misconfigured."""
    name = settings.get("EMBEDDINGS_PROVIDER", "bedrock")
    if name == "mock":
        return MockProvider()
    if name == "bedrock":
        return BedrockProvider()
    logger.error(f"unknown EMBEDDINGS_PROVIDER '{name}'")
    return None


def is_available():
    provider = get_provider()
    return provider is not None and provider.is_available()


def embed_texts(texts):
    """Embed a list of texts. Returns a list of float vectors, same order."""
    provider = get_provider()
    if provider is None or not provider.is_available():
        raise RuntimeError("no embeddings provider available")
    return provider.embed([(t or "")[:MAX_INPUT_CHARS] for t in texts])


def embed_query(text):
    """Embed a single query string."""
    return embed_texts([text])[0]


class BedrockProvider:
    name = "bedrock"

    def __init__(self):
        self.model_id = settings.get("BEDROCK_EMBED_MODEL", DEFAULT_BEDROCK_MODEL)
        self.region = settings.get("BEDROCK_REGION", None) or settings.get("AWS_REGION", "us-east-1")
        self.access_key = settings.get("AWS_KEY", None)
        self.secret_key = settings.get("AWS_SECRET", None)

    def is_available(self):
        return bool(self.access_key and self.secret_key)

    def _client(self):
        from mojo.helpers.aws.client import get_session
        session = get_session(self.access_key, self.secret_key, self.region)
        return session.client("bedrock-runtime")

    def embed(self, texts):
        # Titan embeddings accept one input per invoke — loop the batch.
        client = self._client()
        dim = get_dim()
        vectors = []
        for text in texts:
            body = json.dumps({"inputText": text, "dimensions": dim})
            resp = client.invoke_model(modelId=self.model_id, body=body)
            data = json.loads(resp["body"].read())
            vectors.append(data["embedding"])
        return vectors


class MockProvider:
    name = "mock"
    model_id = "mock"

    def is_available(self):
        return True

    def embed(self, texts):
        return [self._vector(t) for t in texts]

    def _vector(self, text):
        dim = get_dim()
        seed = text.encode("utf-8")
        values = []
        counter = 0
        while len(values) < dim:
            digest = hashlib.sha512(seed + struct.pack("<I", counter)).digest()
            for offset in range(0, len(digest) - 1, 2):
                if len(values) >= dim:
                    break
                values.append(struct.unpack_from("<h", digest, offset)[0] / 32768.0)
            counter += 1
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]
