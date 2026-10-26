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
os.environ.setdefault("DOCKER_IMAGE", "loongnix-build:23.2")
os.environ.setdefault("MAX_RETRIES", "4")
