# OpenClaw Skill 经验库

本目录存放龙芯 LoongArch 编译平台的 OpenClaw 标准 Skill 文件。

## 目录结构

```
skills/
├── gen-pip-loongarch/
│   ├── SKILL.md
│   └── scripts/
│       └── run.sh
├── gen-go-toolchain-1.22/
│   ├── SKILL.md
│   └── scripts/
│       └── run.sh
└── ollama-oe2403-build/
    ├── SKILL.md
    └── scripts/
        └── run.sh
```

## ⚠️ 重要说明

**本目录为只读经验库，禁止 AI 自动修改或生成文件。**

所有新增编译经验必须通过平台提交 → 人工审核 → 入库流程。

## Skill 格式规范

参考设计文档第六章：OpenClaw Skill 经验体系

每个 Skill 包含：
- `SKILL.md`：经验说明文档
- `scripts/run.sh`：执行脚本（可选）

## 现有 Skill 列表

| Skill ID | 名称 | 类型 | 适用系统 | 版本 |
|----------|------|------|----------|------|
| S-001 | gen-pip-loongarch | 通用 | openEuler2403 | v2.1 |
| S-002 | gen-go-toolchain-1.22 | 通用 | 全平台 | v1.4 |
| S-015 | ollama-oe2403-build | 专用 | openEuler2403 | v0.9.7 |
| S-022 | nginx-uos-v20-build | 专用 | 统信UOS V20 | v1.25 |
