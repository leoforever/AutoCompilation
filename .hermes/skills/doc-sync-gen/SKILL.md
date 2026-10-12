---
name: doc-sync-gen
description: 开发完成后同步更新接口文档和开发笔记
category: software-development
---

# 文档同步生成规范

## 触发时机

每次完成以下任务后，必须更新相关文档：
- 新增/修改 API 接口
- 新增/修改数据库表
- 新增页面组件
- 修复重要 bug

## 文档更新范围

### 1. OpenAPI 接口文档
**路径**：`docs/api.md`

**格式**：
```markdown
## POST /api/compile/submit

**描述**：提交编译任务

**请求体**：
```json
{
  "system_id": 1,
  "software_name": "ollama v0.9.7",
  "source_type": "upload",
  "source_path": "ollama-v0.9.7.tar.gz",
  "compile_mode": "container"
}
```

**响应**：
```json
{
  "code": 200,
  "data": {
    "task_id": "T-XXXXX",
    "status": "submitted"
  }
}
```
```

### 2. 数据库 DDL 文档
**路径**：`docs/sql/schema.sql`

每次数据库变更，输出完整 SQL 脚本（含注释），人工审核后执行。

### 3. 开发笔记
**路径**：`docs/changelog.md`

```markdown
## YYYY-MM-DD

### 新增
- 接口：POST /api/compile/submit
- 功能：编译任务提交

### 修改
- 修复：WebSocket 连接未关闭导致内存泄漏

### 数据库
- 新增表：compile_tasks
```

## 更新流程

1. 完成代码开发
2. 运行 `python3 scripts/gen_docs.py` 自动生成文档框架
3. 人工补充业务说明
4. 提交文档变更

## 禁止行为
- ❌ 跳过文档更新直接提交代码
- ❌ 文档与实际代码不一致
- ❌ 手动编写 SQL 而不输出到文件
