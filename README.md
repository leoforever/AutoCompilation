# 龙芯 LoongArch 智能编译平台

面向龙芯 LoongArch 架构的软件源码编译场景：云端容器编译 + AI Agent（DeepAgents）执行 + 工单人工兜底 + 经验闭环沉淀（编译现场记录 BUILD_NOTES → 攻略草稿 → 审批 → Skill 库）。

## 项目结构

```
compile-platform/
├── backend/           # FastAPI 后端（worker 线程池在进程内跑编译）
├── frontend/          # Vue3 + Element Plus 前端
├── client/            # 本地 Python 客户端（私有化部署用）
├── skills/            # Skill 经验库（agent 编译时直接读磁盘；git 跟踪，DB 为备份）
├── artifacts/         # 编译产物与日志（按 task_id/ 归档，运行数据不入 git）
├── docs/              # API 文档、教程、变更记录
└── project-spec.md    # 项目契约文件
```

## 快速开始（新环境部署）

非容器化部署，`setup.sh` 一条命令装好。**唯一必填**：AI key（agent 编译靠它）。

```bash
# 1. 克隆
git clone <repo-url> compile-platform && cd compile-platform

# 2. 一键安装：装 PG → 建库建用户 → 复制 .env → 建表+种子数据 → 装前后端依赖 → 启动
#    管理员密码必须在初始化时显式给出（不再默认 admin123）：
ADMIN_INIT_PASSWORD='***' bash setup.sh install
#    （不带该变量且在交互终端下运行会提示输入两次；非交互环境直接 fail-fast）

# 3. 编辑 backend/.env.compile 填 AI key（install 过程中会提示）
#    AI_API_KEY / AI_BASE_URL / AI_MODEL —— 模板见 .env.compile.example

# 4. 访问
#    前端 http://<host>:3000/    API http://<host>:8000/docs
```

**编译沙箱前置**：stage3 需要龙芯构建镜像。`loongnix-build:23.2` 为自制镜像（无公共源，需 `docker load` 迁移包或按镜像文档重建）；openEuler 系镜像走 lcr.loongnix.cn 公共 pull。新环境先 `docker images` 确认，缺镜像时任务会失败在阶段 3。

## setup.sh 子命令

| 命令 | 作用 |
|---|---|
| `install` | 一次性环境准备：装 PG / 建库 / 依赖 / 建表 / admin 账号 / 启动 |
| `init` | 建表+幂等迁移+种子数据+admin（走完整 `init_db()` 迁移链；需 `ADMIN_INIT_PASSWORD`） |
| `start` / `stop` / `restart` / `status` | 服务管理（restart 支持 dev/prod 模式参数） |
| `uninstall` | 数据级重置：停服务、清平台容器、DROP 库（先 pg_dump 备份）、删 artifacts/logs/cache；镜像/skills/配置保留 |
| `uninstall --purge` | 全清档：另删自制镜像（单独二次确认）、skills/、.env、dist、node_modules、PG 账号 |
| `--yes` / `RECONCILE_AUTO=0` | uninstall 免交互 / 对账只报告不自动改库 |

## 技术栈

- **后端**：FastAPI + SQLAlchemy 2.0 + Pydantic v2 + PostgreSQL（`backend/config.yaml`）
- **前端**：Vue 3 + Vite + Element Plus + Pinia（开发 `vite`，生产 nginx/静态）
- **AI 引擎**：DeepAgents（langchain），system_prompt 只给元信息、具体知识从 skills 读
- **编译沙箱**：Docker 容器（loongnix-build:23.2 / openEuler 镜像），worker 线程池进程内执行
- **思考型模型适配**：reasoning 计入 max_completion_tokens（草稿生成 65536、工单总结 2048、2048 会 content 空）

## 核心机制（速览）

- **编译流水线**：阶段1 初始化 → 2 环境 → 3 建容器 → 4a 拉源码 → 4 就绪 → 5 DeepAgents 执行（硬闸：时长 STAGE5_MAX_SECONDS=3h / chunk 500 / 连续快速失败）→ 6 归档清理
- **心跳与看门狗**：每任务 60s 心跳线程刷 updated_at；worker 真死（心跳停 >30min）才由看门狗回收——长静默命令（pip 源码编译、submodule）不再被误杀
- **BUILD_NOTES 制度**：agent 强制维护编译现场记录（开工建骨架/分段更新/成败收尾），stage6 落定 `artifacts/<task_id>/BUILD_NOTES.md`；成功喂经验流、失败喂工单流
- **统一终态写入器**（task_writer）：核心状态与日志分块提交、NUL/非法字符净化、失败落 `.persist_error` 哨兵 + WS 告警 + stats 计数
- **对账自愈**（task_reconciler）：每小时扫冻结任务，有成功现场证据自动恢复并留审计；无证据不动
- **经验闭环**：工单 resolve → 攻略草稿（preview-job 异步轮询）→ 审批页预览可编辑 → 通过写 Skill 表 + 落盘 `skills/`（stage5 读磁盘生效）
- **Skill 命中收口**：agent 读过的 SKILL.md 记入 `matched_skills` 列 + WS 事件 + build.log 收口行
- **自助注册默认关闭**：账号由管理员在用户管理页创建（`ALLOW_SELF_REGISTER=1` 可开）

## 文档

- [API 接口文档](docs/api.md)
- [入门教程](docs/TUTORIAL.md) ｜ [测试指南](docs/test-guide.md)
- [变更记录](docs/changelog.md) ｜ [项目契约](project-spec.md)
- Skill 库说明：`skills/README.md`

## 开发规范

详细规范见 `project-spec.md`；调试经验与踩坑记录在 Hermes 技能 `compile-platform-debugging`（部署 TODO、BUILD_NOTES 设计、账本设计等 references）。
