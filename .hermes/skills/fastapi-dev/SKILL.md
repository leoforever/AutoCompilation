---
name: fastapi-dev
description: FastAPI 后端开发约束，pydantic v2、WebSocket、任务队列、工单对话
category: software-development
---

# FastAPI 开发规范（龙芯编译平台专用）

## 强制约束

### Pydantic v2 语法
- **必须使用** `model_config = ConfigDict(...)` 替代 `class Config`
- **禁止使用** Pydantic v1 语法（`class Config:`、`schema_extra` 等）
- 所有请求/响应模型必须定义在 `schemas.py`

```python
from pydantic import BaseModel, ConfigDict, Field

class CompileTaskCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    
    system_id: int
    software_name: str = Field(..., min_length=1, max_length=100)
    source_type: str = Field(..., pattern="^(upload|git)$")
    source_path: str | None = None
    git_url: str | None = None
    git_branch: str | None = None
    build_args: dict | None = None
    compile_mode: str = Field(default="container", pattern="^(container|local)$")
```

### 数据库 DDL 规范
- **禁止 Hermes 直接执行 DDL**
- 所有数据库变更必须输出 SQL 脚本，由人工审核执行
- 使用 SQLAlchemy 2.0 声明式语法

```python
from sqlalchemy import Column, Integer, String, DateTime, Enum
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class CompileTask(Base):
    __tablename__ = "compile_tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(50), unique=True, index=True)
    status = Column(Enum("submitted", "compiling", "success", "failed"), default="submitted")
    # ... 其他字段
```

**输出 SQL 示例**：
```sql
CREATE TABLE compile_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id VARCHAR(50) UNIQUE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'submitted',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### WebSocket 实时日志
编译任务状态通过 WebSocket 推送：
```python
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
active_connections: list[WebSocket] = []

@router.websocket("/ws/compile/log/{task_id}")
async def compile_log_websocket(websocket: WebSocket, task_id: str):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            # 保持连接，由服务端主动推送
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)
```

### 异常处理规范
所有接口必须加异常捕获，返回统一错误格式：
```python
from fastapi import HTTPException

@router.post("/api/compile/submit")
async def submit_compile(task: CompileTaskCreate, db: Session = Depends(get_db)):
    try:
        # 业务逻辑
        return {"task_id": task_id, "status": "submitted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Compile submit failed: {e}")
        raise HTTPException(status_code=500, detail="内部错误")
```

### 统一响应格式
```python
from pydantic import BaseModel

class ApiResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: dict | list | None = None

class ErrorResponse(BaseModel):
    code: int
    message: str
    detail: str | None = None
```

### 权限控制
- 用户前台接口：需要登录（`get_current_user` 依赖）
- 管理员接口：需要 admin 角色（`require_admin` 依赖）
- 敏感操作记录审计日志

```python
from fastapi import Depends
from app.dependencies import get_current_user, require_admin

@router.get("/api/stats/overview")
async def get_stats(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    # 仅管理员可访问
```

### 任务队列
使用 Redis 作为任务队列：
```python
import redis
import json

r = redis.Redis(host='localhost', port=6379, db=0)

def enqueue_compile_task(task_id: str, task_data: dict):
    r.lpush('compile_queue', json.dumps({
        'task_id': task_id,
        'data': task_data,
        'created_at': datetime.now().isoformat()
    }))

def dequeue_compile_task() -> dict | None:
    data = r.rpop('compile_queue')
    return json.loads(data) if data else None
```

### 自检清单
- [ ] 使用 Pydantic v2 语法
- [ ] 所有接口有异常捕获
- [ ] WebSocket 连接管理正确
- [ ] DDL 仅输出 SQL，不执行
- [ ] 权限控制正确
- [ ] 统一响应格式
