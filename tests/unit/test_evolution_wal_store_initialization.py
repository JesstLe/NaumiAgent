from __future__ import annotations

import asyncio
import sqlite3

import pytest

from naumi_agent.evolution.decision_inputs import EvolutionDecisionInputStore
from naumi_agent.evolution.independent_reviews import EvolutionIndependentReviewStore
from naumi_agent.evolution.mechanical_gates import EvolutionMechanicalGateStore


@pytest.mark.asyncio
async def test_evolution_stores_concurrently_initialize_one_wal_database(tmp_path) -> None:
    db_path = tmp_path / "evolution.db"
    stores = [
        *[EvolutionDecisionInputStore(db_path) for _ in range(4)],
        *[EvolutionMechanicalGateStore(db_path) for _ in range(4)],
        *[EvolutionIndependentReviewStore(db_path) for _ in range(4)],
    ]

    await asyncio.gather(*(store._ensure_schema() for store in stores))

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "evolution_decision_inputs",
        "evolution_mechanical_gates",
        "evolution_independent_reviews",
        "evolution_independent_review_claims",
    } <= tables


@pytest.mark.asyncio
async def test_schema_guard_reinitializes_replaced_database_file(tmp_path) -> None:
    db_path = tmp_path / "evolution.db"
    store = EvolutionDecisionInputStore(db_path)
    await store._ensure_schema()

    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    db_path.unlink()
    await store._ensure_schema()

    with sqlite3.connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "evolution_decision_inputs" in tables
