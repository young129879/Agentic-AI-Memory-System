"""
SSE tap.

The proxy has to do two things with a streamed reply that pull in opposite
directions: forward every byte to the client immediately, and hold the whole
reply so it can be written to memory. Buffering to do the second would
destroy the first.

So bytes are forwarded untouched and a parser watches them go past. The
client sees no added latency; the parser's output is only used after the
stream ends.

The subtlety is that SSE events do not align with chunk boundaries. A single
`data:` line can arrive split across three chunks, so events are reassembled
from a running buffer rather than parsed per chunk.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AnthropicStreamTap:
    """
    Accumulates an Anthropic message stream while it is being forwarded.

    Usage:
        tap = AnthropicStreamTap()
        async for chunk in upstream:
            tap.feed(chunk)
            yield chunk          # client is never made to wait
        text = tap.assistant_text
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._text_parts: List[str] = []
        self._tool_uses: List[Dict[str, Any]] = []
        self.usage: Dict[str, int] = {}
        self.stop_reason: Optional[str] = None
        self.model: Optional[str] = None
        self.parse_errors = 0

    @property
    def assistant_text(self) -> str:
        return "".join(self._text_parts).strip()

    @property
    def tool_use_count(self) -> int:
        return len(self._tool_uses)

    def feed(self, chunk: bytes) -> None:
        """
        Consume a chunk of the raw stream.

        Never raises: a parse failure costs one turn of memory, while an
        exception here would break the response the user is reading.
        """
        try:
            self._buffer += chunk
            while b"\n\n" in self._buffer:
                raw_event, self._buffer = self._buffer.split(b"\n\n", 1)
                self._handle_event(raw_event)
        except Exception as e:
            self.parse_errors += 1
            logger.debug(f"Stream tap error (ignored): {e}")

    def finish(self) -> None:
        """Flush a trailing event that arrived without its blank-line terminator."""
        if self._buffer.strip():
            try:
                self._handle_event(self._buffer)
            except Exception as e:
                logger.debug(f"Stream tap flush error (ignored): {e}")
            self._buffer = b""

    def _handle_event(self, raw_event: bytes) -> None:
        payload = None
        for line in raw_event.split(b"\n"):
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                break
        if not payload or payload == b"[DONE]":
            return

        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.parse_errors += 1
            return

        if not isinstance(data, dict):
            return

        event_type = data.get("type")

        if event_type == "message_start":
            message = data.get("message") or {}
            self.model = message.get("model")
            # Anthropic reports input tokens here and output tokens at the
            # end, so usage is merged rather than replaced.
            self.usage.update(message.get("usage") or {})

        elif event_type == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                })

        elif event_type == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._text_parts.append(delta.get("text", ""))

        elif event_type == "message_delta":
            self.usage.update(data.get("usage") or {})
            self.stop_reason = (data.get("delta") or {}).get("stop_reason")

        elif event_type == "error":
            logger.warning(f"Upstream stream error: {data.get('error')}")


def text_from_non_streaming(body: Dict[str, Any]) -> str:
    """Assistant text from a non-streamed Messages response."""
    content = body.get("content") or []
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


class OpenAIStreamTap:
    """
    Same idea as AnthropicStreamTap, for Chat Completions.

    Differences that matter:
      - text arrives as choices[0].delta.content, not a typed delta
      - the stream ends with a literal `data: [DONE]` sentinel
      - usage is only present when the caller set stream_options.include_usage
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._text_parts: List[str] = []
        self._tool_call_ids: set = set()
        self.usage: Dict[str, int] = {}
        self.finish_reason: Optional[str] = None
        self.model: Optional[str] = None
        self.parse_errors = 0
        self.saw_done = False

    @property
    def assistant_text(self) -> str:
        return "".join(self._text_parts).strip()

    @property
    def tool_use_count(self) -> int:
        return len(self._tool_call_ids)

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk
            while b"\n\n" in self._buffer:
                raw_event, self._buffer = self._buffer.split(b"\n\n", 1)
                self._handle_event(raw_event)
        except Exception as e:
            self.parse_errors += 1
            logger.debug(f"Stream tap error (ignored): {e}")

    def finish(self) -> None:
        if self._buffer.strip():
            try:
                self._handle_event(self._buffer)
            except Exception as e:
                logger.debug(f"Stream tap flush error (ignored): {e}")
            self._buffer = b""

    def _handle_event(self, raw_event: bytes) -> None:
        payload = None
        for line in raw_event.split(b"\n"):
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                break
        if not payload:
            return
        if payload == b"[DONE]":
            self.saw_done = True
            return

        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.parse_errors += 1
            return

        if not isinstance(data, dict):
            return

        if data.get("model"):
            self.model = data["model"]
        if data.get("usage"):
            self.usage.update(data["usage"])

        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                self._text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        self._text_parts.append(part.get("text", ""))

            for call in delta.get("tool_calls") or []:
                if isinstance(call, dict):
                    # Index, not id: ids arrive only on the first fragment of
                    # each call, while index is present throughout.
                    self._tool_call_ids.add(call.get("index", call.get("id")))
