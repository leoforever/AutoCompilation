"""
Auth API tests

⚠️ 这个文件**曾经**会清空真 PostgreSQL `compile_platform` 库的所有表（包括 admin 账号）。
    现在改用临时 SQLite 做隔离：所有 create_all / drop_all 只动临时文件，真 PG 不受影响。

设计：
  - conftest.py 在 app.models 导入前 monkey-patch，把 engine / SessionLocal / Base 临时替换
  - app.routes.auth 内部 `next(get_db())` 走的是 SessionLocal()，动态获取，所以替换有效
  - 每个 test 前建表、test 后 drop 临时库（临时库在 /tmp 下，自动清理）
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 触发 anthropic shim（conftest.py 已经做了，保险起见）
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


# ===================== 临时 SQLite 引擎（隔离真 PG） =====================
@pytest.fixture(autouse=True)
def isolated_sqlite():
    """每个 test 用一个临时 SQLite 库，autouse 隔离真 PostgreSQL。

    替换 app.models 的 engine / SessionLocal / get_db，test 结束后恢复。
    """
    import app.models as models

    # 备份真值
    real_engine = models.engine
    real_session_local = models.SessionLocal
    real_get_db = models.get_db

    # 临时 SQLite
    tmp_dir = tempfile.mkdtemp(prefix="auth_test_")
    db_path = Path(tmp_dir) / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def _test_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    # 替换
    models.engine = test_engine
    models.SessionLocal = TestSessionLocal
    models.get_db = _test_get_db

    # 建表
    _RealBase.metadata.create_all(bind=test_engine)

    try:
        yield
    finally:
        # drop 临时库 + 恢复
        _RealBase.metadata.drop_all(bind=test_engine)
        test_engine.dispose()
        try:
            os.unlink(db_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass
        models.engine = real_engine
        models.SessionLocal = real_session_local
        models.get_db = real_get_db


# TestClient 在 fixture 之后初始化，确保所有请求走临时库
# 本文件测试注册逻辑本身 → 放开开关（生产默认关闭，见 test_register_disabled_by_default）
os.environ["ALLOW_SELF_REGISTER"] = "1"
client = TestClient(app)


# ===================== Tests =====================
def test_register_disabled_by_default(monkeypatch):
    """生产默认：不允许自助注册 → 403（开关是运行时 os.environ 读取，monkeypatch 即生效）"""
    monkeypatch.delenv("ALLOW_SELF_REGISTER", raising=False)
    response = client.post("/api/auth/register", json={
        "username": "no_self_reg", "password": "pass1234", "role": "user"
    })
    assert response.status_code == 403
    assert "暂不开放自助注册" in response.json()["detail"]


def test_register_success():
    """测试用户注册成功"""
    response = client.post("/api/auth/register", json={
        "username": "newuser_test",
        "password": "newpass123",
        "role": "user"
    })
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["data"]["username"] == "newuser_test"
    assert data["data"]["role"] == "user"


def test_register_duplicate_username():
    """测试重复用户名注册失败"""
    # 先注册一个用户
    client.post("/api/auth/register", json={
        "username": "dupuser",
        "password": "pass1234",
        "role": "user"
    })
    # 重复注册应失败
    response = client.post("/api/auth/register", json={
        "username": "dupuser",
        "password": "pass1234",
        "role": "user"
    })
    assert response.status_code == 400
    assert "用户名已被注册" in response.json()["detail"]


def test_register_short_password():
    """测试密码长度不足"""
    response = client.post("/api/auth/register", json={
        "username": "shortpass",
        "password": "123",
        "role": "user"
    })
    assert response.status_code == 422


def test_login_success():
    """测试登录成功"""
    # 先注册用户
    client.post("/api/auth/register", json={
        "username": "logintest",
        "password": "testpass123",
        "role": "user"
    })

    response = client.post("/api/auth/login", data={
        "username": "logintest",
        "password": "testpass123"
    })
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert "access_token" in data["data"]
    assert data["data"]["token_type"] == "bearer"
    assert data["user"]["username"] == "logintest"


def test_login_wrong_password():
    """测试错误密码登录失败"""
    # 先注册用户
    client.post("/api/auth/register", json={
        "username": "wrongpwd",
        "password": "correctpass",
        "role": "user"
    })

    response = client.post("/api/auth/login", data={
        "username": "wrongpwd",
        "password": "wrongpass"
    })
    assert response.status_code == 401
    assert "用户名或密码错误" in response.json()["detail"]


def test_login_user_not_found():
    """测试不存在的用户登录失败"""
    response = client.post("/api/auth/login", data={
        "username": "nonexistent",
        "password": "testpass"
    })
    assert response.status_code == 401
    assert "用户名或密码错误" in response.json()["detail"]


def test_get_me_with_token():
    """测试使用Token获取当前用户信息"""
    # 先注册用户并登录
    client.post("/api/auth/register", json={
        "username": "metest",
        "password": "testpass123",
        "role": "user"
    })
    login_response = client.post("/api/auth/login", data={
        "username": "metest",
        "password": "testpass123"
    })
    token = login_response.json()["data"]["access_token"]

    # 使用token获取用户信息
    response = client.get("/api/auth/me", headers={
        "Authorization": f"Bearer {token}"
    })
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["data"]["username"] == "metest"


def test_get_me_without_token():
    """测试未登录访问受保护接口"""
    response = client.get("/api/auth/me")
    assert response.status_code == 401


def test_get_me_with_invalid_token():
    """测试使用无效Token访问"""
    response = client.get("/api/auth/me", headers={
        "Authorization": "Bearer invalid_token"
    })
    assert response.status_code == 401
    assert "无效令牌" in response.json()["detail"]


def test_change_password_success():
    """测试修改密码成功"""
    # 先注册用户并登录
    client.post("/api/auth/register", json={
        "username": "changepwd",
        "password": "oldpass123",
        "role": "user"
    })
    login_response = client.post("/api/auth/login", data={
        "username": "changepwd",
        "password": "oldpass123"
    })
    token = login_response.json()["data"]["access_token"]

    # 修改密码
    response = client.post(
        "/api/auth/change-password",
        params={"old_password": "oldpass123", "new_password": "newpass456"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["message"] == "密码修改成功"

    # 使用新密码登录成功
    new_login = client.post("/api/auth/login", data={
        "username": "changepwd",
        "password": "newpass456"
    })
    assert new_login.status_code == 200


def test_change_password_wrong_old():
    """测试原密码错误时修改失败"""
    # 先注册用户并登录
    client.post("/api/auth/register", json={
        "username": "wrongold",
        "password": "correctold",
        "role": "user"
    })
    login_response = client.post("/api/auth/login", data={
        "username": "wrongold",
        "password": "correctold"
    })
    token = login_response.json()["data"]["access_token"]

    # 使用错误原密码修改
    response = client.post(
        "/api/auth/change-password",
        params={"old_password": "wrong_password", "new_password": "newpass456"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 400
    assert "原密码错误" in response.json()["detail"]


def test_login_user_disabled():
    """测试禁用用户登录失败"""
    # 创建禁用用户
    import bcrypt
    from app.models import User
    password_hash = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode()
    user = User(username="disableduser", password_hash=password_hash, role="user", is_active=False)
    # 用替换后的 SessionLocal 创建（写临时库）
    from app.models import SessionLocal
    db = SessionLocal()
    db.add(user)
    db.commit()
    db.close()

    response = client.post("/api/auth/login", data={
        "username": "disableduser",
        "password": "testpass123"
    })
    assert response.status_code == 403
    assert "账号已被禁用" in response.json()["detail"]
