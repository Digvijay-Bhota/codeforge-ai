"""Bounded capture of a child process's output streams."""

from __future__ import annotations

import asyncio

# Per-stream cap on captured output.
MAX_OUTPUT_BYTES: int = 64 * 1024
_READ_CHUNK: int = 64 * 1024


class BoundedBuffer:
    """Keeps the first *limit* bytes of a stream and discards the rest."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        # Keep reading past the limit so the child never blocks on a full pipe.
        while chunk := await stream.read(_READ_CHUNK):
            room = self.limit - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def text(self) -> str:
        out = self.data.decode("utf-8", errors="replace")
        if self.truncated:
            out += f"\n... (output truncated at {self.limit} bytes)"
        return out
