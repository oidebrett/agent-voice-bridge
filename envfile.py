"""Read /opt/agent-voice/.env (systemd EnvironmentFile format) for the CLI helpers."""

from __future__ import annotations

import os

ENV_PATH = "/opt/agent-voice/.env"


def load(path: str = ENV_PATH) -> None:
    for line in open(path).read().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
