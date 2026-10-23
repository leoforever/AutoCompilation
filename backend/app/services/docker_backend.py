"""Docker 容器后端 - 实现 deepagents SandboxBackendProtocol

原 compile-demo/langgraph_main.py:DockerBackend 整体迁移，逻辑无修改。
- execute 返回 ExecuteResponse
- ls/read/write/edit/grep/glob 返回对应 *Result dataclass
- _exec 用 `(cd ${work_dir} || true)` 兜底，避免 work_dir 不存在时报错
- download_files 批量读取（SkillsMiddleware 加载 SKILL.md 用）
- a* async 方法全部 NotImplementedError（同步后端）
"""
import os
import uuid
import time
import shlex
import socket as _socket

import docker

from deepagents.backends.protocol import (
    ExecuteResponse, SandboxBackendProtocol,
    LsResult, ReadResult, WriteResult, EditResult, DeleteResult,
    GlobResult, GrepResult, FileDownloadResponse,
)


# 模块级 Docker 客户端（单例）
_docker_client = None


def get_docker_client():
    """懒加载 Docker 客户端；连接失败返回 None 不抛错"""
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
        except Exception:
            _docker_client = None
    return _docker_client


class DockerBackend(SandboxBackendProtocol):
    """让 DeepAgent 在 Docker 容器内执行命令和操作文件"""

    def __init__(self, container_id: str, work_dir: str, exec_registry: "dict | None" = None):
        self.container_id = container_id
        self.work_dir = work_dir
        # T9-U 工单停止功能：活跃 exec 登记簿 {exec_id: {"pid","cmd","started"}}（session 注入，
        # 命令起止进出登记；stop 端点遍历击杀。None=不登记，编译引擎等路径零影响）
        self.exec_registry = exec_registry
        cli = get_docker_client()
        if cli is None:
            raise RuntimeError("Docker 客户端未连接")
        self.container = cli.containers.get(container_id)

    def _exec(self, command: str, timeout: int = None):
        """底层执行命令，返回 (output: str, exit_code: int)

        默认 timeout 取环境变量 DOCKER_EXEC_TIMEOUT，默认 3600s（1 小时），
        留给 ollama cmake --build 这种大项目充足时间。
        """
        if timeout is None:
            try:
                from app.config import get_cfg
                timeout = int(get_cfg("docker_exec.timeout", 3600))
            except (ValueError, TypeError):
                timeout = 3600
        # 容器内 coreutils timeout 包裹（timeout -k 60 N cmd：N 秒 SIGTERM、再 60 秒 SIGKILL）。
        # 教训 59CEF3（2026-10-22/23）：sock.settimeout 只能让"等待"结束，容器内进程照跑——
        # agent 自写坏 gcc 垫片死循环，两个进程烧 13.6h CPU，socket 超时分支也没触发。
        # 命令本体必须死在容器内，recv 流才会自然 EOF；外层 socket timeout 降为第二保险。
        full_cmd = f"(cd {self.work_dir} 2>/dev/null || true); timeout -k 60 {timeout} bash -c {shlex.quote(command)}"
        _reg = self.exec_registry is not None
        _tag = uuid.uuid4().hex[:12] if _reg else None
        try:
            exec_id = self.container.client.api.exec_create(
                self.container.id, ["bash", "-c", full_cmd],
                environment=[f"HBEXEC_TAG={_tag}"] if _reg else None,
            )["Id"]
            # 🔴 2026-10-23 根因修复（90 号工单"同一命令并发跑两次"悬案定罪于此）：
            # docker-py 7.x 的 exec_start(socket=True) 返回 SocketIO 包装（无 settimeout/recv），
            # 旧主路径每次于 sock.settimeout 抛 AttributeError → 落降级 exec_run 重跑同一条命令，
            # 弃用 exec 仍在容器跑完 = 双执行/双倍 CPU/交错写坏文件（sysdeps 事故真凶）。
            # 改用官方流式 API（stream+demux 生成器）；命令寿命由容器内 timeout 包裹保证，
            # 客户端不再持 socket 计时。
            if _reg:
                self.exec_registry[exec_id] = {"tag": _tag, "cmd": command[:120], "started": time.time()}
            try:
                parts = []
                for chunk in self.container.client.api.exec_start(
                    exec_id, stream=True, demux=True
                ):
                    out_c, err_c = chunk if isinstance(chunk, tuple) else (chunk, b"")
                    if out_c:
                        parts.append(out_c)
                    if err_c:
                        parts.append(err_c)
                output = b"".join(parts).decode("utf-8", errors="replace")
            finally:
                if _reg:
                    self.exec_registry.pop(exec_id, None)
            inspect = self.container.client.api.exec_inspect(exec_id)
            return output, inspect.get("ExitCode", 0)
        except Exception as e:
            # 降级路径（full_cmd 已含容器内 timeout，即便走 exec_run 也有硬超时保护）
            try:
                result = self.container.exec_run(["bash", "-c", full_cmd])
                raw = result.output
                output = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else (raw or "")
                return output, result.exit_code
            except Exception as e2:
                return f"Error: {type(e).__name__}: {e}; fallback: {type(e2).__name__}: {e2}", -1

    def kill_active_execs(self, sig: int = 15) -> int:
        """T9-U 停止功能：击杀登记簿内全部活跃命令（容器内进程组级）。

        实现依据（本日实验）：exec_inspect().Pid 在本环境恒 0，宿主侧击杀不可信；
        改为 _exec 注入 environment=HBEXEC_TAG=<唯一tag>，容器内遍历 /proc/*/environ
        匹配后 kill -SIG -<pgid>（进程组，timeout/bash/子命令一网打尽，59CEF3 孤儿形态
        即防）。pgid=1（容器 init）绝对排除。返回击杀的进程组数。"""
        if not self.exec_registry:
            return 0
        tags = [v.get("tag") for v in list(self.exec_registry.values()) if v.get("tag")]
        if not tags:
            return 0
        script = (
            'mypid=$$; mypg=$(awk \'{print $5}\' /proc/$mypid/stat 2>/dev/null); '
            'for t in ' + " ".join(tags) + '; do '
            'for d in /proc/[0-9]*/environ; do '
            'p=${d#/proc/}; p=${p%/environ}; [ "$p" = "$mypid" ] && continue; '
            'if tr "\\0" "\\n" < "$d" 2>/dev/null | grep -qx "HBEXEC_TAG=$t"; then '
            'g=$(awk \'{print $5}\' /proc/$p/stat 2>/dev/null); '
            '[ -n "$g" ] && [ "$g" != "$mypg" ] && [ "$g" != "1" ] && kill -' + str(sig) + ' -"$g" 2>/dev/null && echo "K $g"; fi; done; done'
        )
        try:
            out = self.container.exec_run(["sh", "-c", script])[1].decode(errors="replace")
            return out.count("K ")
        except Exception:
            return 0

    def execute(self, command: str, *, timeout: int | None = None, **kwargs) -> ExecuteResponse:
        # 默认 timeout 由 _exec 内部从环境变量读取（默认 3600s）
        # 调用方传 timeout=None 时直接传 None 给 _exec，让它自己读 env
        from app.config import get_cfg as _gcfg
        _eff = timeout or int(_gcfg("docker_exec.timeout", 3600))
        output, exit_code = self._exec(command, timeout=timeout)
        # 超时语义统一注释（124/137=容器内 coreutils timeout 杀；-1=socket 兜底）——
        # 归口在 execute()（工具结果构造点）：_exec 两条路径（主/降级）出口都经过这里，
        # 2026-10-23 验证发现只注主路径会漏（sock 超时抛的异常类型走降级分支）
        if exit_code in (124, 137, -1):
            output += (f"\n[硬性超时] 命令运行超过 {_eff}s 已被强制终止(SIGTERM→SIGKILL)。"
                       "若这是正常长任务：用 nohup 后台化+轮询 marker/日志（勿前台干等）；"
                       "否则检查命令是否在等输入/死循环——交互式验证必须非交互化。")
        return ExecuteResponse(
            output=output,
            exit_code=exit_code,
            truncated=(exit_code == -1),
        )

    def ls(self, path: str, **kwargs) -> "LsResult":
        rel = path.rstrip("/") or "/"
        output, exit_code = self._exec(
            f"find {shlex.quote(rel)} -maxdepth 1 -mindepth 1 "
            f"\\( -type f -o -type d \\) "
            f"-printf '%y|%s|%p|%TY-%Tm-%TdT%TH:%TM\\n' 2>/dev/null"
        )
        if exit_code != 0:
            return LsResult(error=output or f"ls failed: exit_code={exit_code}")
        entries = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) < 4:
                continue
            type_char, size, p, mtime = parts[0], parts[1], parts[2], parts[3]
            entries.append({
                "path": p,
                "is_dir": (type_char == "d"),
                "size": int(size) if size.isdigit() else 0,
                "modified_at": mtime,
            })
        return LsResult(entries=entries)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000, **kwargs) -> "ReadResult":
        wc_out, wc_code = self._exec(f"wc -l < {shlex.quote(file_path)} 2>/dev/null")
        total_lines = int(wc_out.strip()) if wc_code == 0 and wc_out.strip().isdigit() else None
        cmd = f"sed -n '{offset+1},{offset+limit}p' {shlex.quote(file_path)} 2>&1"
        output, exit_code = self._exec(cmd)
        if exit_code != 0:
            return ReadResult(error=output or f"read failed: exit_code={exit_code}")
        content = output
        if not content:
            line_count = 0
        elif content.endswith("\n"):
            line_count = content.count("\n")
        else:
            line_count = content.count("\n") + 1
        end_line = offset + line_count
        if total_lines is not None and end_line > total_lines:
            end_line = total_lines
        next_offset = end_line if (total_lines is None or end_line < total_lines) else None
        return ReadResult(
            file_data={"content": content, "encoding": "utf-8"},
            total_lines=total_lines,
            start_line=offset + 1,
            end_line=end_line,
            next_offset=next_offset,
        )

    def write(self, file_path: str, content: str, **kwargs) -> "WriteResult":
        import base64
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        rel_q = shlex.quote(file_path)
        dir_q = shlex.quote(os.path.dirname(file_path) or ".")
        cmd = f"mkdir -p {dir_q} && echo {b64} | base64 -d > {rel_q}"
        output, exit_code = self._exec(cmd)
        if exit_code != 0:
            return WriteResult(error=output or f"write failed: exit_code={exit_code}")
        return WriteResult(path=file_path)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False, **kwargs) -> "EditResult":
        rr = self.read(file_path, offset=0, limit=100000)
        if rr.error:
            return EditResult(error=rr.error)
        if not rr.file_data:
            return EditResult(error="read returned no content")
        content = rr.file_data["content"]
        if old_string not in content:
            return EditResult(error=f"old_string not found in {file_path}")
        if not replace_all and content.count(old_string) > 1:
            return EditResult(
                error=f"old_string appears {content.count(old_string)} times; use replace_all=True or be more specific"
            )
        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        occurrences = content.count(old_string) if replace_all else 1
        wr = self.write(file_path, new_content)
        if wr.error:
            return EditResult(error=wr.error)
        return EditResult(path=file_path, occurrences=occurrences)

    def delete(self, file_path: str, **kwargs) -> "DeleteResult":
        cmd = f"rm -rf {shlex.quote(file_path)}"
        output, exit_code = self._exec(cmd)
        if exit_code != 0:
            return DeleteResult(error=output or f"delete failed: exit_code={exit_code}")
        return DeleteResult(path=file_path)

    def glob(self, pattern: str, path: str = None, **kwargs) -> "GlobResult":
        if path is None:
            path = self.work_dir
        if "/" in pattern:
            name = pattern.rstrip("/").split("/")[-1]
        else:
            name = pattern
        find_name = name.replace("*", "")
        if find_name:
            find_pat = f"*{find_name}*"
        else:
            find_pat = "*"
        output, exit_code = self._exec(
            f"find {shlex.quote(path)} -name {shlex.quote(find_pat)} -type f 2>/dev/null | head -500"
        )
        if exit_code != 0:
            return GlobResult(error=output or "glob failed")
        matches = [{"path": p, "is_dir": False} for p in output.strip().split("\n") if p]
        return GlobResult(matches=matches)

    def grep(self, pattern: str, path: str = None, glob: str = None,
             max_count: int = None, **kwargs) -> "GrepResult":
        if path is None:
            path = self.work_dir
        glob_filter = f" --include={shlex.quote(glob)}" if glob else ""
        count_flag = f" -m {max_count}" if max_count else ""
        output, exit_code = self._exec(
            f"grep -rn{count_flag}{glob_filter} {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -100"
        )
        if exit_code not in (0, 1):
            return GrepResult(error=output or f"grep failed: exit_code={exit_code}")
        matches = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) >= 3:
                matches.append({
                    "path": parts[0],
                    "line": int(parts[1]) if parts[1].isdigit() else 0,
                    "text": parts[2],
                })
        return GrepResult(matches=matches)

    def download_files(self, paths):
        """批量读取文件，SkillsMiddleware 加载 SKILL.md 用"""
        responses = []
        for p in paths:
            rr = self.read(p, offset=0, limit=1000000)
            if rr.error:
                err = rr.error
                if 'No such file' in err or 'cannot access' in err or 'not found' in err.lower():
                    err = 'file_not_found'
                responses.append(FileDownloadResponse(path=p, content=None, error=err))
            else:
                content = rr.file_data.get('content', '') if rr.file_data else ''
                responses.append(FileDownloadResponse(path=p, content=content.encode('utf-8'), error=None))
        return responses

    # ===== async stubs =====（同步后端，async 全部禁用）
    async def adelete(self, *a, **kw): raise NotImplementedError
    async def adownload_files(self, *a, **kw): raise NotImplementedError
    async def aedit(self, *a, **kw): raise NotImplementedError
    async def aexecute(self, *a, **kw): raise NotImplementedError
    async def aglob(self, *a, **kw): raise NotImplementedError
    async def agrep(self, *a, **kw): raise NotImplementedError
    async def als(self, *a, **kw): raise NotImplementedError
    async def aread(self, *a, **kw): raise NotImplementedError
    async def aupload_files(self, *a, **kw): raise NotImplementedError
    async def awrite(self, *a, **kw): raise NotImplementedError
