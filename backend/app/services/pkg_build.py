# -*- coding: utf-8 -*-
"""兜底产系统包（交付矩阵 11-17 二期）：stage6 归档后，AI 未出自格式包时平台补产。

口径（拍板在案）：默认开 / 前缀听上游 / 元数据不足不硬造 / 只加文件不动原始树。
执行面=宿主机（实测 dpkg-deb、ar、rpmbuild、cpio 宿主全有；构建镜像内缺不影响）。
失败语义=返回 (None, 原因) 只记日志，主流程永不因产包失败伤交付。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 上游包名映射（deb Depends 用；未命中不猜包名——只进 Description 提示）
_DEB_LIB_MAP = {
    "libssl.so.3": "libssl3", "libcrypto.so.3": "libssl3",
    "liblua5.4.so.0": "liblua5.4-0", "liblua5.1.so.5": "liblua5.1-5",
    "libpcap.so.1": "libpcap0.8", "libz.so.1": "zlib1g",
    "libncurses.so.6": "libncurses6", "libtinfo.so.6": "libtinfo6",
    "libstdc++.so.6": "libstdc++6", "libgcc_s.so.1": "libgcc-s1",
    "libpcre2-8.so.0": "libpcre2-8-0", "libssh2.so.1": "libssh2-1.2",
}


def needs_fallback_pack(output_dir: Path, fmt: str) -> bool:
    """触发判定：已登记目标包格式 ∧ output 是散装 install 树（顶层 bin/ 或 lib/）
    ∧ 树里没现成 .deb/.rpm 成品（AI 已自产就不越俎）。"""
    if fmt not in ("deb", "rpm"):
        return False
    out = Path(output_dir)
    if not out.is_dir():
        return False
    if any(p.suffix in (".deb", ".rpm") for p in out.rglob("*") if p.is_file()):
        return False
    return (out / "bin").is_dir() or (out / "lib").is_dir()


def _arch_of() -> str:
    u = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
    return {"loongarch64": "loong64", "x86_64": "amd64", "aarch64": "arm64"}.get(u, u)


def _rpm_arch_of() -> str:
    return subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip() or "loongarch64"


def pack_tree(output_dir: Path, fmt: str, name: str, version: str,
              runtime_libs=None, description: str = "") -> tuple:
    """入口分发。返回 (产物Path|None, 说明)。name/version 缺一即拒（不硬造）。"""
    if not name or not version or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", version or ""):
        return None, f"元数据不足（name={name!r} version={version!r}），不产{fmt}包"
    try:
        if fmt == "deb":
            return _build_deb(Path(output_dir), name, version, runtime_libs or [], description)
        if fmt == "rpm":
            return _build_rpm(Path(output_dir), name, version, description)
    except Exception as e:
        return None, f"产 {fmt} 包异常: {type(e).__name__}: {str(e)[:120]}"
    return None, f"未知格式 {fmt}"


def _tree_files(out: Path):
    """散装树内容清单（排除文档伴生物），deb/rpm 共用。"""
    skip_top = {"BUILD_NOTES.md", "RUN.md", "PACKAGE.json", "LICENSE", "README"}
    return [p for p in sorted(out.iterdir()) if p.name not in skip_top and not p.name.endswith(".meta.json")]


def _build_deb(out: Path, name: str, version: str, runtime_libs, description) -> tuple:
    """deb=ar 三成员：debian-binary + control.tar.gz + data.tar.gz（宿主 dpkg-deb 造，
    自算 Depends 映射 + 未命中只写描述）。"""
    libs = [l for l in runtime_libs if isinstance(l, str)]
    pkgs = sorted({_DEB_LIB_MAP[l] for l in libs if l in _DEB_LIB_MAP})
    unknown = [l for l in libs if l not in _DEB_LIB_MAP]
    desc = (description or f"{name} {version} (built by compile-platform)")[:200]
    if unknown:
        desc += "\n . " + "需运行库: " + " ".join(unknown)[:150]
    ctl = ("Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: "
           "compile-platform <noreply@localhost>\nPriority: optional\n"
           "Depends: %s\nDescription: %s\n %s\n"
           % (name, version, _arch_of(), ", ".join(pkgs), name, desc))
    with tempfile.TemporaryDirectory(prefix="pkgdeb-") as td:
        tdp = Path(td)
        (tdp / "debian-binary").write_text("2.0\n")
        (tdp / "control").write_text(ctl)
        data = tdp / "data"
        data.mkdir()
        for p in _tree_files(out):  # 前缀听上游：树即内容，装到 /（平台树本就在 output 下）
            dst = data / p.name
            if p.is_dir():
                shutil.copytree(p, dst, symlinks=True)
            else:
                shutil.copy2(p, dst)
        # control.tar.gz 只含 control（debian-binary 是 ar 顶层成员，不进 control 包）；
        # data.tar.gz 成员 ./ 前缀是 dpkg 规范，-C dir . 天然满足
        subprocess.run(["tar", "-czf", str(tdp / "control.tar.gz"), "-C", td, "./control"],
                       check=True, capture_output=True)
        subprocess.run(["tar", "-czf", str(tdp / "data.tar.gz"), "-C", str(data), "."],
                       check=True, capture_output=True)
        deb = out / f"{name}_{version}_{_arch_of()}.deb"
        if deb.exists():
            deb.unlink()
        subprocess.run(["ar", "qc", str(deb), "debian-binary", "control.tar.gz", "data.tar.gz"],
                       cwd=td, check=True, capture_output=True)
        # 自验：dpkg-deb 能解析才算好包（宁缺勿造）
        v = subprocess.run(["dpkg-deb", "-I", str(deb)], capture_output=True, text=True)
        if v.returncode != 0:
            deb.unlink(missing_ok=True)
            return None, "dpkg-deb -I 校验未过，弃产"
        return deb, f"deb 已产（Depends: {', '.join(pkgs) or '无'}）"


def _build_rpm(out: Path, name: str, version: str, description) -> tuple:
    """rpm 用 rpmbuild 真构建（宿主实测有）；%files 逐顶层项，前缀=上游默认（装在 /）。"""
    if not shutil.which("rpmbuild"):
        return None, "宿主无 rpmbuild，弃产 rpm"
    with tempfile.TemporaryDirectory(prefix="pkgrpm-") as td:
        tdp = Path(td)
        for d in ("BUILD", "RPMS", "SOURCES", "SPEC", "SRPMS", "BUILDROOT"):
            (tdp / d).mkdir()
        buildroot = tdp / "BUILDROOT" / f"{name}-{version}-1.{_rpm_arch_of()}"
        buildroot.mkdir()
        for p in _tree_files(out):
            dst = buildroot / p.name
            if p.is_dir():
                shutil.copytree(p, dst, symlinks=True)
            else:
                shutil.copy2(p, dst)
        filelist = "\n".join("/" + p.name for p in _tree_files(out))
        spec = tdp / "SPEC" / f"{name}.spec"
        spec.write_text(
            "Name: %s\nVersion: %s\nRelease: 1\nSummary: %s\n"
            "License: TBD\nBuildArch: %s\n%%description\n%s\n%%files\n%s\n"
            % (name, version, (description or name)[:80].replace("\n", " "),
               _rpm_arch_of(), (description or f"{name} built by compile-platform")[:200].replace("\n", " "),
               filelist), encoding="utf-8")
        r = subprocess.run(["rpmbuild", "-bb", "--noclean", "-v",
                            "--define", f"_topdir {tdp}", str(spec)],
                           capture_output=True, text=True, timeout=600)
        cand = list((tdp / "RPMS").rglob(f"{name}-*.rpm"))
        if not cand:
            return None, f"rpmbuild 未出包: {r.stdout[-160:] or r.stderr[-160:]}"
        rpm_path = out / cand[0].name
        shutil.move(str(cand[0]), rpm_path)
        return rpm_path, "rpm 已产"
