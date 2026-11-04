"""Shared isolated Engine construction and bounded execution for personal Jobs."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace


def make_execution_child(parent, job, *, team_execution=None, peer_message_id=""):
    from naumi_agent.orchestrator.engine import AgentEngine
    from naumi_agent.runtime.composition import build_runtime_paths

    config = parent.config.model_copy(deep=True)
    config.memory.long_term_enabled = False
    config.safety.permission_mode = job.request.permission_mode
    config.safety.max_turns = job.request.max_turns
    config.safety.max_budget_usd = (
        None if job.request.max_cost_microusd is None else job.request.max_cost_microusd / 1_000_000
    )
    if team_execution:
        config.safety.max_input_tokens = team_execution["max_input_tokens"]
        config.safety.max_output_tokens = team_execution["max_output_tokens"]
    paths = build_runtime_paths(config)
    execution_key = hashlib.sha256(job.job_id.encode()).hexdigest()[:32]
    directory = paths.runtime_data_dir / "personal-executions" / execution_key
    if peer_message_id:
        directory = directory / "p" / peer_message_id.removeprefix("pm_")
    changes = {}
    for name in paths.__dataclass_fields__:
        if name not in {
            "workspace_root",
            "session_db_path",
            "runtime_data_dir",
            "chat_run_db_path",
        }:
            key = hashlib.sha256(name.encode()).hexdigest()[:12]
            changes[name] = directory / key / getattr(paths, name).name
    return AgentEngine(
        config,
        paths=replace(paths, **changes),
        isolated_child=True,
        execution_data_dir=directory,
    )


async def bounded_execution(operation, *, timeout_seconds):
    """The shared cancellation/timeout boundary; never admit or create another Job."""
    return await asyncio.wait_for(operation, timeout=timeout_seconds)


async def renew_execution_lease(jobs, job, owner_id, execution, authorize, *, interval=20):
    try:
        while True:
            await asyncio.sleep(interval)
            await jobs.renew_claim(
                job.job_id,
                owner_id=owner_id,
                claim_epoch=job.claim_epoch,
                lease_seconds=90,
            )
            await authorize()
    except asyncio.CancelledError:
        raise
    except BaseException:
        if not execution.done():
            execution.cancel()
        raise
