"""``python -m naumi_agent`` entry point.

When ``--tui`` / ``-t`` is detected, silences fd 1/2 before importing so
that libraries in the import chain (Rich, ChromaDB, …) cannot emit ANSI
terminal queries that corrupt Textual's DSR handling.
"""

import os
import sys


def _tui_main() -> None:
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    original_argv = sys.argv[:]
    sys.argv = [a for a in sys.argv if a not in ("--tui", "-t")]

    from naumi_agent.main import _launch_tui  # noqa: E402

    sys.argv = original_argv
    os.dup2(saved_out, 1)
    os.dup2(saved_err, 2)
    os.close(saved_out)
    os.close(saved_err)
    sys.stdout = open("/dev/tty", "w")
    sys.stderr = open("/dev/tty", "w")

    config_path = "config.yaml"
    for i, a in enumerate(sys.argv):
        if a in ("--config", "-c") and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]

    _launch_tui(config_path)


def main() -> None:
    if "--tui" in sys.argv or "-t" in sys.argv:
        _tui_main()
    else:
        from naumi_agent.main import cli

        cli()


if __name__ == "__main__":
    main()
