---
name: loongarch-build-openclaw
description: OpenClaw Skill 经验库（只读），存储 LoongArch 编译经验标准，禁止AI改写
category: software-development
read_only: true
---

# OpenClaw Skill 经验库（只读）

## ⚠️ 权限约束

**此技能目录为只读，禁止 AI 自动修改、生成或覆盖任何文件。**

所有编译经验必须通过平台提交 → 人工审核 → 入库流程，禁止 AI 自动生成编译脚本。

## Skill 目录结构标准

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

## SKILL.md 标准格式

```markdown
# Skill 名称

## 适用系统
- openEuler 24.03 LTS (loongarch64)
- 统信 UOS V20 专业版

## 问题描述
编译 xxx 时遇到的问题描述

## 依赖列表
| 依赖 | 版本要求 | 安装方式 |
|------|----------|----------|
| go | ≥ 1.22.0 | dnf install golang-go |

## 编译步骤
```bash
# 步骤 1
git clone ...
# 步骤 2
export CROSS_COMPILE=...
# 步骤 3
make -j$(nproc)
```

## 注意事项
- 注意事项 1
- 注意事项 2

## 错误排查
- 问题现象 → 解决方案

## 版本信息
- 创建者: xxx
- 创建时间: YYYY-MM-DD
- 适用架构: loongarch64
- 来源任务: #xxxx
```

## Skill 生命周期

```
新建 → 提交 → 管理员审核 → 入库 → 同步客户端 → 迭代更新
                         → 驳回 → 填写原因 → 通知提交人
```

## 现有 Skill 列表（截至 2026-09-21）

| Skill ID | 名称 | 类型 | 适用系统 | 版本 |
|----------|------|------|----------|------|
| S-001 | gen-pip-loongarch | 通用 | openEuler2403 | v2.1 |
| S-002 | gen-go-toolchain-1.22 | 通用 | 全平台 | v1.4 |
| S-015 | ollama-oe2403-build | 专用 | openEuler2403 | v0.9.7 |
| S-022 | nginx-uos-v20-build | 专用 | 统信UOS V20 | v1.25 |

## 操作规范

### ✅ 允许的操作
- 读取 Skill 内容用于编译匹配
- 展示 Skill 详情给用户
- 统计 Skill 使用情况

### ❌ 禁止的操作
- 自动生成新的 SKILL.md 文件
- 修改现有 Skill 内容
- 覆盖已有编译脚本
- 绕过审核流程直接入库

## 相关文件
- 设计文档第六章：OpenClaw Skill 经验体系
- UI 原型：经验库管理页面、Skill 经验审核页面
