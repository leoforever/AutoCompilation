# 龙芯 LoongArch 智能编译平台

面向龙芯 LoongArch 架构的软件编译场景，打造云端公共编译服务 + 本地私有化客户端编译双模式智能编译平台。

## 项目结构

```
compile-platform/
├── backend/           # FastAPI 后端
├── frontend/          # Vue3 + ElementPlus 前端
├── client/            # 本地 Python 客户端（私有化部署用）
├── skills/            # OpenClaw Skill 经验库（agent 编译时直接读）
├── docs/              # 接口文档、设计笔记
└── project-spec.md    # 项目契约文件
```

## 快速开始（生产部署 / 新机器）

非容器化部署，使用 `setup.sh` 一键安装启动。

```bash
# 1. 克隆
git clone <repo-url> compile-platform
cd compile-platform

# 2. 一键安装（首次：装 PG、建库表、装 Python/前端依赖、初始化 admin 账号）
bash setup.sh install
# 期间会提示编辑 backend/.env.compile，填 AI_API_KEY / AI_BASE_URL / AI_MODEL
# （AI key 必填，否则 agent 编译时调用 LLM 会失败）

# 3. 启动
bash setup.sh start

# 4. 访问
#   前端：http://<host>:3000/
#   API:  http://<host>:8000/docs
#   默认账号：admin / admin123
```

`setup.sh` 子命令：

| 命令 | 作用 |
|---|---|
| `install` | 一次性环境准备：装 PG / 建库 / 装依赖 / 建表 / 默认 admin 账号 |
| `init` | 仅"建表 + 默认 admin 账号"，便于重置 |
| `start` | 启动后端 + 前端（不初始化） |
| `stop` | 按端口探活后停掉 |
| `restart` | stop + start |
| `status` | 探活报告实际运行情况 |

## 技术栈

- **后端**：FastAPI + SQLAlchemy 2.0 + Pydantic v2
- **前端**：Vue 3 + Vite + Element Plus + Pinia
- **数据库**：PostgreSQL（默认）/ SQLite（兜底）
- **AI**：DeepAgents + langchain（通过 SKILLS_PATHS 路径注入 skill）
- **编译沙箱**：Docker（`loongnix-build:23.2` 镜像，龙芯专用）

## 关键路径

- `backend/.env.compile`：**不入 git**，含 AI key / Docker 镜像 / DB 配置。首次安装由 setup.sh 从 `.env.compile.example` 复制
- `backend/.env.compile.example`：模板文件，**入 git**
- `skills/`：OpenClaw 经验库，agent 编译时直接读磁盘
- `artifacts/`：编译产物，按 `task_id/` 归档
- `logs/`：后端 + 前端运行日志 + PID 文件

## Skill 经验库管理

`skills/` 目录是 agent 编译时**唯一**读取的来源。管理员通过 `SkillLib` 页面（`/admin/SkillLib`）管理：

- **新增 / 编辑 / 删除** Skill：写盘 + 同步 DB
- **磁盘 → DB 自动同步**：每次进入 SkillLib 页面自动触发 reconcile
- **误删容错**：删除的 skill 进 DB 回收站，可恢复或永久删除
- 详细设计见 `skills/README.md`

## 文档

- [API 接口文档](docs/api.md)
- [数据库 DDL](docs/sql/schema.sql)
- [项目契约](project-spec.md)
- [变更记录](docs/changelog.md)
- [开发排期](docs/roadmap.md)

## 开发规范

详细开发规范请参考 `project-spec.md` 和 `.hermes/skills/` 目录下的技能文件。
