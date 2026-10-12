# 龙芯 LoongArch 智能编译平台 · 项目契约

> **唯一事实源**：本文件 + 根目录设计文档 `龙芯LoongArch智能编译平台设计文档.docx` + UI原型 `compile-platform-mockup.html`
> **禁止**：Hermes 不得自行新增业务逻辑、字段、状态值。所有需求以两份文档为准。

---

## 一、项目简介

面向龙芯 LoongArch 架构的软件编译场景，打造**云端公共编译服务 + 本地私有化客户端编译**双模式智能编译平台。

**核心闭环**：用户编译 → 问题反馈 → 人工优化 → 经验沉淀 → 全员复用

**双模式**：
- **云端公共编译**：开源软件、无保密需求，源码上传云端，容器隔离执行
- **本地私有编译**：涉密代码，编译全程离线，仅选择性上报脱敏日志

**Skill 经验体系**：OpenClaw 标准格式，通用 Skill（跨系统）+ 专用 Skill（特定软件×系统），审核入库后同步至所有客户端。

---

## 二、固定目录结构

```
compile-platform/
├── project-spec.md          # 本文件，每次任务必读
├── docs/                    # 接口文档、开发笔记
├── backend/                 # FastAPI 后端（禁止移到其他目录）
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          # FastAPI 入口
│   │   ├── config.py        # 配置加载
│   │   ├── models.py        # SQLAlchemy ORM 模型（DDL 仅生成，不自动执行）
│   │   ├── schemas.py       # Pydantic v2 请求/响应模型
│   │   ├── dependencies.py  # 依赖注入（auth、权限）
│   │   ├── routes/          # API 路由（按模块拆分）
│   │   │   ├── compile.py   # 编译任务接口
│   │   │   ├── tickets.py   # 工单接口
│   │   │   ├── experience.py # 经验/ Skill 接口
│   │   │   ├── admin.py     # 管理员接口
│   │   │   └── ws.py        # WebSocket 实时日志
│   │   └── services/        # 业务逻辑层
│   │       ├── compile_engine.py  # 编译引擎（容器/本地）
│   │       ├── skill_matcher.py   # AI Skill 匹配
│   │       ├── ticket_service.py  # 工单服务
│   │       └── experience_service.py # 经验服务
│   ├── requirements.txt
│   └── alembic/             # 数据库迁移（可选）
├── frontend/                # Vue3 + Vite + ElementPlus（禁止移到其他目录）
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── main.ts
│   │   ├── App.vue
│   │   ├── router/          # 路由配置
│   │   ├── views/           # 页面组件（按角色分目录）
│   │   │   ├── user/        # 用户前台（5页）
│   │   │   └── admin/       # 管理员后台（7页）
│   │   ├── components/      # 公共组件
│   │   │   ├── LogViewer.vue       # 长日志虚拟滚动 + WebSocket
│   │   │   ├── SkillPreview.vue    # SKILL.md 预览
│   │   │   ├── ChatBox.vue         # 工单对话气泡
│   │   │   └── StatusTag.vue       # 状态标签
│   │   ├── stores/          # Pinia 状态管理
│   │   └── utils/           # 工具函数（axios封装等）
│   └── public/
├── client/                  # 本地 Python 客户端（LoongArch）
│   ├── compile_client.py
│   ├── skill_sync.py
│   └── requirements.txt
├── skills/                  # OpenClaw Skill 定义（只读，禁止AI改写）
│   └── README.md            # Skill 库说明
└── .hermes/
    └── skills/              # Hermes 自定义技能（本项目专用）
        ├── vue3-elementplus-dev/
        ├── fastapi-dev/
        ├── loongarch-build-openclaw/  # ⚠️ 只读，禁止AI修改
        └── doc-sync-gen/
```

**强制约束**：
- 所有代码必须在此目录结构内，**禁止新建其他目录**存放代码
- 前端代码不得进入 `backend/`，后端代码不得进入 `frontend/`
- `skills/` 目录为 Skill 经验库，仅用于存放和读取，**禁止 AI 自动写入**

---

## 三、技术栈硬性约束

| 层级 | 技术 | 版本/约束 |
|------|------|----------|
| 后端框架 | FastAPI | 最新稳定版 |
| 数据验证 | Pydantic v2 | `model_config = ConfigDict(...)` 语法 |
| ORM | SQLAlchemy 2.0 | 异步/同步均可，统一使用 `sessionmaker` |
| 数据库 | PostgreSQL | 开发阶段可用 SQLite，生产用 PG |
| 缓存/队列 | Redis | 任务队列、WebSocket 广播、Skill 缓存 |
| 实时通信 | WebSocket | FastAPI `WebSocket` 类 |
| 前端框架 | Vue 3 | `<script setup>` 语法，**禁止 Vue2 语法** |
| UI 组件库 | Element Plus | 最新稳定版 |
| 构建工具 | Vite | TypeScript |
| 状态管理 | Pinia | Composition API 风格 |
| HTTP 客户端 | axios | 全局封装，拦截器处理 token |
| 客户端 | Python 3.9+ | 标准库 + requests |
| Skill 格式 | OpenClaw | 严格遵守 `SKILL.md` 标准（见设计文档 6.2） |

---

## 四、编码约束

### 4.1 后端
- 所有 API 必须加异常捕获（`try/except`），返回统一错误格式
- WebSocket 必须组件卸载销毁（`onUnmounted` 等价逻辑）
- 数据库 DDL **仅生成 SQL 脚本，禁止 Hermes 直接执行**
- 所有用户输入必须校验（Pydantic schema）
- 敏感操作记录审计日志

### 4.2 前端
- 所有组件使用 `<script setup>` 语法
- WebSocket / 定时器必须在 `onUnmounted` 销毁
- 长日志列表必须使用虚拟滚动（`el-virtual-list` 或自研）
- UI 布局、表格字段、弹窗交互**严格对齐 `compile-platform-mockup.html`**

### 4.3 通用
- **输出优先 diff，禁止全量重写文件**（除非全新文件）
- **一次只实现单个模块/单个文件**，禁止一次性写整套系统
- 每次任务开始前必须 `git commit` 保存当前状态

---

## 五、输入源约定

**所有需求、页面、字段、API、状态流转必须以下列两份材料为准**：

1. **设计文档**：`龙芯LoongArch智能编译平台设计文档.docx`（需求、架构、API、流程图）
2. **UI 原型**：`compile-platform-mockup.html`（页面布局、交互、样式）

**禁止行为**：
- ❌ 自行新增业务逻辑
- ❌ 自行新增状态值（编译任务状态、工单状态、Skill 审核状态必须与文档流程图完全一致）
- ❌ 自行修改 UI 布局或增加 mockup 中不存在的功能
- ❌ 自行设计数据库表结构（必须基于设计文档第十一节 API 推导）

---

## 六、状态机定义（必须严格遵守）

### 6.1 编译任务状态
```
submitted → compiling → success
                       → failed
```

### 6.2 工单状态
```
pending → processing → resolved → closed
           ↓
         (user) → 可重新打开
```

### 6.3 Skill 审核状态
```
submitted → under_review → approved → synced
                      → rejected
```

---

## 七、Hermes 会话规范

### 7.1 每次任务开场白（必须粘贴）
```
读取 ./project-spec.md，参考根目录下设计文档【龙芯LoongArch智能编译平台设计文档.docx】和UI原型【compile-platform-mockup.html】，加载skills目录下面的vue3-elementplus-dev、fastapi-dev技能。所有业务逻辑、字段、页面、API严格以两份文档为准，不要自行增加业务。一次只实现单个模块/单个文件，修改优先输出diff，不要全量重写完整文件。
```

### 7.2 会话拆分策略
| 会话 | 内容 | 输出物 |
|------|------|--------|
| 会话1 | 架构 & 数据库设计 | SQL DDL 脚本、OpenAPI 契约 |
| 会话2 | 后端 FastAPI 开发 | 逐个接口实现 |
| 会话3 | Vue 前端开发 | 页面组件 |
| 会话4 | Bug 调试 | 问题排查 |
| 会话5 | 本地客户端开发 | Python 客户端 |

### 7.3 权限控制
- ✅ 允许：文件读写、diff 对比、语法检查
- ❌ 禁止：执行 DDL、删除文件（`rm -rf`）、安装系统包
- ⚠️ 高危操作必须人工确认

---

## 八、API 接口清单（基于设计文档第十一节）

### 8.1 用户前台 API
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/compile/submit` | 提交编译任务 |
| GET | `/api/compile/tasks` | 获取任务列表 |
| GET | `/api/compile/tasks/{id}` | 获取任务详情 |
| GET | `/api/compile/tasks/{id}/log` | 获取编译日志 |
| POST | `/api/tickets/report` | 上报问题工单 |
| GET | `/api/tickets/my` | 获取我的工单 |
| POST | `/api/experience/submit` | 提交经验 |
| GET | `/api/experience/my` | 获取我的经验 |
| GET | `/api/skills` | 获取可用 Skill 列表 |
| GET | `/api/systems` | 获取目标系统列表 |

### 8.2 管理员后台 API
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/stats/overview` | 统计看板数据 |
| GET | `/api/tickets` | 工单列表 |
| GET | `/api/tickets/{id}` | 工单详情 |
| POST | `/api/tickets/{id}/messages` | 发送消息 |
| POST | `/api/tickets/{id}/resolve` | 标记解决 |
| GET | `/api/experience/pending` | 待审核经验 |
| POST | `/api/experience/{id}/approve` | 通过审核 |
| POST | `/api/experience/{id}/reject` | 驳回审核 |
| GET | `/api/skills` | Skill 列表 |
| POST | `/api/skills` | 新增 Skill |
| PUT | `/api/skills/{id}` | 更新 Skill |
| DELETE | `/api/skills/{id}` | 删除 Skill |
| GET | `/api/users` | 用户列表 |
| POST | `/api/audit/logs` | 操作日志 |
| GET | `/api/systems` | 目标系统列表 |
| POST | `/api/systems` | 新增目标系统 |
| PUT | `/api/systems/{id}` | 更新目标系统配置 |
| DELETE | `/api/systems/{id}` | 删除目标系统 |

---

## 九、安全与隐私规则

1. 本地客户端编译全程离线，源码/日志/产物**不上传云端**
2. 云端编译失败时，工单上报日志**自动脱敏**
3. Skill 经验库仅同步规则，不同步用户代码数据
4. 涉密代码场景必须使用本地客户端模式

---

## 十、版本信息

- 项目版本：v1.0
- 设计文档日期：2026-09-21
- 最后更新：2026-09-22
