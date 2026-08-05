"""Minimal frozen entrypoint for the stable release-slot launcher."""

from naumi_agent.release.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
