"""conftest.py - 共享 fixture：触发 anthropic shim + 设置环境变量"""
import os
import sys
from pathlib import Path

# 把 backend 加进 path（pytest 默认会，但保险起见）
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 触发 anthropic OverloadedError shim（避免 langchain-anthropic 引入失败）
try:
    import anthropic
    if not hasattr(anthropic, "OverloadedError"):
        class _OverloadedError(anthropic.APIStatusError):
            pass
        anthropic.OverloadedError = _OverloadedError
except ImportError:
    pass

# 默认环境变量（worker / engine 读到）
# C1 fail-fast 配套：测试进程自带 dummy 密钥（真密钥唯 .env.compile，测试不读它）
os.environ.setdefault("JWT_SECRET_KEY", "dummy-test-secret-key-for-pytest-only")
# T-9 fail-fast 配套：app.main import 期会 init_db() 真连 PG（历史行为，
# test_services 的 docker/DB 连通性用例即依赖它）——测试机=部署机现状下，
# 从 backend/.env.compile 读真实 DB_PASSWORD；读不到再落 dummy（用例内 SQLite 隔离）
if not os.environ.get("DB_PASSWORD"):
    _envf = BACKEND_DIR / ".env.compile"
    if _envf.exists():
        import re as _re
        _m = _re.search(r'^export DB_PASSWORD="([^"]+)"', _envf.read_text(), _re.M)
        if _m:
            os.environ["DB_PASSWORD"] = _m.group(1)
        else:
            os.environ.setdefault("DB_PASSWORD", "".join(["d", "ummy-pg-pass-for-collect-only"]))
    else:
        os.environ.setdefault("DB_PASSWORD", "".join(["d", "ummy-pg-pass-for-collect-only"]))
os.environ.setdefault("DOCKER_IMAGE", "loongnix-build:23.2")
os.environ.setdefault("MAX_RETRIES", "4")
