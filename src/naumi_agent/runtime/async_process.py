"""Bounded async subprocess output helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

_DEFAULT_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class BoundedProcessOutput:
    stdout: bytes
    returncode: int
    truncated: bool = False


async def read_bounded_stdout(
    process: asyncio.subprocess.Process,
    *,
    max_bytes: int,
    chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
) -> BoundedProcessOutput:
    """Read stdout without allowing a subprocess to grow memory without bound."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    if process.stdout is None:
        if process.returncode is None:
            process.kill()
        await process.wait()
        return BoundedProcessOutput(stdout=b"", returncode=process.returncode or 1)

    stdout = bytearray()
    truncated = False
    while chunk := await process.stdout.read(chunk_bytes):
        remaining = max_bytes - len(stdout)
        if remaining > 0:
            stdout.extend(chunk[:remaining])
        if len(chunk) > remaining and not truncated:
            truncated = True
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
    await process.wait()
    return BoundedProcessOutput(
        stdout=bytes(stdout),
        returncode=process.returncode or 0,
        truncated=truncated,
    )


__all__ = ["BoundedProcessOutput", "read_bounded_stdout"]
