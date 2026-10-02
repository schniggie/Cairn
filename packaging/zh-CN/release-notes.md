由 release workflow 自动构建的 Cairn 中文版发行包。

- `cairn-zh-CN-{{VERSION}}.zip` — 中文独立增量包：下载原版 Cairn 后解压到项目根，运行 `./install.sh` 即可切换中文。
- `Cairn-{{VERSION}}-zh.zip` — 完整中文版整包：原版 + 中文本地化，开箱即用。

切换方法：`dispatch.yaml` 中 `runtime.prompt_group: "zh-CN"`；Web 界面右上角 `中文` 按钮。
