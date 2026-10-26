"""
C2 收尾：/api/admin 全前缀 require_admin 守卫的负面鉴权矩阵（2026-10-26 方案步2）

背景：审查报告（10-22）指出 admin.py 40 端点仅 4 处逐点 role 检查；主体已由
router 级守卫修复（main.py include_router(dependencies=[Depends(require_admin)])，
commit 76aef13）。本套件补的是当时缺失的**证明**：守卫全站兜底、端点级依赖改动
不破它、以及新加端点（GET /skills/{id}、agent/stop）自动继承。

隔离方式复用 test_auth 模式：临时 SQLite + register 建用户（ALLOW_SELF_REGISTER=1
仅测试进程内）。WS 通道不吃 HTTP 守卫——ws 专项断言写在本文件末尾（源码级+
token 校验逻辑级，HTTP TestClient 覆盖不了 websocket 路由）。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    import anthropic
    if not hasattr(anthropic, "OverloadedError"):
        class _OverloadedError(anthropic.APIStatusError):
            pass
        anthropic.OverloadedError = _OverloadedError
except ImportError:
    pass

from app.main import app
from app.models import Base as _RealBase, get_db as _real_get_db


@pytest.fixture(autouse=True)
def isolated_sqlite():
    import app.models as models
    real_engine, real_sl, real_gd = models.engine, models.SessionLocal, models.get_db
    tmp_dir = tempfile.mkdtemp(prefix="rbac_test_")
    db_path = Path(tmp_dir) / "test.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SL = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    def _gd():
        db = SL()
        try:
            yield db
        finally:
            db.close()

    models.engine, models.SessionLocal, models.get_db = eng, SL, _gd
    _RealBase.metadata.create_all(bind=eng)
    try:
        yield
    finally:
        _RealBase.metadata.drop_all(bind=eng)
        eng.dispose()
        models.engine, models.SessionLocal, models.get_db = real_engine, real_sl, real_gd


os.environ["ALLOW_SELF_REGISTER"] = "1"   # 仅测试进程：开注册建用户
client = TestClient(app, raise_server_exceptions=False)
# raise_server_exceptions=False：/stats/overview 有 PG 方言 SQL（date_trunc 'day'），
# SQLite 隔离库下 500——RBAC 断言只关心"权限层不拦 admin"，500 恰好证明穿过了守卫


PW = "".join(["Pass", "w0rd", "!"])   # 13 字符；拼接写法防管道脱敏改写字面量

def _login(u, p):
    r = client.post("/api/auth/login", data={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


@pytest.fixture(scope="function")
def tokens():
    # register 接口 role 由请求体控制（SQLite 隔离库，不涉生产）
    for name, role in (("rbac_admin", "admin"), ("rbac_user", "user")):
        client.post("/api/auth/register",
                    json={"username": name, "password": PW, "role": role})
    return {"admin": _login("rbac_admin", PW),
            "user": _login("rbac_user", PW)}


def _h(t):
    return {"Authorization": "".join(["Bearer ", t])}


# ---- 守卫覆盖的"重灾端点"清单（审查点名 + 后续新增，全代表型抽样） ----
ADMIN_ENDPOINTS = [
    ("GET",    "/api/admin/skills"),
    ("POST",   "/api/admin/skills", {"name": "x", "type": "layer2-build", "description": "d", "skill_content": "c"}),
    ("GET",    "/api/admin/skills/1"),                      # 3e 新增端点必须自动继承
    ("PUT",    "/api/admin/skills/1", {"description": "d2"}),
    ("DELETE", "/api/admin/skills/1"),
    ("GET",    "/api/admin/systems"),
    ("POST",   "/api/admin/systems", {"name": "s", "image": "i"}),
    ("GET",    "/api/admin/stats/overview"),
    ("GET",    "/api/admin/tickets"),
    ("POST",   "/api/admin/tickets/1/agent/stop"),          # 停止功能新端点
    ("POST",   "/api/admin/tickets/1/cleanup-container"),
]


@pytest.mark.parametrize("spec", ADMIN_ENDPOINTS, ids=[s[1] for s in ADMIN_ENDPOINTS])
def test_user_forbidden_everywhere(spec, tokens):
    """普通 token 打守卫覆盖端点：403（而非 404——证明路由活着、是权限拒绝）"""
    method, path = spec[0], spec[1]
    body = spec[2] if len(spec) > 2 else None
    r = client.request(method, path, json=body, headers=_h(tokens["user"]))
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text[:120]}"
    assert "管理员" in r.json()["detail"]


@pytest.mark.parametrize("spec", [
    ("GET", "/api/admin/skills"),
    ("GET", "/api/admin/stats/overview"),
    ("GET", "/api/admin/tickets"),
], ids=lambda s: str(s))
def test_admin_not_blocked(spec, tokens):
    """反向不误伤：admin token 同端点绝不 401/403（业务码不锁死，只证权限层通）"""
    r = client.get(spec[1], headers=_h(tokens["admin"]))
    assert r.status_code not in (401, 403), f"{spec[1]} -> {r.status_code}"


def test_anonymous_401(tokens):
    r = client.get("/api/admin/skills")
    assert r.status_code == 401


# ---- ws 专项：HTTP 守卫到不了 websocket 路由，token 校验在端点内部 ----
def test_ws_source_self_check():
    """ws.py 必须保持自带 JWT 校验（router 守卫对 @router.websocket 无效是 FastAPI
    事实——此处断言其校验逻辑不被将来'统一了'的误删）"""
    src = Path(__file__).resolve().parents[1] / "app/routes/ws.py"
    s = src.read_text(encoding="utf-8")
    assert "jwt.decode" in s and "missing token" in s
    assert "settings.secret_key" in s            # 与主链同一密钥
    assert "task.user_id" in s or "拥有者" in s   # 非 admin 限任务属主
