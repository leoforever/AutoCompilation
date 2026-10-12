# 龙芯智能编译平台 - 启动指南

> 这是历史文档。新部署请直接看 [README.md](../../README.md)，用 `setup.sh` 一键搞定。
>
> 本文档保留作为历史参考，包含一些手动启动步骤的细节。

## 服务访问地址

| 服务 | 地址 | 说明 |
|------|------|------|
| 前端页面 | http://localhost:3000/ | Vue3 + ElementPlus UI |
| 后端API | http://localhost:8000/ | FastAPI REST API |
| API文档 | http://localhost:8000/docs | Swagger UI |
| 替代访问 | http://10.40.65.201:3000/ | 局域网访问 |

## 一、启动服务

### 方式1：一键启动脚本（推荐）

```bash
cd /root/.openclaw/workspace/compile-platform

# 首次：先 install（建 PG 库 + 装依赖 + 建表 + admin 账号）
bash setup.sh install
# 期间会提示编辑 backend/.env.compile，填 AI key

# 之后：start / stop / restart / status
bash setup.sh start
```

### 方式2：手动启动（调试用）

```bash
# 后端
cd /root/.openclaw/workspace/compile-platform/backend
set -a; source .env.compile; set +a
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 前端（另一终端）
cd /root/.openclaw/workspace/compile-platform/frontend
npm run dev -- --host 0.0.0.0
```

> 手动启动前必须先 `source .env.compile`，否则后端读不到 `AI_API_KEY` 等配置。

## 二、测试验证

### 1. 后端API测试

```bash
# 健康检查
curl http://localhost:8000/api/health

# 注册
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"test","password":"test123"}'
```
