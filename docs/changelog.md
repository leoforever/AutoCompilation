# 龙芯 LoongArch 智能编译平台 · 开发笔记

## 2026-09-22

### 项目初始化
- 创建项目结构：backend/frontend/client/skills/docs
- 编写项目契约文件 project-spec.md
- 创建 4 个 Hermes 技能：
  - vue3-elementplus-dev：前端开发规范
  - fastapi-dev：后端开发规范
  - loongarch-build-openclaw：Skill 经验库（只读）
  - doc-sync-gen：文档同步生成

### 后端骨架
- 创建 FastAPI 应用入口 main.py
- 定义 SQLAlchemy ORM 模型（models.py）
- 定义 Pydantic v2 请求/响应模型（schemas.py）
- 实现路由：
  - compile.py：编译任务提交、列表、详情、日志
  - tickets.py：工单上报、列表、消息
  - experience.py：经验提交、审核
  - admin.py：统计看板、Skill CRUD、系统管理
  - ws.py：WebSocket 实时日志推送

### 前端骨架
- 创建 Vue3 + Vite + ElementPlus 项目
- 配置路由（用户前台 5 页 + 管理员后台 7 页）
- 实现状态管理（Pinia role store）
- 实现 axios 封装（utils/request.ts）
- 创建页面组件：
  - 用户前台：Workbench、Tasks、Tickets、Contributions、Download
  - 管理员后台：Dashboard、TicketsAdmin、Review、SkillLib、Users、ChatAdmin

### 数据库设计
- 输出 DDL 脚本（docs/sql/schema.sql）
- 包含 8 张核心表：users、target_systems、compile_tasks、tickets、ticket_messages、experiences、skills、audit_logs

### 文档
- API 接口文档（docs/api.md）
- 开发笔记（docs/changelog.md）

## 下一步

1. 运行数据库初始化
2. 启动后端服务
3. 安装前端依赖并启动
4. 联调测试
5. 实现编译引擎（容器/本地）
6. 对接 AI 中台（Skill 匹配、流程编排）
