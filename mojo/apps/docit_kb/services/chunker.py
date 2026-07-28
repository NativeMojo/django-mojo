"""
Markdown chunker for the docit knowledge base.

Splits page markdown into heading-scoped chunks suitable for embedding: one
chunk per #/##/### section, carrying a breadcrumb heading like
"Install > Quickstart". Oversized sections are hard-split at paragraph
boundaries. Deterministic — the same content always yields the same chunks.
"""
import hashlib
import re

from objict import objict

MAX_CHUNK_CHARS = 4000
HEADING_RE = re.compile(r'^(#{1,3})\s+(\S.*)$')


def chunk_markdown(content):
    """Split markdown into an ordered list of objict(position, heading, content, content_hash)."""
    chunks = []
    for heading, text in _split_sections(content or ""):
        for piece in _split_oversize(text):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(objict(
                position=len(chunks),
                heading=heading[:300],
                content=piece,
                content_hash=hash_chunk(heading, piece),
            ))
    return chunks


def hash_chunk(heading, content):
    return hashlib.sha256(f"{heading}\n{content}".encode("utf-8")).hexdigest()


def _split_sections(content):
    """Return [(breadcrumb, text)] per heading section; preamble before any heading included."""
    trail = {}
    sections = []
    current_heading = ""
    current_lines = []
    in_fence = False
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else HEADING_RE.match(line)
        if match:
            sections.append((current_heading, "\n".join(current_lines)))
            level = len(match.group(1))
            trail[level] = match.group(2).strip()
            for deeper in [l for l in trail if l > level]:
                del trail[deeper]
            current_heading = " > ".join(trail[l] for l in sorted(trail))
            current_lines = [line]
        else:
            current_lines.append(line)
    sections.append((current_heading, "\n".join(current_lines)))
    return sections


def _split_oversize(text):
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    pieces = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > MAX_CHUNK_CHARS and current:
            pieces.append(current)
            current = para
        else:
            current = candidate
    if current:
        pieces.append(current)
    # A single paragraph longer than the cap passes through whole; the
    # embeddings helper truncates pathological inputs rather than erroring.
    return pieces
