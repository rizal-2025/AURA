import asyncio
import unittest

from app.core.conversation_lock_manager import (
    ConversationBusyError,
    ConversationLockManager,
    ConversationLockReentryError,
)


class ConversationLockManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_never_overlaps(self):
        manager = ConversationLockManager()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        second_entered = asyncio.Event()

        async def first():
            async with manager.hold("owner:session"):
                first_entered.set()
                await release_first.wait()

        async def second():
            second_started.set()
            async with manager.hold("owner:session"):
                second_entered.set()

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await second_started.wait()
        await asyncio.sleep(0)
        self.assertFalse(second_entered.is_set())
        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertTrue(second_entered.is_set())
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_different_keys_execute_concurrently(self):
        manager = ConversationLockManager()
        entered = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def worker(index, key):
            async with manager.hold(key):
                entered[index].set()
                await release.wait()

        tasks = [
            asyncio.create_task(worker(0, "owner:one")),
            asyncio.create_task(worker(1, "owner:two")),
        ]
        await asyncio.gather(*(event.wait() for event in entered))
        self.assertEqual(manager.registry_size_for_test, 2)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_established_holder_cannot_be_overtaken(self):
        manager = ConversationLockManager()
        order = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            async with manager.hold("owner:ordered"):
                order.append("first-enter")
                first_entered.set()
                await release_first.wait()
                order.append("first-exit")

        async def second():
            async with manager.hold("owner:ordered"):
                order.append("second-enter")

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(order, ["first-enter", "first-exit", "second-enter"])

    async def test_exception_releases_lock(self):
        manager = ConversationLockManager()
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            async with manager.hold("owner:failure"):
                raise RuntimeError("synthetic")

        async with manager.hold("owner:failure"):
            pass
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_holder_cancellation_releases_lock(self):
        manager = ConversationLockManager()
        entered = asyncio.Event()

        async def holder():
            async with manager.hold("owner:cancel-holder"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(holder())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        async with manager.hold("owner:cancel-holder"):
            pass
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_waiter_cancellation_cleans_reference_without_deleting_holder(self):
        manager = ConversationLockManager()
        entered = asyncio.Event()
        release = asyncio.Event()
        waiter_started = asyncio.Event()

        async def holder():
            async with manager.hold("owner:cancel-waiter"):
                entered.set()
                await release.wait()

        async def waiter():
            waiter_started.set()
            async with manager.hold("owner:cancel-waiter"):
                pass

        holder_task = asyncio.create_task(holder())
        await entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await waiter_started.wait()
        await asyncio.sleep(0)
        waiter_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter_task
        self.assertEqual(manager.registry_size_for_test, 1)
        release.set()
        await holder_task
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_timeout_cleans_waiter_reference(self):
        manager = ConversationLockManager(wait_timeout_seconds=0.01)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with manager.hold("owner:timeout"):
                entered.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await entered.wait()
        with self.assertRaises(ConversationBusyError):
            async with manager.hold("owner:timeout"):
                self.fail("timed-out waiter must not enter")
        self.assertEqual(manager.registry_size_for_test, 1)
        release.set()
        await holder_task
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_entry_remains_while_waiter_exists(self):
        manager = ConversationLockManager()
        entered = asyncio.Event()
        release = asyncio.Event()
        waiter_entered = asyncio.Event()

        async def holder():
            async with manager.hold("owner:waiter"):
                entered.set()
                await release.wait()

        async def waiter():
            async with manager.hold("owner:waiter"):
                waiter_entered.set()

        holder_task = asyncio.create_task(holder())
        await entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        self.assertEqual(manager.registry_size_for_test, 1)
        self.assertFalse(waiter_entered.is_set())
        release.set()
        await asyncio.gather(holder_task, waiter_task)
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_same_task_reentrancy_is_rejected_without_deadlock(self):
        manager = ConversationLockManager()
        async with manager.hold("owner:reentry"):
            with self.assertRaises(ConversationLockReentryError):
                async with manager.hold("owner:reentry"):
                    self.fail("recursive acquisition must not enter")
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_high_cardinality_and_repeated_keys_do_not_leak(self):
        manager = ConversationLockManager()

        async def use(key):
            async with manager.hold(key):
                await asyncio.sleep(0)

        await asyncio.gather(*(use(f"owner:{index}") for index in range(250)))
        for _ in range(100):
            await use("owner:repeated")
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_public_api_has_no_manual_release(self):
        manager = ConversationLockManager()
        self.assertFalse(hasattr(manager, "release"))

    async def test_release_immediately_before_timeout_allows_one_clean_entry(self):
        manager = ConversationLockManager(wait_timeout_seconds=0.25)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_started = asyncio.Event()
        waiter_entries = 0

        async def holder():
            async with manager.hold("owner:before-timeout"):
                holder_entered.set()
                await release_holder.wait()

        async def waiter():
            nonlocal waiter_entries
            waiter_started.set()
            async with manager.hold("owner:before-timeout"):
                waiter_entries += 1

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await waiter_started.wait()
        await asyncio.sleep(0)
        asyncio.get_running_loop().call_later(0.20, release_holder.set)

        await asyncio.wait_for(
            asyncio.gather(holder_task, waiter_task),
            timeout=1,
        )
        self.assertEqual(waiter_entries, 1)
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_release_at_timeout_boundary_is_consistent_and_leak_free(self):
        timeout = 0.03
        manager = ConversationLockManager(wait_timeout_seconds=timeout)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_started = asyncio.Event()
        waiter_entries = 0

        async def holder():
            async with manager.hold("owner:timeout-boundary"):
                holder_entered.set()
                await release_holder.wait()

        async def waiter():
            nonlocal waiter_entries
            waiter_started.set()
            try:
                async with manager.hold("owner:timeout-boundary"):
                    waiter_entries += 1
            except ConversationBusyError:
                return "busy"
            return "acquired"

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await waiter_started.wait()
        asyncio.get_running_loop().call_later(timeout, release_holder.set)

        result = await asyncio.wait_for(waiter_task, timeout=1)
        await asyncio.wait_for(holder_task, timeout=1)
        self.assertIn(result, {"busy", "acquired"})
        self.assertEqual(waiter_entries, 1 if result == "acquired" else 0)
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_cancellation_at_acquisition_boundary_leaves_no_orphaned_lock(self):
        manager = ConversationLockManager()
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_started = asyncio.Event()
        waiter_entries = 0

        async def holder():
            async with manager.hold("owner:cancel-boundary"):
                holder_entered.set()
                await release_holder.wait()

        async def waiter():
            nonlocal waiter_entries
            waiter_started.set()
            async with manager.hold("owner:cancel-boundary"):
                waiter_entries += 1

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await waiter_started.wait()
        await asyncio.sleep(0)

        release_holder.set()
        asyncio.get_running_loop().call_soon(waiter_task.cancel)
        await holder_task
        with self.assertRaises(asyncio.CancelledError):
            await waiter_task

        self.assertEqual(waiter_entries, 0)
        async with manager.hold("owner:cancel-boundary"):
            pass
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_repeated_cancellation_cannot_abandon_shielded_cleanup(self):
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()

        class ControlledCleanupManager(ConversationLockManager):
            async def _remove_reference(self, conversation_key, entry):
                cleanup_started.set()
                try:
                    await allow_cleanup.wait()
                    await super()._remove_reference(conversation_key, entry)
                finally:
                    cleanup_finished.set()

        manager = ControlledCleanupManager()

        async def worker():
            async with manager.hold("owner:repeated-cancel"):
                pass

        task = asyncio.create_task(worker())
        await cleanup_started.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        allow_cleanup.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(cleanup_finished.is_set())
        self.assertEqual(manager.registry_size_for_test, 0)

        async with manager.hold("owner:repeated-cancel"):
            pass
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_timed_out_waiters_do_not_block_later_live_waiter(self):
        manager = ConversationLockManager(wait_timeout_seconds=0.02)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder():
            async with manager.hold("owner:mixed-waiters"):
                holder_entered.set()
                await release_holder.wait()

        async def short_waiter():
            with self.assertRaises(ConversationBusyError):
                async with manager.hold("owner:mixed-waiters"):
                    self.fail("short waiter must not enter")

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        short_waiters = [
            asyncio.create_task(short_waiter())
            for _ in range(8)
        ]
        await asyncio.wait_for(
            asyncio.gather(*short_waiters),
            timeout=1,
        )

        manager.wait_timeout_seconds = 1.0
        live_entered = asyncio.Event()

        async def live_waiter():
            async with manager.hold("owner:mixed-waiters"):
                live_entered.set()

        live_task = asyncio.create_task(live_waiter())
        await asyncio.sleep(0)
        release_holder.set()
        await asyncio.wait_for(
            asyncio.gather(holder_task, live_task),
            timeout=1,
        )
        self.assertTrue(live_entered.is_set())
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_timeout_storm_does_not_leak_or_poison_key(self):
        manager = ConversationLockManager(wait_timeout_seconds=0.02)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        entered_by_timed_out_waiter = 0

        async def holder():
            async with manager.hold("owner:timeout-storm"):
                holder_entered.set()
                await release_holder.wait()

        async def waiter():
            nonlocal entered_by_timed_out_waiter
            try:
                async with manager.hold("owner:timeout-storm"):
                    entered_by_timed_out_waiter += 1
            except ConversationBusyError:
                return "busy"
            return "acquired"

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        results = await asyncio.wait_for(
            asyncio.gather(*(waiter() for _ in range(100))),
            timeout=1,
        )
        self.assertEqual(results, ["busy"] * 100)
        self.assertEqual(entered_by_timed_out_waiter, 0)

        release_holder.set()
        await holder_task
        async with manager.hold("owner:timeout-storm"):
            pass
        self.assertEqual(manager.registry_size_for_test, 0)

    async def test_nested_different_keys_are_allowed_and_same_key_still_rejected(self):
        manager = ConversationLockManager()
        async with manager.hold("owner:nested-a"):
            async with manager.hold("owner:nested-b"):
                self.assertEqual(manager.registry_size_for_test, 2)
            with self.assertRaises(ConversationLockReentryError):
                async with manager.hold("owner:nested-a"):
                    self.fail("same-key nesting must not enter")
        self.assertEqual(manager.registry_size_for_test, 0)

    def test_exception_string_and_repr_do_not_expose_conversation_key(self):
        supplied_key = "owner-secret:session-secret"
        cases = (
            (ConversationBusyError(), "CONVERSATION_BUSY"),
            (ConversationLockReentryError(), "CONVERSATION_LOCK_REENTRY"),
        )
        for error, safe_code in cases:
            with self.subTest(error=type(error).__name__):
                rendered = f"{str(error)} {repr(error)}"
                self.assertEqual(str(error), safe_code)
                self.assertIn(safe_code, repr(error))
                self.assertNotIn(supplied_key, rendered)


if __name__ == "__main__":
    unittest.main()
