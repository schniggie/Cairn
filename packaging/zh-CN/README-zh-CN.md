# Cairn 中文语言包 (cairn-zh-CN)

Cairn 的简体中文本地化增量包。适用于 **Cairn {{VERSION}}**（对应仓库 [oritera/Cairn](https://github.com/oritera/Cairn)）。

> 这是独立的中文增量包：先下载原版 Cairn，再把本包解压进去即可在英文/中文之间切换。
> 不需要改动任何 Python 代码，原版与中文版共用同一套二进制/源码。

## 包含内容

```
files/
└── cairn/src/cairn/
    ├── dispatcher/prompts/zh-CN/     # 中文 agent 提示词（5 个）
    │   ├── bootstrap.md
    │   ├── bootstrap_conclude.md
    │   ├── explore.md
    │   ├── explore_conclude.md
    │   └── reason.md
    └── server/static/index.html      # 中英双语 Web 界面（带 EN/中文 切换按钮）
```

- **提示词层**：原版英文指令模板 + 新增 `## Language` 规则，要求 agent 产出的
  `fact.description`、`intent.description`、reason 文本一律使用简体中文；
  JSON 键名 / 枚举值 / id / 技术标识符保持不变。
- **界面层**：整站 UI 文案中英双语，右上角 `EN / 中文` 一键切换，选择保存在浏览器
  localStorage，并按浏览器语言自动默认。

## 安装

```bash
# 方式一：解压到项目根目录后运行安装脚本（推荐，脚本会自动定位项目根并覆盖文件）
unzip cairn-zh-CN-{{VERSION}}.zip -d /path/to/Cairn
cd /path/to/Cairn && ./install.sh
#   或从包目录显式指定：  ./install.sh /path/to/Cairn

# 方式二：手动放置（路径与仓库一致）
#   files/cairn/src/cairn/dispatcher/prompts/zh-CN/*.md -> Cairn/cairn/src/cairn/dispatcher/prompts/zh-CN/
#   files/cairn/src/cairn/server/static/index.html      -> Cairn/cairn/src/cairn/server/static/index.html
```

安装不修改任何 Python 代码，纯文件覆盖，随时可用 `git checkout` 还原。

## 切换（英文 ↔ 中文）

| 层面 | 英文（原版） | 中文（本包） |
|------|--------------|--------------|
| **agent 提示词** | `runtime.prompt_group: "default"` | `runtime.prompt_group: "zh-CN"`（在 `dispatch.yaml` 中设置） |
| **Web 界面** | 右上角 `EN` | 右上角 `中文`（无需改配置） |

### 提示词切换步骤

1. 在 `dispatch.yaml` 的 `runtime:` 下设置 `prompt_group: "zh-CN"`：

   ```yaml
   runtime:
     prompt_group: "zh-CN"
   ```

2. 重启 server 与 dispatcher：

   ```bash
   ./cairn-local.sh restart   # 或自行重启 serve / dispatch
   ```

3. dispatcher 启动时会校验提示词资源（`validate_prompt_resources`），
   校验通过即生效；想切回英文只需把 `prompt_group` 改回 `"default"` 并重启。

### 界面切换

打开网页后点击右上角 `中文` 按钮即可，切换即时生效并记住选择。

## 验证

安装后可确认中文提示词组已就绪：

```bash
python3 - <<'PY'
from importlib import resources
from cairn.dispatcher.config import validate_prompt_resources
validate_prompt_resources("zh-CN")
print("zh-CN prompt group OK")
PY
```

## 说明

- 本包**不包含** `dispatch.yaml`（含本机路径/密钥，属本地配置）。请参考仓库内的
  `dispatch.local.example.yaml` 创建，并设置 `prompt_group: "zh-CN"`。
- 界面 `index.html` 自带中英双语，安装后原英文用户不受影响（默认按浏览器语言）。
- 与版本 {{VERSION}} 构建产物一致；如需完整版（含中文的原版整包），见同 Release 的
  `Cairn-{{VERSION}}-zh.zip`。
