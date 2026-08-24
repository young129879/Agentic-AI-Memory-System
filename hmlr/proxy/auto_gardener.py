"""
Automatic gardener: periodically archive idle bridge blocks.

Without this the system depends on `python hmlr/run_gardener.py`, a manual
step nobody will remember. Blocks pile up as ACTIVE/PAUSED forever, and
Dossier never learns from facts they hold.

This makes it a background loop: every interval_hours, find blocks whose
updated_at predates inactive_days and hand them to the gardener one at a
time.

The decision uses timestamps, not the LLM -- judging "has this gone quiet" is
a deterministic comparison and must not cost a model call per block. The LLM
work the gardener already does (classify facts, build dossiers) is left
unharmed.

A failed block is retried on the next sweep; the loop never crashes. Logging
replaces the `print()` calls that ManualGardener was written with, so this
can run unattended.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Sentinel so the scheduler can tell "never updated" apart from "just written".
_EPOCH = "2000-01-01 00:00:00"
_MIN_TIMESTAMP = "2000-01-01 00:00:00"


def stale_blocks_sql(
    inactive_days: int,
    statuses: tuple = ("ACTIVE", "PAUSED"),
) -> str:
    cutoff = (datetime.utcnow() - timedelta(days=inactive_days)).strftime("%Y-%m-%d %H:%M:%S")
    status_list = ", ".join(f"'{s}'" for s in statuses)
    # COALESCE guards against NULL updated_at on freshly-created blocks.
    return (
        "SELECT block_id, topic_label, status, updated_at "
        "FROM daily_ledger "
        f"WHERE status IN ({status_list}) "
        "AND COALESCE(updated_at, created_at) < ? "
        "ORDER BY updated_at ASC"
    ), (cutoff,)


class AutoGardener:
    """
    Periodic sweep that archives idle bridge blocks.
    """

    def __init__(self, storage, gardener, *,
                 interval_hours: float = 24.0,
                 inactive_days: int = 30,
                 enabled: bool = True):
        self.storage = storage
        self.gardener = gardener
        self.interval_hours = interval_hours
        self.inactive_days = inactive_days
        self.enabled = enabled
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.last_sweep_result: Dict[str, Any] = {}
        self.sweep_count = 0

    def _fetch_stale_blocks(self) -> list:
        sql, params = stale_blocks_sql(self.inactive_days)
        try:
            return self.storage.conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.error(f"AutoGardener could not query stale blocks: {e}")
            return []

    async def _process_block(self, block_id: str) -> Dict[str, Any]:
        try:
            return await self.gardener.process_bridge_block(block_id)
        except Exception as e:
            logger.error(f"AutoGardener failed on block {block_id}: {e}")
            return {"status": "error", "block_id": block_id, "message": str(e)}

    async def _sweep_once(self) -> None:
        blocks = self._fetch_stale_blocks()
        if not blocks:
            self.last_sweep_result = {"checked": 0, "processed": 0}
            return

        processed, failed = 0, 0
        for row in blocks:
            block_id = row[0]
            # A failed block is retried next sweep, so no permanent loss.
            result = await self._process_block(block_id)
            if result.get("status") == "ok" or result.get("status") == "success":
                processed += 1
            else:
                failed += 1

        self.last_sweep_result = {
            "checked": len(blocks),
            "processed": processed,
            "failed": failed,
        }
        self.sweep_count += 1
        logger.info(
            f"AutoGardener sweep #{self.sweep_count}: "
            f"{processed} archived, {failed} failed, {len(blocks)} checked"
        )

    async def run(self) -> None:
        """Main loop; returns only when stop() sets the event."""
        logger.info(
            f"AutoGardener started: every {self.interval_hours}h, "
            f"archive blocks idle > {self.inactive_days}d"
        )
        while not self._stop.is_set():
            try:
                await self._sweep_once()
            except Exception as e:
                logger.error(f"AutoGardener sweep crashed (will retry): {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_hours * 3600)
            except asyncio.TimeoutError:
                pass

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if not self.enabled or self._task is not None:
            return
        loop = loop or asyncio.get_event_loop()
        self._task = loop.create_task(self.run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
