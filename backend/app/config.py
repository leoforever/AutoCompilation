"""
Configuration management
"""
import os
import yaml
from typing import Dict, Any
from pydantic import BaseModel


class Settings(BaseModel):
    # 数据库
    database_url: str = "sqlite:///./cache/compile_platform.db"
    
    # JWT
    secret_key: str = "loongarch-compile-platform-secret-2026"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 24 hours
    
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    
    # AI 服务
    llm_base_url: str = "http://localhost:8080"
    llm_api_key: str = ""
    
    # 编译引擎
    compile_timeout: int = 3600  # 秒
    max_concurrent_tasks: int = 10
    
    # 文件上传
    max_upload_size: int = 2 * 1024 * 1024 * 1024  # 2GB
    upload_dir: str = "./uploads"
    
    # 产物存储
    artifact_dir: str = "./artifacts"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        # 从 config.yaml 加载（如果存在）
        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")
        if os.path.exists(config_path):
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            _settings = Settings(**data)
        else:
            _settings = Settings()
    return _settings


def load_config() -> Dict[str, Any]:
    """加载完整配置"""
    settings = get_settings()
    return settings.model_dump()
