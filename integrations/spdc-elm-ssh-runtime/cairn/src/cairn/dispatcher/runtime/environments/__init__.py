from __future__ import annotations

from cairn.dispatcher.config import DockerEnvironmentConfig, EnvironmentConfig, SshEnvironmentConfig
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle, EnvironmentState, WorkEnvironment
from cairn.dispatcher.runtime.environments.docker import DockerEnvironment
from cairn.dispatcher.runtime.environments.ssh import SshEnvironment


def build_environment(config: EnvironmentConfig) -> WorkEnvironment:
    if isinstance(config, DockerEnvironmentConfig):
        return DockerEnvironment(config)
    if isinstance(config, SshEnvironmentConfig):
        return SshEnvironment(config)
    raise TypeError(f"unsupported environment config: {type(config)!r}")


__all__ = [
    "DockerEnvironment",
    "EnvironmentHandle",
    "EnvironmentState",
    "SshEnvironment",
    "WorkEnvironment",
    "build_environment",
]
