"""操作审计唯一写口（T10，2026-10-27 设计 →references/audit-log-design-20261027.md）

规则（对齐 task_writer"单一权威写口"先例）：
- IP：X-Real-IP / X-Forwarded-For 首段 → request.client.host 兜底
  （nginx 生产链路下直取 client.host 会全是 127.0.0.1，conf 已透传真实 IP）
- 时间：显式 naive UTC（全链时区纪律）
- details：2KB 截断 + 凭据值脱敏（password/secret/token/key= 样式替换 [REDACTED]）
- best-effort：内部吞错只 warn——审计失败绝不阻断业务；不自行 commit，
  挂调用方事务（业务成=审计存，业务回滚=审计同灭，原子）
- action 命名 `对象_动作` snake_case；存量 admin 10 处历史名不动（stats 消费）
"""
import re
from datetime import datetime

# 凭据样式值：key=value / key":"value 等，值替 [REDACTED]（键名也一并糊掉，连字段名都不留）
_CRED_RE = re.compile(
    r'(?i)(["\']?(?:password|passwd|secret|token|access_token|refresh_token|api[_-]?key|jwt)["\']?\s*[=:]\s*)(["\']?)([^"\'\s,;}{]+)(\2)'
)
# 裸词兜底：出现这些字面量整段替换（防 "旧密码=abc" 中文格式漏网）
_CRED_LOOSE = re.compile(r'(?i)(旧密码|新密码|口令|密码)\s*[=:：]?\s*\S+')
# URL 内嵌凭据：scheme://user:***@host —— userinfo 口令段糊掉（T16 问答注入面实抓：
# 编译日志常见 clone URL 带 token；键值正则救不了这种形态）
_URL_USERINFO = re.compile(r'://([^/\s:@]+):([^/\s@]+)@')

MAX_DETAILS = 2048


def sanitize_details(text: str) -> str:
    t = _CRED_RE.sub(r'\1\2[REDACTED]\4', str(text))
    t = _CRED_LOOSE.sub(r'\1=[REDACTED]', t)
    t = _URL_USERINFO.sub(r'://\1:[REDACTED]@', t)
    return t[:MAX_DETAILS]


def client_ip(request) -> str | None:
    if request is None:
        return None
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        rip = request.headers.get("x-real-ip")
        if rip:
            return rip.strip()
        return request.client.host if request.client else None
    except Exception:
        return None


def log_audit(db, *, user_id: "int | None" = None, action: str,
              target_type: "str | None" = None,
              target_id: "int | None" = None, target_key: "str | None" = None,
              details: str = "", request=None) -> None:
    """写一行审计（best-effort）。调用方保持自身 commit；此处只 add。"""
    try:
        from app.models import AuditLog
        db.add(AuditLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_key=str(target_key)[:100] if target_key is not None else None,
            details=sanitize_details(details) if details else None,
            ip_address=client_ip(request),
            created_at=datetime.utcnow(),   # naive UTC 纪律，不依赖列 default 混源
        ))
    except Exception as e:
        # 审计写失败不波及业务（登录不该因审计表炸而登不上）
        import logging
        logging.getLogger(__name__).warning("[audit] 写入失败 action=%s: %s", action, e)
