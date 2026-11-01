# 第三方组件与许可范围

本项目原创代码以 Apache-2.0 提供（无附加条款）。该声明不改变第三方组件原有的版权、许可证和署名要求。

- DeepSeek Harness 及其插件由使用者独立安装；本包不捆绑其 node_modules。版本与许可证以所安装组件自带的 package.json、LICENSE 和上游仓库为准：https://github.com/deepseek-ai/deepseek-harness 。
- Electron/Chromium、@resvg/resvg-js、saxes、pngjs 等依赖亦不捆绑在此源码候选中，分发这些组件时须同时保留它们原有的声明。
- 原运行目录的 runtime-auto-review.mjs 是 DeepSeek 自动审查组件的本机适配副本，不属于本项目可重新指定许可的原创代码。本候选不包含该副本。它所依据的 @deepseek-ai/dsh-experimental-auto-review 0.2.1-rc.2 包声明为 MIT；原有版权及 MIT 条款继续适用。
- 私人人格、账号密钥、历史记录、第三方素材和虚拟机镜像不因项目源码采用开源许可证而自动纳入发行包。

LICENSE 正文取自 Apache Software Foundation：
https://www.apache.org/licenses/LICENSE-2.0.txt

许可证正文保持原样，仅末尾版权行按规范填本项目作者署名（`Copyright 2026 唐怀瑟`），
未包含任何第三方项目的版权声明或摘录；版权与“无附加条款”的说明见 NOTICE。

项目不对使用者的**代码使用**附加任何额外条件，也不要求使用者向原作者申请授权（理由见 LICENSE-SCOPE.md
与 许可决定.md）。`TRADEMARK.md` 与 `使用条款.md` 是非许可性质的说明：前者依 Apache-2.0 第 6 条
保留名称与形象，后者说明用途与免责；二者都不削减 LICENSE 授予的权利，冲突时以 LICENSE 为准。
