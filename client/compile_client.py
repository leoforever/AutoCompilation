#!/usr/bin/env python3
"""
LoongArch Compile Platform · Client
本地私有化编译客户端

功能：
1. Skill 同步（从云端拉取最新 Skill）
2. 本地编译（容器/本地两种模式）
3. 编译日志脱敏上报
"""
import argparse
import json
import os
import sys
import hashlib
import logging
from datetime import datetime
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# 配置
CONFIG_PATH = Path.home() / ".compile-platform" / "config.json"
SKILL_CACHE_DIR = Path.home() / ".compile-platform" / "skills"
LOG_DIR = Path.home() / ".compile-platform" / "logs"
ARTIFACT_DIR = Path.home() / ".compile-platform" / "artifacts"


def _git_clone(url: str, branch: str, dst: str) -> int:
    """T-8②：原 os.system(f"...{git_url}") 为宿主命令注入面（$()/反引号/分号）。
    改参数列表直传（不经 shell 无转义面）+ 协议白名单（与后端 schemas 同款规则，
    客户端独立部署不便 import 服务端代码，注明来源即可）。"""
    import re as _re
    import subprocess
    if not _re.match(r"^(https?|git)://", url or ""):
        raise ValueError(f"git_url 协议不允许（仅 http(s)/git）：{url!r}")
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [url, dst]
    return subprocess.run(cmd, capture_output=True, text=True).returncode


def load_config() -> dict:
    """加载客户端配置"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "server_url": "http://localhost:8000",
        "token": "",
        "auto_sync": True,
        "default_compile_mode": "local"
    }


def save_config(config: dict):
    """保存客户端配置"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)


def sync_skills(server_url: str, token: str):
    """从云端同步 Skill 库"""
    import requests
    
    logger.info("正在同步 Skill 库...")
    
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{server_url}/api/skills", headers=headers, timeout=30)
    
    if resp.status_code != 200:
        logger.error(f"同步失败：{resp.text}")
        return
    
    skills = resp.json().get('data', [])
    SKILL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    synced_count = 0
    for skill in skills:
        if not skill.get('is_active'):
            continue
        
        skill_name = skill['name']
        skill_dir = SKILL_CACHE_DIR / skill_name
        
        # 创建 Skill 目录结构
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill.get('skill_content', ''))
        
        if skill.get('script_content'):
            script_dir = skill_dir / "scripts"
            script_dir.mkdir(exist_ok=True)
            (script_dir / "run.sh").write_text(skill['script_content'])
            (script_dir / "run.sh").chmod(0o755)
        
        synced_count += 1
    
    logger.info(f"Skill 同步完成，共同步 {synced_count} 条")
    return synced_count


def compile_local(software_name: str, source_path: str = None, 
                  git_url: str = None, git_branch: str = "main",
                  compile_mode: str = "local", build_args: dict = None):
    """
    本地编译
    
    Args:
        software_name: 软件名称
        source_path: 源码路径（上传模式）
        git_url: Git 仓库地址（Git 模式）
        git_branch: Git 分支
        compile_mode: 编译模式（container/local）
        build_args: 构建参数
    """
    logger.info(f"开始编译 {software_name}...")
    
    # 创建编译工作目录
    work_dir = Path.home() / ".compile-platform" / "work" / datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir.mkdir(parents=True, exist_ok=True)
    
    # 下载或解压源码
    if source_path:
        logger.info(f"从本地路径加载源码：{source_path}")
        # TODO: 实现源码解压逻辑
    elif git_url:
        logger.info(f"克隆源码：{git_url} (branch: {git_branch})")
        _git_clone(git_url, git_branch, str(work_dir / "src"))
    
    # 匹配本地 Skill
    logger.info("匹配本地 Skill...")
    # TODO: 实现 Skill 匹配逻辑
    
    # 执行编译
    logger.info("执行编译...")
    # TODO: 实现编译逻辑
    
    logger.info("编译完成")
    return {
        "status": "success",
        "work_dir": str(work_dir),
        "artifact_dir": str(ARTIFACT_DIR)
    }


def report_ticket(task_id: int, error_log: str, source: str = "client"):
    """上报编译失败工单"""
    import requests
    # T-8①（2026-10-26）：原 from config import load_config 必抛 ImportError——
    # load_config 就在本模块，直接调用即可，否则 report 路径一用即崩
    config = load_config()
    headers = {"Authorization": f"Bearer {config['token']}"}
    
    # 脱敏日志
    sanitized_log = sanitize_log(error_log)
    
    resp = requests.post(
        f"{config['server_url']}/api/tickets/report",
        headers=headers,
        json={
            "task_id": task_id,
            "source": source,
            "title": f"编译失败：{task_id}",
            "description": sanitized_log
        }
    )
    
    if resp.status_code == 200:
        ticket_id = resp.json().get('data', {}).get('ticket_id')
        logger.info(f"工单已提交：#{ticket_id}")
        return ticket_id
    else:
        logger.error(f"工单提交失败：{resp.text}")
        return None


def sanitize_log(log: str) -> str:
    """脱敏日志"""
    # 移除可能的敏感信息（IP、路径、密钥等）
    import re
    
    # 移除 IP 地址
    log = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[REDACTED_IP]', log)
    
    # 移除文件路径
    log = re.sub(r'/[\w/.-]+', '[REDACTED_PATH]', log)
    
    # 移除 API key
    log = re.sub(r'api[_-]?key["\s:=]+[\w-]{20,}', 'api_key=[REDACTED]', log, flags=re.IGNORECASE)
    
    return log


def main():
    parser = argparse.ArgumentParser(description="龙芯 LoongArch 智能编译平台客户端")
    parser.add_argument("command", choices=["compile", "sync", "config"], help="命令")
    parser.add_argument("--software", "-s", help="软件名称")
    parser.add_argument("--source", help="源码路径")
    parser.add_argument("--git", help="Git 仓库地址")
    parser.add_argument("--branch", default="main", help="Git 分支")
    parser.add_argument("--mode", choices=["container", "local"], default="local", help="编译模式")
    parser.add_argument("--report", help="上报工单（指定任务 ID）")
    
    args = parser.parse_args()
    
    config = load_config()
    
    if args.command == "sync":
        sync_skills(config['server_url'], config['token'])
    elif args.command == "compile":
        if not args.software:
            logger.error("请指定软件名称 (--software)")
            sys.exit(1)
        
        result = compile_local(
            software_name=args.software,
            source_path=args.source,
            git_url=args.git,
            git_branch=args.branch,
            compile_mode=args.mode
        )
        
        # 检查是否需要上报
        if result['status'] == 'failed' and args.report:
            report_ticket(args.report, result.get('error_log', ''))
    elif args.command == "config":
        print(json.dumps(config, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
