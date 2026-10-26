"""
Database models for LoongArch Compile Platform
"""
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum
import logging

logger = logging.getLogger(__name__)

# 加载配置文件
import os
import yaml
from pathlib import Path

# 仓库根：默认从本文件位置推断（backend/app/models.py → 往上跳 2 层是仓库根）
# 可被 COMPILE_PLATFORM_DIR 环境变量覆盖（与 setup.sh 同源）
# 用法：COMPILE_PLATFORM_DIR=/your/path python -m ...
_PROJECT_ROOT_ENV = os.environ.get("COMPILE_PLATFORM_DIR")
if _PROJECT_ROOT_ENV:
    BASE_DIR = os.path.join(_PROJECT_ROOT_ENV, "backend")
else:
    # models.py 在 backend/app/，往上两级到仓库根，再加 backend
    BASE_DIR = str(Path(__file__).resolve().parents[1])
CONFIG_PATH = os.path.join(BASE_DIR, 'config.yaml')

def load_db_config():
    """数据库配置：env > config.yaml（T-9 2026-10-26：口令出清 git 并轮换；
    口令唯 .env.compile 的 DB_PASSWORD，yaml 无值且 env 无值 → 拒绝启动）"""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    db = cfg.get('database', {})
    db_type = db.get('type', 'postgresql')
    if db_type == 'postgresql':
        pg = dict(db.get('postgresql', {}) or {})
        host = os.environ.get("DB_HOST") or pg.get('host', 'localhost')
        port = os.environ.get("DB_PORT") or pg.get('port', 5432)
        user = os.environ.get("DB_USER") or pg.get('username')
        dbname = os.environ.get("DB_NAME") or pg.get('database')
        password = os.environ.get("DB_PASSWORD") or pg.get('password')
        if not password:
            raise RuntimeError(
                "DB_PASSWORD 未配置，拒绝启动。生成：bash setup.sh rotate-db-password"
                "（ALTER USER + 写 .env.compile 一步闭环），或手动 openssl rand -hex 16"
                " 追加 DB_PASSWORD=<串> 到 backend/.env.compile"
            )
        from urllib.parse import quote_plus
        return {
            'url': f"postgresql://{quote_plus(user)}:{quote_plus(str(password))}@{host}:{port}/{dbname}",
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
# 审查 #15 修复（2026-10-23 配置链落地顺带）：旧版只展开 db_cfg.get('extra')（恒不存在）
# → pool_pre_ping/pool_recycle/check_same_thread 静默蒸发，PG 重启后死连接打 500
engine = create_engine(db_cfg['url'], **{k: v for k, v in db_cfg.items() if k != 'url'})
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
    original_username = Column(String(50), nullable=True)  # 软删时原名快照（用于恢复 / 审计 / 显示）
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="user", nullable=False)  # user | admin
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    # 软删：NULL = 正常；非 NULL = 注销时间（行不删、引用不断、统计可用）
    # 删除时同时：is_active=False + 改名 deleted_<uid>_<ts>（释放 username unique）+ 清空 password_hash
    deleted_at = Column(DateTime, nullable=True)
    deleted_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)  # 谁操作的（admin 注销他人/自己）

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
    artifact_path = Column(Text)  # 产物路径 JSON 列表（多产物时 > 255 字符，不能用 String(255)）
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
    resolution = Column(Text)  # 管理员填的解决方案（必填，admin 标记已解决时写）

    # 关系
    task = relationship("CompileTask", back_populates="ticket")
    user = relationship("User", back_populates="tickets")
    messages = relationship("TicketMessage", back_populates="ticket", order_by="TicketMessage.created_at")
    agent_record = relationship("TicketAgent", back_populates="ticket", uselist=False, cascade="all, delete-orphan")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.ticket_id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    role = Column(String(20), nullable=False)
    # T8: 消息线别——chat=用户可见对话 / agent_cmd=admin→Agent 工作台指令（用户端不返回）
    kind = Column(String(12), default="chat", nullable=False)
    content = Column(Text, nullable=False)
    # T8: 用户侧已读标记（admin 回复→用户红点；打开对话面板清零）。历史行由 init 迁移全标 True
    is_read = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    ticket = relationship("Ticket", back_populates="messages")
    user = relationship("User")


class TicketAgent(Base):
    """工单答疑 agent 持久化记录（容器 + work_dir + checkpoint）

    设计目的：解决"每次接单都重新 git clone + docker run 5-30s"问题
    - container_id: 上次的容器 ID（复用时 docker start）
    - work_dir: artifacts/{task_id}-TKT{n}/ 路径（永不删；agent checkpoint 在这里）
    - checkpoint_path: langgraph state 序列化文件路径
    - skills_snapshot: 接单时一次注入的 skills 列表（resume 时一致）
    - last_error: 启动失败时记这里（admin 端可见）
    """
    __tablename__ = "ticket_agents"

    ticket_id = Column(Integer, ForeignKey("tickets.ticket_id"), primary_key=True)
    container_id = Column(String(64))
    work_dir = Column(Text)
    checkpoint_path = Column(Text)
    # checkpoint_state: langgraph state 序列化 JSON（跨进程/重启恢复多轮对话）
    checkpoint_state = Column(Text)
    # 2026-10-19 B3：agent 消息持久化（解决"退出再进 agent 消息丢"问题）
    # 存最近一轮的 messages list JSON（不存 DB ticket_messages，遵循 §62 决策）
    # admin 抽屉打开时 GET /agent/messages 拉取
    last_messages_json = Column(Text)
    skills_snapshot = Column(Text)  # JSON list[str]
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ticket = relationship("Ticket", back_populates="agent_record")


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
    ticket_id = Column(Integer, ForeignKey("tickets.ticket_id"), nullable=True)  # 来源工单（工单经验专用）
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
    _safe_add_column("tickets", "resolution", "TEXT")
    # 幂等迁移：删字段
    _safe_drop_column("skills", "sync_status")
    # 2026-10-13: TargetSystem 简化（drop arch / tag，只用 name + base_image）
    _safe_drop_column("target_systems", "arch")
    _safe_drop_column("target_systems", "tag")
    # 2026-10-16: User 软删（deleted_at + deleted_by + original_username）
    _safe_add_column("users", "deleted_at", "TIMESTAMP NULL")
    _safe_add_column("users", "deleted_by", "INTEGER NULL")
    _safe_add_column("users", "original_username", "VARCHAR(50) NULL")
    # 2026-10-20: 工单经验统一走 experience 审批流（ticket_id 标识来源工单）
    _safe_add_column("experiences", "ticket_id", "INTEGER NULL")
    # T8 工单对话线：ticket_messages.is_read（2026-10-22）。存量历史消息一律标已读，
    # 防功能上线瞬间满屏红点（用户拍板）；新 admin 回复默认未读由写入侧显式置 False
    # 回填由 DDL 的 DEFAULT TRUE 天然完成（ADD COLUMN 存量行自动填默认值，无需 UPDATE；
    # 别在这写 db.execute——init_db 作用域没有 session，2026-10-22 启动炸 NameError 教训）
    _safe_add_column("ticket_messages", "is_read", "BOOLEAN NOT NULL DEFAULT TRUE")
    _safe_add_column("ticket_messages", "kind", "VARCHAR(12) NOT NULL DEFAULT 'chat'")
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
