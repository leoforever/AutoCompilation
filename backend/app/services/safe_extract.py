"""安全解包（批3 3b，2026-10-23；链接策略修订 2026-10-27）

统一三个解包点的防护（此前 stage4a 有简化版、admin resolve/stage6 是裸 extractall）：
  - 路径边界：commonpath 判定（startswith 可被 /dst-evil 兄弟目录名旁路）
  - 链接成员：tar 的 symlink/hardlink **只拒逃逸**（绝对 linkname 或规范化后落在
    解压目录外）。旧策略"一律拒绝"砍死了带 SONAME 版本链的正常产物
    （ollama：libllama.so→.so.0→.so.0.7.0 是动态链接刚需，见 388 归档事故）；
    攻击载荷本就是"链接指向目录外"（zip slip 符号链接变体），树内链接无越界能力
  - 设备/FIFO：全拒（无合法产物场景；zip 的 S_IFLNK 成员也全拒——zipfile 无法
    还原符号链接，遇链接成员会静默降级成含目标路径文本的普通文件，宁拒勿坏）
  - 数量/体积：zip bomb 双上限
校验与解包之间无 TOCTOU 容忍需求（压缩包内容不可信即整包拒绝，异常上抛）。
extractall(filter="data") 兜底层与这里的链接口径天然一致（CPython data filter
的 link 检查同为"绝对/越界拒绝、树内放行"）。
"""
import os
from pathlib import Path

MAX_FILES = 50_000
MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


def _inside(base: Path, target: Path) -> bool:
    """target 解析后必须在 base 目录内（commonpath 同根判定，堵兄弟目录前缀旁路）"""
    try:
        return os.path.commonpath([str(base), str(target)]) == str(base)
    except ValueError:      # 不同盘符/相对混合——视为越界
        return False


def _link_escapes(dst: Path, member_name: str, linkname: str) -> bool:
    """链接目标越界判定（纯字面规范化，不跟随已存在文件的真身——校验期与解包期
    文件系统状态无关）：绝对 linkname 即逃逸；相对 linkname 以成员所在目录为基准
    做 normpath 消解 ../，落在 dst 外即逃逸。ollama SONAME 链（同目录三级链）与
    树内互链全放行；`/etc/passwd`、`../../x` 载荷仍拒。"""
    if not linkname or linkname.startswith("/"):
        return True
    base = (dst / Path(member_name).parent)
    resolved = Path(os.path.normpath(str(base / linkname)))
    return not _inside(dst, resolved)


def safe_extract_tar(tar_path, dst_dir, *, max_files=MAX_FILES, max_bytes=MAX_BYTES,
                     on_progress=None):
    import tarfile
    dst = Path(dst_dir).resolve()
    members = 0
    total = 0
    with tarfile.open(tar_path) as tf:
        for m in tf:
            members += 1
            if members > max_files:
                raise RuntimeError(f"压缩包内文件数超过 {max_files}，疑似 zip bomb")
            if m.issym() or m.islnk():
                # 10-27 修订：一律拒绝→只拒逃逸（模块 docstring 有事故背景）
                if _link_escapes(dst, m.name, m.linkname):
                    raise RuntimeError(f"拒绝逃逸链接成员（zip slip 变体）：{m.name} -> {m.linkname}")
            if m.ischr() or m.isblk() or m.isfifo():
                raise RuntimeError(f"拒绝设备/FIFO 成员：{m.name}")
            target = (dst / m.name).resolve() if not m.name.startswith("/") else Path(m.name)
            if m.name.startswith("/") or not _inside(dst, target):
                raise RuntimeError(f"非法解压路径（zip slip?）：{m.name}")
            total += m.size or 0
            if total > max_bytes:
                raise RuntimeError(f"解压后总字节超过 {max_bytes // (1024**3)} GB")
            if on_progress and members % 200 == 0:
                on_progress(members)
        # 校验全部通过后才落盘（extractall 走 tarfile 的 data filter：再挡一层
        # 平台级特权/路径异常；其对 symlink/hardlink 成员跳过 chown（tarfile 3.11
        # 回背源码 _init_member），与树内链接放行策略兼容——本机带链 tar 实测）。
        # 10-27 修订：此前"拒绝一切链接"的预检会砍死 SONAME 版本链正常产物，
        # 预检改为只拒逃逸；data filter 的 link 检查口径与 _link_escapes 一致
        tf.extractall(dst, filter="data")
    return members


def safe_extract_zip(zip_path, dst_dir, *, max_files=MAX_FILES, max_bytes=MAX_BYTES,
                     on_progress=None):
    import zipfile
    dst = Path(dst_dir).resolve()
    members = 0
    total = 0
    import stat as _stat
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            members += 1
            if members > max_files:
                raise RuntimeError(f"压缩包内文件数超过 {max_files}，疑似 zip bomb")
            mode = info.external_attr >> 16
            if mode and _stat.S_ISLNK(mode):
                raise RuntimeError(f"拒绝 symlink 成员：{info.filename}")
            target = (dst / info.filename).resolve()
            if info.filename.startswith("/") or not _inside(dst, target):
                raise RuntimeError(f"非法解压路径（zip slip?）：{info.filename}")
            total += info.file_size or 0
            if total > max_bytes:
                raise RuntimeError(f"解压后总字节超过 {max_bytes // (1024**3)} GB")
            if on_progress and members % 200 == 0:
                on_progress(members)
        zf.extractall(dst)
    return members
