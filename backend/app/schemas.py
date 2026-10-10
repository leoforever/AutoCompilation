"""
Pydantic schemas for request/response validation
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Any
from datetime import datetime
from app.models import CompileMode, TaskStatus, TicketStatus, ExperienceStatus, UserRole


# ==================== 用户相关 ====================

class UserCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    role: UserRole = UserRole.USER


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
    arch: str = Field(..., pattern="^loongarch64$")
    base_image: str = Field(..., min_length=1)
    tag: Optional[str] = "latest"


class TargetSystemUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    base_image: Optional[str]
    tag: Optional[str]
    is_active: Optional[bool]


class TargetSystemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    system_id: int
    name: str
    arch: str
    base_image: str
    tag: Optional[str]
    is_active: bool
    created_at: Optional[datetime]


# ==================== 编译任务 ====================

class CompileTaskCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    system_id: int
    software_name: str = Field(..., min_length=1, max_length=100)
    source_type: str = Field(..., pattern="^(upload|git)$")
    source_path: Optional[str] = None
    git_url: Optional[str] = None
    git_branch: Optional[str] = "main"
    build_args: Optional[dict] = None
    compile_mode: CompileMode = CompileMode.CONTAINER


class CompileTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    task_id: str
    user_id: int
    system_id: int
    software_name: str
    source_type: str
    source_path: Optional[str]
    git_url: Optional[str]
    git_branch: Optional[str]
    compile_mode: CompileMode
    # status 用 str 而非 TaskStatus 枚举：支持 cancelled（用户主动取消）
    # 是合法状态但不在原枚举里，序列化用 str 避免 500
    status: str
    matched_skills: Optional[dict]
    depend_tree: Optional[dict]
    artifact_path: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[int]
    updated_at: Optional[datetime]


class CompileTaskListResponse(BaseModel):
    total: int
    tasks: List[CompileTaskResponse]


# ==================== 工单 ====================

class TicketCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    task_id: int
    source: str = Field(..., pattern="^(cloud|client)$")
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


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


# ==================== Skill ====================

class SkillCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern="^(generic|specific)$")
    description: Optional[str]
    system_ids: List[int]
    version: Optional[str]
    skill_content: str
    script_content: Optional[str] = None


class SkillUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str]
    system_ids: Optional[List[int]]
    version: Optional[str]
    is_active: Optional[bool]
    skill_content: Optional[str]


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    skill_id: int
    name: str
    type: str
    description: Optional[str]
    system_ids: List[int]
    version: Optional[str]
    is_active: bool
    sync_status: str
    created_at: datetime
    updated_at: datetime


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
