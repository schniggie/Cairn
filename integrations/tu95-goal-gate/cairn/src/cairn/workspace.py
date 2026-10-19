from __future__ import annotations

import os
from pathlib import Path

"""运行时产物的统一落盘位置。

默认放在当前工作目录下的 ./workspace（随仓库 .gitignore 忽略），
可用环境变量 CAIRN_WORKSPACE 覆盖到别处。
把 DB、prompt 快照、worker 运行目录等 "工作进行中产生的内容" 集中到一处，
避免散落在 ~/.local/share 和 /tmp 等多个地方。
"""

ENV_VAR = "CAIRN_WORKSPACE"
DEFAULT_DIRNAME = "workspace"


def workspace_root() -> Path:
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.cwd() / DEFAULT_DIRNAME


def workspace_path(*parts: str) -> Path:
    return workspace_root().joinpath(*parts)
