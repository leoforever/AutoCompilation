"""安全解包（批3 3b，2026-10-23）

统一两个解包点的防护（此前 stage4a 有简化版、admin resolve 拷贝是裸 extractall）：
  - 路径边界：commonpath 判定（startswith 可被 /dst-evil 兄弟目录名旁路）
  - 成员类型：拒 symlink/hardlink/字符设备/块设备/FIFO（tar 的链接成员可把写入
    "指向"目录外；zip 外部属性 S_IFLNK 同理）
  - 数量/体积：zip bomb 双上限
校验与解包之间无 TOCTOU 容忍需求（压缩包内容不可信即整包拒绝，异常上抛）。
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
                raise RuntimeError(f"拒绝链接成员（zip slip 变体）：{m.name}")
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
        # 平台级特权/路径异常；3.11 回背版已含）
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
