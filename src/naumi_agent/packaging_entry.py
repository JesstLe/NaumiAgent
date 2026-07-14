"""Single frozen executable entrypoint for the public CLI and internal UI Bridge."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["__ui-bridge"]:
        from naumi_agent.ui.bridge import main as bridge_main

        bridge_main(args[1:])
        return

    from naumi_agent.main import cli

    cli()


if __name__ == "__main__":
    main()
