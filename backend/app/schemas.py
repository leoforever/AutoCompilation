"""
Pydantic schemas for request/response validation
"""
import json
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from typing import Optional, List, Any
from datetime import datetime
from app.models import CompileMode, TaskStatus, TicketStatus, ExperienceStatus, UserRole


# ==================== 用户相关 ====================

class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    role: UserRole = UserRole.USER


class UserPasswordReset(BaseModel):
    """管理员重置指定用户密码"""
    model_config = ConfigDict(str_strip_whitespace=True)
    new_password: str = Field(..., min_length=6)


class UserAdminCreate(BaseModel):
    """管理员新增用户（角色可选，默认普通用户）"""
    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    role: str = Field(default="user", pattern="^(user|admin)$")


class UserAdminUpdate(BaseModel):
    """管理员编辑用户（角色 + 启用/禁用，均可选）"""
    role: Optional[str] = Field(default=None, pattern="^(user|admin)$")
    is_active: Optional[bool] = None


class UserLogin(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: int
    username: str
    role: UserRole
    last_active: Optional[datetime]
    is_active: bool


# ==================== 目标系统 ====================

class TargetSystemCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=100)
    # 镜像（不含 tag，admin 写完整 image:tag 字符串，如 loongnix-build:23.2）
    # 默认值给老数据迁移兜底（init_db seed 走老 path 时不写也会用默认）
    base_image: str = Field(default="loongnix-build:23.2", min_length=1)


class TargetSystemUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    base_image: Optional[str] = Field(None, min_length=1)
    is_active: Optional[bool]


class TargetSystemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    system_id: int
    name: str
    base_image: str
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

# ==================== 编译任务 ====================

class CompileTaskCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    system_id: int
    software_name: str = Field(..., min_length=1, max_length=100)
    source_type: str = Field(..., pattern="^(upload|git)$")
    source_path: Optional[str] = None
    git_url: Optional[str] = None

    @field_validator("git_url")
    @classmethod
    def _git_url_scheme(cls, v):
        # 3f（2026-10-23）：仅 http(s)/git 协议。file://可 clone 宿主机任意 git 仓
        # （含他人任务源码），ssh://带宿主 key 可内网横探测；git:// 必须放行
        # （skaware 系源码 443 被墙时唯一通道，93/801346 实测）。
        import re as _re
        if v and not _re.match(r"^(https?|git)://", v):
            raise ValueError("git_url 仅支持 http(s):// 或 git:// 协议")
        return v

    @field_validator("source_path")
    @classmethod
    def _source_path_scope(cls, v):
        # 3a：source_path 只允许平台 artifacts 区（upload 落盘 sources/ 或任务工作区），
        # 防"提交任务指向宿主机任意路径"的读取通道。resolve 后判前缀（兼容符号链接目录）。
        if not v:
            return v
        import os as _os
        # 与引擎同口径：公共权威 config.artifacts_dir（config 不依赖 models/schemas，无环）
        try:
            from app.config import artifacts_dir as _artifacts_dir  # Q1b：公共权威，不再反向 import 引擎私有
            root = str(_artifacts_dir().resolve())
        except Exception:
            root = _os.path.realpath(_os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "..", "..", "artifacts"))
        rp = _os.path.realpath(v)
        try:
            ok = _os.path.commonpath([root, rp]) == root
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("source_path 仅允许指向平台 artifacts 目录内")
        return v
    git_branch: Optional[str] = "main"
    build_args: Optional[dict] = None
    compile_mode: CompileMode = CompileMode.CONTAINER


class CompileTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    task_id: str
    user_id: int
    system_id: int
    # 目标系统名（从 TargetSystem.name 拼进来，list/get 端点 join 后填）
    system_name: Optional[str] = None
    software_name: str
    source_type: str
    source_path: Optional[str]
    git_url: Optional[str]
    git_branch: Optional[str]
    compile_mode: CompileMode
    # status 用 str 而非 TaskStatus 枚举：支持 cancelled（用户主动取消）
    # 是合法状态但不在原枚举里，序列化用 str 避免 500
    status: str
    # matched_skills 列是 Text（JSON 字符串 list[str]，2026-10-20 起落 agent 实际读过的 SKILL.md 名单）
    # 声明成 dict 会在非空值时 Pydantic 校验 500——用 str，前端 JSON.parse
    matched_skills: Optional[str]
    depend_tree: Optional[dict]
    artifact_path: Optional[str]
    bundle_size: Optional[int] = None  # artifacts.tar.gz 真实字节数=下载传输量（非 ORM 列，路由注入）
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[int]
    updated_at: Optional[datetime]

    @field_serializer('created_at', 'updated_at', 'started_at', 'completed_at', check_fields=False)
    def _serialize_dt(self, v):
        """DB 写的是 datetime.utcnow()（naive UTC）；序列化时加 'Z' 后缀

        前端 new Date(...Z) 正确解析为 UTC，toLocaleString 自动转本地时区
        naive datetime（无 tzinfo）一律当 UTC 处理
        """
        if v is None:
            return None
        if v.tzinfo is None:
            return v.isoformat() + 'Z'
        return v.isoformat()



class CompileTaskListResponse(BaseModel):
    total: int
    tasks: List[CompileTaskResponse]


# ==================== 工单 ====================

class TicketCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    task_id: int
    source: str = Field(..., pattern="^(cloud|client)$")
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None  # 选填（用户可以留空，附 build.log 末 50 行兜底）
    include_log_tail: bool = True       # 描述空时是否附日志


class TicketMessageCreate(BaseModel):
    content: str = Field(..., min_length=1)


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ticket_id: int
    task_id: int
    user_id: int
    source: str
    status: TicketStatus
    title: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime]
    messages: List[dict]


# ==================== 经验 ====================

class ExperienceSubmit(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    task_id: int
    skill_name: str = Field(..., min_length=1, max_length=100)
    skill_type: str = Field(..., pattern="^(generic|specific)$")
    system_id: int
    skill_content: str = Field(..., min_length=1)


class ExperienceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    experience_id: int
    task_id: int
    user_id: int
    skill_name: str
    skill_type: str
    system_id: int
    status: ExperienceStatus
    submitted_at: datetime
    reviewed_at: Optional[datetime]
    # 来源工单（工单经验专用；用户经验为 None）
    ticket_id: Optional[int] = None
    # SKILL.md 内容（经验贡献核心字段，列表接口也返方便前端预览）
    # 体量：单条经验 markdown 通常 < 10KB，可接受
    skill_content: Optional[str] = None
    rejection_reason: Optional[str] = None


# ==================== Skill ====================

class SkillCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern="^(generic|specific)$")
    description: Optional[str]
    system_ids: List[int]
    # version 列 String(20)：schema 先拦超长，防 DB 层"值太长"500（同 SkillUpdate，2026-10-21）
    version: Optional[str] = Field(None, max_length=20)
    skill_content: str
    script_content: Optional[str] = None


class SkillUpdate(BaseModel):
    # 全部 = None：Pydantic v2 里 Optional[X] 无默认值=必填字段，
    # 前端编辑表单不带 is_active/部分字段 → 422 Field required → 用户端显示"网络错误"（2026-10-21 事故）
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    system_ids: Optional[List[int]] = None
    # version 列 String(20)：超长会在 DB 层炸 500"值太长了(20)"（2026-10-21 实测），schema 先拦成 422 可读错误
    version: Optional[str] = Field(None, max_length=20)
    is_active: Optional[bool] = None
    skill_content: Optional[str] = None


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    skill_id: int
    name: str
    type: str
    description: Optional[str]
    # system_ids 持久化时是 JSON 字符串（'[]' / '[1,2]'），model_validate 直接拿 ORM
    # 字段会是 str。接受 str/None/list，缺省空 list。
    system_ids: List[int] = []
    version: Optional[str]
    is_active: bool
    source: Optional[str] = "admin"  # admin | experience
    # 列表接口返这两个字段可能让响应变大，但前端 editSkill 需要填充编辑器；
    # 如果嫌大可以另开 /admin/skills/{id} 详情接口
    skill_content: Optional[str] = ""
    script_content: Optional[str] = ""
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("system_ids", mode="before")
    @classmethod
    def _parse_system_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [int(x) for x in parsed]
            except (json.JSONDecodeError, ValueError, TypeError):
                return []
            return []
        if isinstance(v, list):
            return [int(x) for x in v]
        return []


# ==================== 统计 ====================

class StatsOverview(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    success_rate: float
    total_tasks: int
    total_experiences: int
    active_users: int
    recent_success_rate_trend: List[dict]
    top_errors: List[dict]
    cluster_nodes: List[dict]
    skill_sync_summary: dict


# ==================== 认证相关 ====================

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: int
    username: str
    role: str
    is_active: bool
    last_active: Optional[datetime] = None


class LoginResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: TokenResponse
    user: UserOut


# ==================== 统一响应 ====================

class ApiResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: Any = None


class ErrorResponse(BaseModel):
    code: int
    message: str
    detail: Optional[str] = None
