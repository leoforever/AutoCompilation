# 龙芯 LoongArch 智能编译平台 · API 接口文档

> 基于设计文档第十一节 API 接口设计，版本 v1.0

## 基础信息

- Base URL: `http://localhost:8000`
- 认证方式: Bearer Token (JWT)
- 数据格式: JSON

---

## 一、用户前台 API

### 1.1 提交编译任务

**POST** `/api/compile/submit`

**请求体**:
```json
{
  "system_id": 1,
  "software_name": "ollama v0.9.7",
  "source_type": "upload",
  "source_path": "ollama-v0.9.7.tar.gz",
  "compile_mode": "container",
  "build_args": {
    "CROSS_COMPILE": "loongarch64-unknown-linux-gnu",
    "CGO_ENABLED": "1"
  }
}
```

**响应**:
```json
{
  "code": 200,
  "message": "编译任务已提交",
  "data": {
    "task_id": "T-20260922103045-A1B2C3",
    "status": "submitted"
  }
}
```

---

### 1.2 获取任务列表

**GET** `/api/compile/tasks`

**Query Parameters**:
- `status`: 状态筛选（submitted/compiling/success/failed）
- `search`: 搜索软件名或任务 ID
- `page`: 页码（默认 1）
- `page_size`: 每页数量（默认 20）

**响应**:
```json
{
  "code": 200,
  "data": {
    "total": 100,
    "tasks": [
      {
        "id": 1,
        "task_id": "T-20260922103045-A1B2C3",
        "user_id": 1,
        "system_id": 1,
        "software_name": "ollama v0.9.7",
        "source_type": "upload",
        "compile_mode": "container",
        "status": "success",
        "artifact_path": "ollama-0.9.7-loongarch64.rpm",
        "created_at": "2026-09-22T10:30:45",
        "completed_at": "2026-09-22T10:35:17",
        "duration_seconds": 272
      }
    ]
  }
}
```

---

### 1.3 获取任务详情

**GET** `/api/compile/tasks/{task_id}`

**响应**: 同任务列表中的单条记录

---

### 1.4 获取编译日志

**GET** `/api/compile/tasks/{task_id}/log`

**响应**:
```json
{
  "code": 200,
  "data": {
    "task_id": "T-20260922103045-A1B2C3",
    "log": "[INFO] 初始化编译环境...\n[OK] 步骤 1/5 完成..."
  }
}
```

---

### 1.5 WebSocket 实时日志

**WS** `/api/ws/compile/log/{task_id}`

客户端连接后，服务端主动推送日志行（纯文本）。

---

### 1.6 上报问题工单

**POST** `/api/tickets/report`

**请求体**:
```json
{
  "task_id": 1,
  "source": "cloud",
  "title": "nginx v1.25 编译失败",
  "description": "configure: error: the HTTP rewrite module requires the PCRE library"
}
```

**响应**:
```json
{
  "code": 200,
  "message": "工单已提交",
  "data": {
    "ticket_id": 1,
    "status": "pending"
  }
}
```

---

### 1.7 获取我的工单

**GET** `/api/tickets/my`

**Query Parameters**:
- `status`: 状态筛选

**响应**: 工单列表

---

### 1.8 提交编译经验

**POST** `/api/experience/submit`

**请求体**:
```json
{
  "task_id": 1,
  "skill_name": "ollama v0.9.7",
  "skill_type": "specific",
  "system_id": 1,
  "skill_content": "# Skill 内容..."
}
```

---

### 1.9 获取我的经验

**GET** `/api/experience/my`

---

### 1.10 获取可用 Skill 列表

**GET** `/api/skills`

**Query Parameters**:
- `type`: 类型筛选（generic/specific）
- `system_id`: 系统 ID 筛选

---

### 1.11 获取目标系统列表

**GET** `/api/systems`

**响应**:
```json
{
  "code": 200,
  "data": [
    {
      "system_id": 1,
      "name": "openEuler 24.03 LTS",
      "arch": "loongarch64",
      "base_image": "loongarch64/oe2403:latest",
      "is_active": true
    }
  ]
}
```

---

## 二、管理员后台 API

### 2.1 统计看板

**GET** `/api/admin/stats/overview`

**响应**:
```json
{
  "code": 200,
  "data": {
    "success_rate": 87.3,
    "total_tasks": 1247,
    "total_experiences": 342,
    "active_users": 89,
    "recent_success_rate_trend": [...],
    "top_errors": [...],
    "cluster_nodes": [...],
    "skill_sync_summary": {...}
  }
}
```

---

### 2.2 工单列表

**GET** `/api/admin/tickets`

**Query Parameters**:
- `status`: 状态筛选
- `search`: 搜索工单号或软件名

---

### 2.3 发送工单消息

**POST** `/api/admin/tickets/{ticket_id}/messages`

**请求体**:
```json
{
  "content": "请确认 pcre-devel 是否安装"
}
```

---

### 2.4 标记工单解决

**POST** `/api/admin/tickets/{ticket_id}/resolve`

---

### 2.5 待审核经验列表

**GET** `/api/admin/experience/pending`

---

### 2.6 通过经验审核

**POST** `/api/admin/experience/{experience_id}/approve`

---

### 2.7 驳回经验审核

**POST** `/api/admin/experience/{experience_id}/reject`

**请求体**:
```json
{
  "reason": "依赖列表不完整"
}
```

---

### 2.8 Skill CRUD

**GET** `/api/admin/skills` - 列表
**POST** `/api/admin/skills` - 新增
**PUT** `/api/admin/skills/{id}` - 更新
**DELETE** `/api/admin/skills/{id}` - 删除

---

### 2.9 用户列表

**GET** `/api/admin/users`

---

### 2.10 目标系统 CRUD

**GET** `/api/admin/systems` - 列表
**POST** `/api/admin/systems` - 新增
**PUT** `/api/admin/systems/{id}` - 更新
**DELETE** `/api/admin/systems/{id}` - 删除

---

## 三、统一响应格式

### 成功响应
```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

### 错误响应
```json
{
  "code": 400,
  "message": "参数错误",
  "detail": "system_id 不能为空"
}
```

---

## 四、状态枚举

### 编译任务状态
- `submitted`: 已提交
- `compiling`: 编译中
- `success`: 成功
- `failed`: 失败

### 工单状态
- `pending`: 待处理
- `processing`: 处理中
- `resolved`: 已解决
- `closed`: 已关闭

### Skill 审核状态
- `submitted`: 已提交
- `under_review`: 审核中
- `approved`: 已通过
- `rejected`: 已驳回
- `synced`: 已同步
