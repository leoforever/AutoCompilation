"""
Configuration management —— 统一配置链（config-params-plan v2 落地）

优先级统一：env > backend/config.yaml > 代码默认。
- 密钥/凭据 → .env.compile（AI 三件套；JWT_SECRET_KEY fail-fast 属最终批未启用）
- 运维可调参数 → config.yaml 各段（jwt/worker/stage5/docker_exec/compile/auth/server）
- 进程内部协议常量 → 留代码默认，不开放
历史病灶（本文件旧版）：路径指向仓库根 config.yaml（不存在）+ 平铺字段接不住嵌套段
→ yaml 100% 不生效，"配置靠代码版本"。2026-10-23 修通。
"""
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel


# 与 models.py 同款路径推断：COMPILE_PLATFORM_DIR env 优先，否则本文件反推
# （backend/app/config.py → parents[1] = backend/，config.yaml 的家）
_PROJECT_ROOT_ENV = os.environ.get("COMPILE_PLATFORM_DIR")
if _PROJECT_ROOT_ENV:
    BASE_DIR = str(Path(_PROJECT_ROOT_ENV).resolve() / "backend")
else:
    BASE_DIR = str(Path(__file__).resolve().parents[1])
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")


class Settings(BaseModel):
    """平铺视图：仅保留真实消费字段（旧 7 个零引用死字段已删，见方案·四）"""
    # JWT（auth.py 签发链）
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24
    secret_key: str = ""  # yaml jwt.secret_key 填充；fail-fast 化在最终批（1c）
    # 产物/上传
    artifact_dir: str = "./artifacts"
    max_upload_size: int = 2 * 1024 * 1024 * 1024  # 2GB


_raw: dict = {}
_settings: Optional[Settings] = None


def _load_yaml() -> dict:
    global _raw
    if not _raw:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    _raw = yaml.safe_load(f) or {}
            except Exception:
                _raw = {}
    return _raw


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        raw = _load_yaml()
        j = raw.get("jwt") or {}
        c = raw.get("compile") or {}
        u = raw.get("upload") or {}
        _settings = Settings(
            algorithm=str(j.get("algorithm", "HS256")),
            access_token_expire_minutes=int(j.get("expire_minutes", 1440)),
            secret_key=str(j.get("secret_key", "") or ""),
            artifact_dir=str(c.get("artifact_dir", "./artifacts")),
            max_upload_size=int(u.get("max_size", 2 * 1024 * 1024 * 1024)),
        )
        # C1（2026-10-26 方案步4）：env > yaml > 无值拒绝——泄露串已出清 git/yaml，
        # 密钥唯 .env.compile 一个来源（env 优先级与配置链 v2 的 env>yaml 规则一致）
        _sk = os.environ.get("JWT_SECRET_KEY") or _settings.secret_key
        if not _sk:
            raise RuntimeError(
                "JWT_SECRET_KEY 未配置，拒绝启动（密钥泄露已轮换）。"
                "生成：bash setup.sh install 自动写入，或 "
                "openssl rand -hex 32 后追加 JWT_SECRET_KEY=<串> 到 backend/.env.compile"
            )
        _settings.secret_key = _sk
    return _settings


def _coerce(v: Any, default: Any) -> Any:
    """按 default 类型转换 env/yaml 字符串值；default=None 时原样返回"""
    if isinstance(default, bool):
        return str(v).lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return v


def get_cfg(key: str, default: Any = None, env_alias: "str | list[str] | None" = None) -> Any:
    """读 "section.key"（如 "stage5.max_seconds"）：env > config.yaml > default。

    env 名 = 大写+点转下划线（stage5.max_seconds ↔ STAGE5_MAX_SECONDS）；
    env_alias 兼容历史 env 名（MAX_WORKERS/ARTIFACTS_DIR 等，向后兼容零漂移）。
    """
    names: list[str] = [env_alias] if isinstance(env_alias, str) else list(env_alias or [])
    key_env = key.upper().replace(".", "_")
    if key_env not in names:
        names.append(key_env)
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return _coerce(v, default)
    node: Any = _load_yaml()
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            break
    if node is None:
        return default
    return _coerce(node, default) if isinstance(node, str) else node


def load_config() -> dict:
    """加载完整配置（兼容旧调用名）"""
    return get_settings().model_dump()

def artifacts_dir():
    """产物根目录唯一权威解析（Q2 步3，2026-10-26：原 compile_engine._artifacts_dir
    与 routes/compile.py 各写一份同款逻辑，统一到此）。
    env ARTIFACTS_DIR > yaml compile.artifact_dir > 默认；相对路径按仓库根解析
    （config.py 在 app/ 下 → parents[2]=仓库根），换部署目录不失效。"""
    from pathlib import Path
    base = Path(get_cfg("compile.artifact_dir", get_settings().artifact_dir, env_alias="ARTIFACTS_DIR"))
    if not base.is_absolute():
        base = Path(__file__).resolve().parents[2] / base
    base.mkdir(parents=True, exist_ok=True)
    return base
