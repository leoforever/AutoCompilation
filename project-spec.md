# 龙芯 LoongArch 智能编译平台 · 项目契约

> **本文件性质**：对 AI 助手（Hermes）的工程约束——目录结构、技术栈、编码纪律、状态机、红线。
> **实现现状以代码 + README + docs/api.md 为准**；历史设计文档（.docx / mockup.html）不在本仓库，不再作为"唯一事实源"引用（2026-10-22 修订：契约与实现对齐，删除已失效的附件依赖）。
> **禁止**：Hermes 不得自行新增业务逻辑、字段、状态值——要加先提案、用户拍板。

---

## 一、项目简介

面向龙芯 LoongArch 架构的软件源码编译平台：**云端容器编译 + DeepAgents 执行 + 工单人工兜底 + 经验闭环**（BUILD_NOTES 现场记录 → 攻略草稿 → 审批 → Skill 库 → 下次编译命中）。

核心闭环：用户编译 → 问题反馈 → 人工/Agent 处理 → 经验沉淀 → 复用加速。

> 原"本地私有化客户端"双模式为远期规划：`client/` 仅骨架，当前实现为纯云端容器模式。涉密场景暂不支持，安全条款见第九节现状版。

---

## 二、目录结构（与代码同步，禁止随意增删顶层目录）

```
compile-platform/
├── project-spec.md          # 本文件，改本项目代码前必读
├── README.md                # 部署/使用入口
├── setup.sh                 # install/init/start/stop/restart/status/uninstall
├── docs/                    # API、教程、变更记录（注：docs/ 不入 git，磁盘为准）
├── backend/                 # FastAPI 后端（禁止移动）
│   ├── app/
│   │   ├── main.py          # 入口 + lifespan（worker 线程池、心跳、sweep 对账挂点）
│   │   ├── config.py        # 配置加载（.env.compile > 默认值）
│   │   ├── models.py        # ORM（PostgreSQL；新列经 init_db 的 _safe_add_column 幂等迁移）
│   │   ├── schemas.py       # Pydantic v2（Optional 一律 = None；上限对齐列宽）
│   │   ├── dependencies.py  # JWT 鉴权、DB 会话
│   │   ├── routes/          # auth / compile / tickets / experience / admin / ws
│   │   └── services/        # compile_engine（6 阶段引擎）· docker_backend · ticket_agent
│   │                        #   task_queue · task_writer（唯一终态写入口）· task_reconciler（对账自愈）
│   │                        #   skill_loader · skill_draft · experience_generator · ws_broadcaster · sample_data
│   ├── config.yaml          # 数据库连接
│   ├── .env.compile         # AI key/镜像/超时（不入 git）；.example 模板入 git
│   └── tests/               # pytest 套件
├── frontend/                # Vue3 + Vite + Element Plus + Pinia（禁止移动）
│   └── src/views/           # user 5 页（Workbench/Tasks/Tickets/Contributions/Download）
│                            # admin 7 页（Dashboard/TicketsAdmin/Review/SkillLib/Users/Systems/ChatAdmin）
├── client/                  # 本地客户端骨架（远期，勿扩功能）
└── skills/                  # SKILL.md 经验库：git 跟踪；仅经审批流/API 写入，禁止 AI 直改文件
```

**强制约束**：
- 前后端代码不得互串目录；新模块放对应 services/ 或 routes/ 下
- `skills/` 的产生路径唯一：Experience 审批流 approve（写表+落盘）或 admin Skill API——绕过审批的直改一律禁止
- 终态落库唯一入口 `services/task_writer.write_terminal`——禁止新开 `task.status = ...` 直写点

## 三、技术栈（当前实际）

FastAPI + SQLAlchemy 2.0 + Pydantic v2 + PostgreSQL（config.yaml，compile_user/compile_platform）；
Vue 3 `<script setup>` + Vite + Element Plus + Pinia + axios（全局 30s，慢端点调用级放宽/异步 job）；
DeepAgents/langgraph（checkpointer 显式传参）+ docker SDK；WS = **进程内 BROADCASTER 单例**（原设计 Redis 队列/广播未采用——worker 与 API 同进程，DB 轮询队列够用）；无 alembic（迁移走 init_db 幂等链）。

## 四、编码约束

**后端**：API 统一异常捕获返回 `{code,message,data}`；用户输入必过 Pydantic；敏感操作写 AuditLog；DDL 变更走 models.py + init_db（幂等，自动执行）；**async 端点内禁止直调分钟级同步 IO**（docker/git clone/requests）——必须 `asyncio.to_thread`/executor，否则冻结全站（2026-10-22 接单事故）。
**前端**：`<script setup>`；WS/定时器 `onUnmounted` 销毁；长日志虚拟滚动；大文本默认折叠懒加载（不点不请求、失败不弹错）；>30s 的外部调用端点按 job+轮询设计，不靠放宽超时硬扛。
**通用**：优先 diff 不大改；改前 git 干净；后端改动需 `setup.sh restart`（挑编译空窗——重启杀在跑任务）；前端改动 `npx vite build` 同步 dist。

## 五、状态机（与代码一致）

```
编译任务：submitted → compiling → success / failed / cancelled
工单：    pending → processing → resolved → closed（closed 触发容器回收 recycle）
经验审批：submitted →（approve：写 Skill 表+落盘，同名旧版 superseded）/ rejected
```

## 六、AI 会话规范

- 开场必读本文件 + README；调试经验/设计档案在 Hermes 技能 `compile-platform-debugging`（~/.hermes/skills/，含部署 TODO、账本设计、BUILD_NOTES 设计）
- 一次一个模块；"先别改"=零 patch 只出方案；"记着"=写进技能 references；数据删除类操作先影子演练
- 权限：允许文件读写/语法检查/只读查询；执行 DDL 破坏性语句、rm -rf、装系统包、sudo 必须用户明示
- 验证：改完必须实跑（pytest 基线 39 passed + 针对性 ad-hoc），空口"已完成"无效

## 七、API 面

端点清单与语义以 **docs/api.md**（按代码实测重写版）+ Swagger `/docs` 为准，本文件不再维护副本（原第八节清单系 9 月设计稿，与实现已有出入，废弃）。

## 八、安全与隐私（现状版）

1. 当前仅云端模式：用户源码上传后存 `artifacts/<task_id>/`，任务终态后保留（uninstall 清除）
2. 工单上报可附编译日志；`AI_API_KEY` 等密钥只存 `.env.compile`（不入 git），日志/草稿生成需避免回显 key
3. Skill 库只存编译经验（命令/坑/攻略），不得包含用户源码片段
4. 自助注册默认关闭（ALLOW_SELF_REGISTER），账号由管理员创建；JWT + 角色守卫，普通用户不可达 admin 面

## 九、验收口径（平台立场，2026-10-22 定）

编译验收 = **基本三件套**（干净环境安装 / 导入加载 / 一次最小调用）；上游全量测试套件（pytest/test_ops/gradcheck/benchmark）非必需且禁止默认执行——已制度化注入 task_prompt、两草稿生成器、build-notes 模板四处，改动勿只动其一。

## 十、版本信息

- 契约版本：v2（2026-10-22 瘦身对齐实际；v1=2026-09-22 设计期，依赖的外部 docx/mockup 不在仓库）
- 实现事实源：代码 + README + docs/api.md + git log
