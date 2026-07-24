"""In-process serialization for authenticated conversation operations."""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator


DEFAULT_CONVERSATION_LOCK_WAIT_SECONDS = 15.0


class ConversationBusyError(RuntimeError):
    """A safe, key-free signal that a conversation lock timed out."""

    code = "CONVERSATION_BUSY"

    def __init__(self):
        super().__init__(self.code)


class ConversationLockReentryError(RuntimeError):
    """Reject recursive acquisition by the task already holding a key."""

    code = "CONVERSATION_LOCK_REENTRY"

    def __init__(self):
        super().__init__(self.code)


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reference_count: int = 0
    owner_task: asyncio.Task | None = None


class ConversationLockManager:
    """Maintain one cancellation-safe asyncio lock per active conversation.

    This manager is process-local. It deliberately exposes no registry keys and
    does not provide a public manual-release API.
    """

    def __init__(
        self,
        *,
        wait_timeout_seconds: float = DEFAULT_CONVERSATION_LOCK_WAIT_SECONDS,
    ):
        if (
            isinstance(wait_timeout_seconds, bool)
            or not isinstance(wait_timeout_seconds, (int, float))
            or not math.isfinite(wait_timeout_seconds)
            or wait_timeout_seconds <= 0
        ):
            raise ValueError("Conversation lock wait timeout must be positive.")
        self.wait_timeout_seconds = float(wait_timeout_seconds)
        self._entries: dict[str, _LockEntry] = {}
        self._registry_guard = asyncio.Lock()

    @property
    def registry_size_for_test(self) -> int:
        """Return only an entry count for deterministic leak tests."""

        return len(self._entries)

    async def _register(self, conversation_key: str, task: asyncio.Task) -> _LockEntry:
        if not isinstance(conversation_key, str) or not conversation_key:
            raise ValueError("A validated conversation key is required.")
        async with self._registry_guard:
            entry = self._entries.get(conversation_key)
            if entry is None:
                entry = _LockEntry()
                self._entries[conversation_key] = entry
            if entry.owner_task is task:
                raise ConversationLockReentryError()
            entry.reference_count += 1
            return entry

    async def _remove_reference(
        self,
        conversation_key: str,
        entry: _LockEntry,
    ) -> None:
        async with self._registry_guard:
            if entry.reference_count <= 0:
                raise RuntimeError("Conversation lock reference invariant failed.")
            entry.reference_count -= 1
            if (
                entry.reference_count == 0
                and not entry.lock.locked()
                and self._entries.get(conversation_key) is entry
            ):
                self._entries.pop(conversation_key, None)

    async def _remove_reference_safely(
        self,
        conversation_key: str,
        entry: _LockEntry,
    ) -> None:
        """Finish registry cleanup even if the caller is cancelled again."""

        cleanup = asyncio.create_task(
            self._remove_reference(conversation_key, entry)
        )
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        cleanup.result()
        if cancellation is not None:
            raise cancellation

    @asynccontextmanager
    async def hold(self, conversation_key: str) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Conversation locks require an asyncio task.")

        entry = await self._register(conversation_key, task)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    entry.lock.acquire(),
                    timeout=self.wait_timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise ConversationBusyError() from None

            acquired = True
            entry.owner_task = task
            try:
                yield
            finally:
                ownership_valid = entry.owner_task is task
                entry.owner_task = None
                if entry.lock.locked():
                    entry.lock.release()
                acquired = False
                if not ownership_valid:
                    raise RuntimeError("Conversation lock ownership invariant failed.")
        finally:
            # If the context never yielded, the task was cancelled or timed out
            # while waiting and therefore must not release another task's lock.
            if acquired and entry.owner_task is task:
                entry.owner_task = None
                entry.lock.release()
            await self._remove_reference_safely(conversation_key, entry)
