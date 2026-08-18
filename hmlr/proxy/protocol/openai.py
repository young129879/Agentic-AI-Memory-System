"""
OpenAI Chat Completions request handling.

Same job as the Anthropic adapter, different shape. The differences that
matter to a proxy:

    system prompt   Anthropic: top-level `system` field
                    OpenAI:    a message with role="system", usually first
                               (newer models call it "developer")

    message content Anthropic: always a list of blocks
                    OpenAI:    a plain string, or a list of parts for
                               multimodal input

    tool results    Anthropic: a user message containing tool_result blocks
                    OpenAI:    a separate role="tool" message

    stream usage    Anthropic: always reported
                    OpenAI:    only when stream_options.include_usage is set

Everything else is passed through untouched.
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Both are treated as the system slot; "developer" is what newer OpenAI
# models use for the same purpose.
SYSTEM_ROLES = ("system", "developer")


def _text_of(content: Any) -> str:
    """Flatten OpenAI content, which is a string or a list of typed parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def extract_system(body: Dict[str, Any]) -> str:
    """Concatenate every system/developer message, in order."""
    messages = body.get("messages") or []
    parts = [
        _text_of(m.get("content"))
        for m in messages
        if isinstance(m, dict) and m.get("role") in SYSTEM_ROLES
    ]
    return "\n\n".join(p for p in parts if p)


def append_to_system(body: Dict[str, Any], text: str) -> Dict[str, Any]:
    """
    Append text to the system prompt, returning a new body.

    Appends to the *last* system message rather than inserting a new one:
    OpenAI's prompt caching keys on an exact prefix, and inserting a message
    shifts every following message, invalidating the whole cache.

    When there is no system message at all, one is prepended -- the only
    position where it is still a prefix.
    """
    if not text:
        return body

    new_body = dict(body)
    messages = [dict(m) if isinstance(m, dict) else m
                for m in (body.get("messages") or [])]

    last_system = None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") in SYSTEM_ROLES:
            last_system = i

    if last_system is None:
        messages.insert(0, {"role": "system", "content": text})
    else:
        msg = messages[last_system]
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = f"{content}\n\n{text}"
        elif isinstance(content, list):
            msg["content"] = list(content) + [{"type": "text", "text": text}]
        else:
            msg["content"] = text

    new_body["messages"] = messages
    return new_body


def last_user_text(body: Dict[str, Any]) -> str:
    """
    Text of the most recent user message.

    role="tool" messages are skipped: they are the agent's own tool output
    fed back to itself, not something the user asked, and retrieving memory
    for them pollutes the results.
    """
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _text_of(msg.get("content")).strip()
        if text:
            return text
    return ""


def is_streaming(body: Dict[str, Any]) -> bool:
    return bool(body.get("stream"))


def session_hint(body: Dict[str, Any],
                 headers: Optional[Dict[str, str]] = None) -> str:
    """
    Derive a stable session id.

    Preference: explicit header, then the `user` field, then a hash of the
    first user message. The hash works because every request replays the
    whole history, so the opening message is constant for a conversation and
    differs between windows.
    """
    if headers:
        lower = {k.lower(): v for k, v in headers.items()}
        for key in ("x-session-id", "x-conversation-id", "x-hmlr-session"):
            value = (lower.get(key) or "").strip()
            if value:
                return value

    user = (body.get("user") or "").strip()
    if user:
        return f"openai-{user}"

    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _text_of(msg.get("content"))
            if text:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                return f"conv-{digest[:16]}"
            break

    return "default_session"


def text_from_non_streaming(body: Dict[str, Any]) -> str:
    """Assistant text from a Chat Completions response."""
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    return _text_of(message.get("content")).strip()
