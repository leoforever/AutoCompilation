# Skill 经验库

本目录存放龙芯 LoongArch 编译平台的 Skill 经验文件（SKILL.md 标准格式）。**这是 agent 编译时唯一读取的来源**——平台 stage5 扫磁盘发现 SKILL.md，DB 里存的是备份/元数据。

## 目录结构

```
skills/
├── generic/                   # 通用 Skill（工具链 / 环境 / 错误经验，跨软件）
│   ├── go-toolchain/          #   Go 工具链下载与编译
│   ├── nodejs-toolchain/      #   Node.js 工具链
│   ├── python-environment/    #   Python 环境/依赖
│   ├── python-package/        #   Python 包通用编译
│   └── build-notes-standard/  #   BUILD_NOTES 现场记录格式标准（agent 强制读）
└── specific/                  # 专用 Skill（特定软件 × 特定系统攻略）
    └── <软件-系统>-build/         #   如 pytorch-2160a0-loongnix-server-232-loongarch64-build
```

每个 Skill 一个目录：`SKILL.md`（必需）+ 可选 `scripts/run.sh`。

## 治理规则（重要）

**新增/修改/删除只走审批流，禁止 AI 或人直接改本目录后绕过：**

```
工单 resolve 勾自动生成 / 用户沉淀经验 → LLM 草稿（1~4 分钟）
→ 审批页预览可编辑 → 通过 → 写 skills 表 + 落盘本目录 + 同名旧版标 superseded
```

- 平台管理员也可在「经验库管理」页直接 CRUD（同走 DB+磁盘双写）
- 删除=进回收站（DB deleted_at 软删 + 磁盘目录移除），可恢复或永久删除
- 编辑保存注意：**version 列最长 20 字符**（超长报可读校验错误）

## 生效机制

- 每次 stage5 启动**重扫磁盘**（`get_skills_paths`）——审批入库的新攻略无需重启后端即可被下一个编译任务读到
- agent 按 system_prompt 指引先读攻略全文再执行；**实际读过哪些 SKILL.md 会被收口**记入任务 `matched_skills` 列并实时 WS 推送（前端"本次命中 N 个"）
- 磁盘与 DB 不一致时以磁盘为准（页面进入时自动 reconcile）

## git 跟踪说明

本目录**在 git 内**：新环境 `git clone` 即自带全部攻略（含 BUILD_NOTES 标准与实测回写版）。运行数据（artifacts/logs/cache）不入 git。若外部手工增删 SKILL.md（不走 API），平台 reconcile 会感知并同步 DB。
