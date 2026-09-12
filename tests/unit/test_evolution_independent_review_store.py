from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest

from naumi_agent.evolution.independent_reviews import EvolutionIndependentReviewStore


@pytest.mark.asyncio
async def test_concurrent_first_claims_share_one_schema_initialization(tmp_path) -> None:
    store = EvolutionIndependentReviewStore(tmp_path / "review.db")

    claims = await asyncio.gather(*(
        store.acquire(
            gate_id="evgate_" + "a" * 24,
            owner_token=f"{index:032x}",
            now=datetime(2026, 9, 12, tzinfo=UTC),
            lease_seconds=30,
        )
        for index in range(1, 9)
    ))

    assert sum(claim.acquired for claim in claims) == 1
    with sqlite3.connect(tmp_path / "review.db") as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.asyncio
async def test_concurrent_store_instances_wait_for_wal_schema_lock(tmp_path) -> None:
    stores = [
        EvolutionIndependentReviewStore(tmp_path / "review.db")
        for _ in range(8)
    ]

    claims = await asyncio.gather(*(
        store.acquire(
            gate_id="evgate_" + "b" * 24,
            owner_token=f"{index:032x}",
            now=datetime(2026, 9, 12, tzinfo=UTC),
            lease_seconds=30,
        )
        for index, store in enumerate(stores, start=1)
    ))

    assert sum(claim.acquired for claim in claims) == 1
