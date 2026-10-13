"""
Database models for LoongArch Compile Platform
"""
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum
import logging

logger = logging.getLogger(__name__)

# 加载配置文件
import os
import yaml
BASE_DIR = '/root/.openclaw/workspace/compile-platform/backend'
CONFIG_PATH = os.path.join(BASE_DIR, 'config.yaml')

def load_db_config():
    """从 config.yaml 加载数据库配置"""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    db = cfg.get('database', {})
    db_type = db.get('type', 'postgresql')
    if db_type == 'postgresql':
        pg = db.get('postgresql', {})
        return {
            'url': f"postgresql://{pg['username']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['database']}",
            'pool_pre_ping': True,
            'pool_recycle': 3600
        }
    elif db_type == 'sqlite':
        sqlite = db.get('sqlite', {})
        path = sqlite.get('path', './cache/compile_platform.db')
        if not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return {
            'url': f"sqlite:///{path}",
            'check_same_thread': False
        }
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")

db_cfg = load_db_config()
engine = create_engine(db_cfg['url'], **(db_cfg.get('extra', {})))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class CompileMode(str, enum.Enum):
    CONTAINER = "container"
    LOCAL = "local"


class TaskStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    COMPILING = "compiling"
    SUCCESS = "success"
    FAILED = "failed"


class TicketStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ExperienceStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SYNCED = "synced"
    # 已被新版本/删除取代（旧 approved 行被新 approve 或被删 Skill 级联清时标）
    SUPERSEDED = "superseded"


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"

# 兼容字符串存储
UserRoleStr = {"user": UserRole.USER, "admin": UserRole.ADMIN}


# ==================== 数据库表定义 ====================

class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="user", nullable=False)  # user | admin
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    # 关系
    compile_tasks = relationship("CompileTask", back_populates="user")
    tickets = relationship("Ticket", back_populates="user")
    experiences = relationship("Experience", back_populates="user", foreign_keys="[Experience.user_id]")
    audit_logs = relationship("AuditLog", back_populates="user")


class TargetSystem(Base):
    __tablename__ = "target_systems"

    system_id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)  # 如：openEuler 24.03 LTS
    base_image = Column(String(255), nullable=False)  # Docker/Podman 镜像（不含 tag）
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    compile_tasks = relationship("CompileTask", back_populates="system")


class CompileTask(Base):
    __tablename__ = "compile_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(50), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    system_id = Column(Integer, ForeignKey("target_systems.system_id"), nullable=False)
    software_name = Column(String(100), nullable=False)
    source_type = Column(String(20), nullable=False)
    source_path = Column(String(255))
    git_url = Column(String(500))
    git_branch = Column(String(100))
    build_args = Column(Text)  # JSON 字符串
    compile_mode = Column(String(20), default="container")
    status = Column(String(20), default="submitted")
    matched_skills = Column(Text)  # JSON 字符串：匹配的 Skill ID 列表
    depend_tree = Column(Text)  # JSON 字符串：依赖树
    log_output = Column(Text)  # 编译日志
    artifact_path = Column(String(255))  # 产物路径
    error_message = Column(Text)
    container_id = Column(String(64))  # 编译容器 ID（cancel 时用于 docker stop）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    duration_seconds = Column(Integer)

    # 关系
    user = relationship("User", back_populates="compile_tasks")
    system = relationship("TargetSystem", back_populates="compile_tasks")
    ticket = relationship("Ticket", back_populates="task", uselist=False)
    experience = relationship("Experience", back_populates="task", uselist=False)


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("compile_tasks.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    source = Column(String(20), nullable=False)  # 来源
    status = Column(String(20), default="pending")
    title = Column(String(255), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at = Column(DateTime)

    # 关系
    task = relationship("CompileTask", back_populates="ticket")
    user = relationship("User", back_populates="tickets")
    messages = relationship("TicketMessage", back_populates="ticket", order_by="TicketMessage.created_at")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.ticket_id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    ticket = relationship("Ticket", back_populates="messages")
    user = relationship("User")


class Experience(Base):
    __tablename__ = "experiences"

    experience_id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("compile_tasks.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    skill_name = Column(String(100), nullable=False)
    skill_type = Column(String(20), nullable=False)  # 通用/专用
    system_id = Column(Integer, ForeignKey("target_systems.system_id"), nullable=False)
    status = Column(String(20), default="submitted")
    skill_content = Column(Text)  # SKILL.md 内容
    rejection_reason = Column(Text)
    submitted_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime)
    reviewer_id = Column(Integer, ForeignKey("users.user_id"))

    # 关系
    task = relationship("CompileTask", back_populates="experience")
    user = relationship("User", back_populates="experiences", foreign_keys="[Experience.user_id]")
    system = relationship("TargetSystem")
    reviewer = relationship("User", foreign_keys="[Experience.reviewer_id]")


class Skill(Base):
    __tablename__ = "skills"

    skill_id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    type = Column(String(20), nullable=False)
    description = Column(Text)
    system_ids = Column(Text)  # JSON 字符串：适用的系统 ID 列表
    version = Column(String(20))
    skill_content = Column(Text)  # SKILL.md 内容（DB 备份用，磁盘为准）
    script_content = Column(Text)  # run.sh 脚本内容
    is_active = Column(Boolean, default=True)
    # 来源：admin（管理页面创建）/ experience（用户提交经验通过审核）
    # 默认 admin；老记录没填也按 admin 视之
    source = Column(String(20), default="admin")
    # 软删：磁盘文件被删后，reconcile 把 deleted_at 设上（DB 行保留作备份）
    # 恢复 = write_skill_to_disk + deleted_at = None
    # 永久删除 = db.delete()（admin 手动操作）
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.user_id"))

    # 关系
    creator = relationship("User")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    action = Column(String(100), nullable=False)  # 操作类型
    target_type = Column(String(50))  # 目标类型：task/ticket/experience/user
    target_id = Column(Integer)
    details = Column(Text)  # JSON 字符串
    ip_address = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="audit_logs")


# ==================== 初始化函数 ====================

def _safe_add_column(table_name: str, column_name: str, ddl_type: str):
    """幂等 ALTER TABLE ADD COLUMN（PostgreSQL / SQLite 都吃）"""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if column_name in {c["name"] for c in insp.get_columns(table_name)}:
        return  # 已存在
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}'))
    logger.info("Added column %s.%s (%s)", table_name, column_name, ddl_type)


def _safe_drop_column(table_name: str, column_name: str):
    """幂等 ALTER TABLE DROP COLUMN（PostgreSQL/SQLite 都吃）"""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if column_name not in {c["name"] for c in insp.get_columns(table_name)}:
        return  # 不存在，no-op
    with engine.begin() as conn:
        # PG 用 DROP COLUMN；SQLite 也支持，括号防 keyword
        conn.execute(text(f'ALTER TABLE {table_name} DROP COLUMN "{column_name}"'))
    logger.info("Dropped column %s.%s", table_name, column_name)


def init_db():
    """初始化数据库（创建所有表 + 幂等迁移 + 种子数据）"""
    Base.metadata.create_all(bind=engine)
    # 幂等迁移：新增字段（老库升级用）
    _safe_add_column("skills", "source", "VARCHAR(20) DEFAULT 'admin'")
    _safe_add_column("skills", "deleted_at", "TIMESTAMP NULL")
    # 幂等迁移：删字段
    _safe_drop_column("skills", "sync_status")
    # 2026-10-13: TargetSystem 简化（drop arch / tag，只用 name + base_image）
    _safe_drop_column("target_systems", "arch")
    _safe_drop_column("target_systems", "tag")
    # 种子数据：默认 target_system（缺了用户提交编译会报"系统不存在"）
    _seed_default_target_systems()
    logger.info("Database initialized")


def _seed_default_target_systems():
    """seed 默认的目标系统（idempotent，已存在跳过）"""
    defaults = [
        {
            "name": "openEuler 24.03 LTS",
            "base_image": "loongnix-build:23.2",
        },
    ]
    db = SessionLocal()
    try:
        for s in defaults:
            existing = db.query(TargetSystem).filter(TargetSystem.name == s["name"]).first()
            if existing:
                continue
            db.add(TargetSystem(name=s["name"], base_image=s["base_image"]))
            logger.info("Seeded target_system: %s", s["name"])
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Seed target_systems failed: %s", e)
    finally:
        db.close()


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
