"""工作项 D：Kali Worker 工具技能库的静态校验。

覆盖对象（均为仓库内文件，不需要 Docker 环境）：
- container/.agents/skills/*/SKILL.md：frontmatter 合法（name/description 存在且与目录名一致）、示例命令非空
- container/AGENTS.md：工具技能索引中的相对路径都存在，且磁盘上的技能全部进入索引
- container/Dockerfile：包含到 /home/kali/.claude/skills 的 home 级复制（Worker cwd 是 bind mount，home 级副本是运行时主路径）
- explore.md / explore_resume.md：含指向 /home/kali/.claude/skills 的技能指引行
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTAINER_DIR = REPO_ROOT / "container"
SKILLS_DIR = CONTAINER_DIR / ".agents" / "skills"
AGENTS_MD = CONTAINER_DIR / "AGENTS.md"
DOCKERFILE = CONTAINER_DIR / "Dockerfile"
PROMPTS_DIR = REPO_ROOT / "cairn" / "src" / "cairn" / "dispatcher" / "prompts" / "default"

SKILLS_HOME_PATH = "/home/kali/.claude/skills"


def _skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _parse_frontmatter(text: str, path: Path) -> dict[str, str]:
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", f"{path}: frontmatter 必须以 --- 开头"
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        raise AssertionError(f"{path}: frontmatter 缺少结束的 ---")
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, f"{path}: frontmatter 行无法解析: {line!r}"
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def test_skill_files_exist() -> None:
    skills = _skill_files()
    assert len(skills) >= 10, f"技能数量不足（{len(skills)} 个），工作项 D 要求约 10~14 个"


def test_skill_frontmatter_valid() -> None:
    for path in _skill_files():
        fm = _parse_frontmatter(path.read_text(encoding="utf-8"), path)
        assert fm.get("name"), f"{path}: frontmatter 缺少 name"
        assert fm.get("description"), f"{path}: frontmatter 缺少 description"
        assert fm["name"] == path.parent.name, (
            f"{path}: name={fm['name']!r} 与目录名 {path.parent.name!r} 不一致"
        )


def test_skill_example_commands_non_empty() -> None:
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
        commands = [
            line.strip()
            for block in blocks
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert commands, f"{path}: 没有非空的示例命令"


def test_agents_index_paths_exist() -> None:
    text = AGENTS_MD.read_text(encoding="utf-8")
    referenced = re.findall(r"\.agents/skills/([\w-]+)/SKILL\.md", text)
    assert referenced, "AGENTS.md 工具技能索引中没有任何 .agents/skills/ 路径"
    for name in referenced:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), (
            f"AGENTS.md 索引引用了不存在的技能: {name}"
        )
    on_disk = {path.parent.name for path in _skill_files()}
    missing = on_disk - set(referenced)
    assert not missing, f"以下技能未进入 AGENTS.md 索引: {sorted(missing)}"


def test_dockerfile_copies_skills_to_claude_home() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    pattern = r"COPY\s+.*\.agents/skills\s+" + re.escape(SKILLS_HOME_PATH)
    assert re.search(pattern, text), (
        f"Dockerfile 缺少将 .agents/skills 复制到 {SKILLS_HOME_PATH} 的指令"
    )


def test_explore_prompts_reference_skills() -> None:
    for name in ("explore.md", "explore_resume.md"):
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert SKILLS_HOME_PATH in text, f"{name} 缺少指向 {SKILLS_HOME_PATH} 的技能指引行"
