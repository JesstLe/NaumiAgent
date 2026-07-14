from __future__ import annotations

from naumi_agent import packaging_entry


def test_packaging_entry_dispatches_internal_bridge_without_typer(monkeypatch) -> None:
    calls: list[list[str] | str] = []
    monkeypatch.setattr(
        "naumi_agent.ui.bridge.main",
        lambda argv=None: calls.append(list(argv or [])),
    )
    monkeypatch.setattr(
        "naumi_agent.main.cli",
        lambda: calls.append("cli"),
    )

    packaging_entry.main(["__ui-bridge", "--config", "project.yaml"])

    assert calls == [["--config", "project.yaml"]]


def test_packaging_entry_uses_public_cli_for_normal_arguments(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("naumi_agent.main.cli", lambda: calls.append("cli"))
    monkeypatch.setattr(packaging_entry.sys, "argv", ["naumi", "doctor"])

    packaging_entry.main()

    assert calls == ["cli"]
