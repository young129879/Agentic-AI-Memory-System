"""
Anthropic Messages API request handling.

Only what a proxy needs: find the system prompt, add to it, put it back.
The rest of the body is passed through untouched, so features this code has
never heard of (new content block types, new sampling params) keep working.

The one thing worth knowing about this format: the system prompt is NOT a
message. It lives in a top-level `system` field, as either a plain string or
a list of content blocks. Code that looks for it in `messages` finds nothing.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def extract_system(body: Dict[str, Any]) -> str:
    """
    Read the system prompt as plain text.

    Returns "" when absent, which is normal -- not every client sends one.
    """
    system = body.get("system")
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    logger.warning(f"Unexpected system field type: {type(system)}")
    return ""


def append_to_system(body: Dict[str, Any], text: str) -> Dict[str, Any]:
    """
    Append text to the system prompt, returning a new body.

    Appends rather than prepends so the caller's own instructions stay at the
    front, where models weight them most heavily.

    The original shape is preserved: a string system stays a string, a block
    list stays a block list. Rewriting one into the other would invalidate any
    cache_control markers the client set for prompt caching.
    """
    if not text:
        return body

    new_body = dict(body)
    system = body.get("system")

    if system is None:
        new_body["system"] = text
    elif isinstance(system, str):
        new_body["system"] = f"{system}\n\n{text}"
    elif isinstance(system, list):
        new_body["system"] = list(system) + [{"type": "text", "text": text}]
    else:
        logger.warning(f"Cannot append to system of type {type(system)}")
        return body

    return new_body


def last_user_text(body: Dict[str, Any]) -> str:
    """
    The text of the most recent user message.

    This is what memory is retrieved for. Tool results are skipped: they are
    machine output the agent fed back to itself, not something the user said,
    and treating them as queries pollutes retrieval.
    """
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue

        content = msg.get("content")
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
            # Tool-result-only turn: keep looking further back.

    return ""


def assistant_text_from_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Concatenate the text blocks of an assistant reply, ignoring tool_use."""
    if not blocks:
        return ""
    return "\n".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def is_streaming(body: Dict[str, Any]) -> bool:
    return bool(body.get("stream"))


def session_hint(body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> str:
    """
    Derive a stable session id for this conversation.

    Order of preference:
      1. An explicit header, if the client sends one.
      2. metadata.user_id, which Claude Code populates.
      3. A hash of the first user message.

    (3) works because every request replays the whole history, so the opening
    message is a fingerprint that stays constant for the life of a
    conversation and differs between windows. Without it, concurrent windows
    would collapse into one session and corrupt each other's topic blocks.
    """
    if headers:
        lower = {k.lower(): v for k, v in headers.items()}
        for key in ("x-session-id", "x-conversation-id", "x-hmlr-session"):
            value = (lower.get(key) or "").strip()
            if value:
                return value

    metadata = body.get("metadata") or {}
    user_id = (metadata.get("user_id") or "").strip()
    if user_id:
        return f"anthropic-{user_id}"

    messages = body.get("messages") or []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if content:
                import hashlib
                digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
                return f"conv-{digest[:16]}"
            break

    return "default_session"
