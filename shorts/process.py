"""Bounded media subprocesses with cancellation of the complete process group."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal


async def terminate_group(proc: asyncio.subprocess.Process, drained: asyncio.Future) -> None:
    """Stop a session and await already-owned stream readers (also for streaming renders)."""
    with contextlib.suppress(ProcessLookupError):
        if os.name == 'posix':
            os.killpg(proc.pid, signal.SIGTERM)
        elif proc.returncode is None:
            proc.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(drained), 3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            if os.name == 'posix':
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        await drained
    await proc.wait()


async def communicate(*args: str, timeout: float = 600, cwd: str | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == 'posix',
        cwd=cwd,
    )
    pending = asyncio.create_task(proc.communicate())
    try:
        out, err = await asyncio.wait_for(asyncio.shield(pending), timeout)
        return proc.returncode or 0, out, err
    except BaseException:
        await terminate_group(proc, pending)
        raise


async def run(*args: str, timeout: float = 600, cwd: str | None = None) -> str:
    code, out, err = await communicate(*args, timeout=timeout, cwd=cwd)
    if code:
        raise RuntimeError(err.decode('utf-8', 'replace')[-500:])
    return out.decode('utf-8', 'replace')
