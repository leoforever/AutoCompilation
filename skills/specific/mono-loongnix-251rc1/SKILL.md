---
name: mono-loongnix-251rc1
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  mono 6.13.0 在龙芯 Loongnix 25.1-rc1 上的构建、deb 打包与验证记录。编译 mono、mono-runtime deb、mcs/csc、Loongnix 25.1-rc1、monolite 引导、BoringSSL Unknown target CPU、mcontext_t __fpregs 时读这条。
---

## 简要场景

- 软件：mono 6.13.0（源码树 `configure.ac` 中 `AC_INIT(mono,[6.13.0])`）
- 系统：Loongnix GNU/Linux 25（任务目标标记为 loongnix-25.1-rc1），`dpkg --print-architecture` 为 **loong64**，`uname -m` 为 loongarch64
- 工具链实测：gcc 14.2.0、glibc 2.41、GNU Make 4.4.1、autoconf 2.72、automake 1.17、libtool 2.5.4、cmake 3.31.6、Python 3.13.5
- 结果：编译成功，产物为 `mono-runtime_6.13.0-1_loong64.deb` 与 `mono-devel_6.13.0-1_loong64.deb`
- 关键背景：mono 上游已带 LoongArch64 后端（`mono/arch/loongarch64/`、`mono/mini/mini-loongarch64.c`），但本平台需要解决 monolite 引导、BoringSSL loongarch64 识别、glibc 2.41 的 `mcontext_t` 布局、Debian 风格 `sysconfdir`、解包权限限制等问题。

## 构建步骤

以下命令与路径来自本次 BUILD_NOTES 实测记录。

1. 安装编译依赖：

```bash
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  build-essential gcc g++ make autoconf automake libtool pkg-config python3 cmake \
  bison flex gettext git zlib1g-dev libglib2.0-dev libicu-dev ca-certificates curl unzip
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fakeroot
```

2. 拉取 `external/` 下 git submodule：

```bash
cd /work/src
git submodule update --init --depth 1 --jobs 8
```

本次记录显示 `external/corefx`、`external/binary-reference-assemblies`、`external/aspnetwebstack`、`external/xunit-binaries`、`external/boringssl`、`external/bdwgc`、`external/cecil`、`external/roslyn-binaries` 等 submodule 被拉取；最终 `git submodule status` 无 `-` 前缀。

3. 准备 monolite 引导包：

```bash
curl -L -o /tmp/monolite.tar.gz https://download.mono-project.com/monolite/monolite-linux-1A5E0066-58DC-428A-B21C-0AD6CDAE2789-latest.tar.gz
# 解包后目录名日志记录为 /tmp/monolite-linux-1A5E0066-58DC-428A-B21C-0AD6CDAE2789-latest
mkdir -p mcs/class/lib/monolite-linux
cp -a /tmp/monolite-linux-1A5E0066-58DC-428A-B21C-0AD6CDAE2789-latest \
      mcs/class/lib/monolite-linux/1A5E0066-58DC-428A-B21C-0AD6CDAE2789
```

目录名中的 GUID 必须等于 `configure.ac` 的 `MONO_CORLIB_VERSION`；本次源码树为 `1A5E0066-58DC-428A-B21C-0AD6CDAE2789`。

4. 打完下方“踩坑点”中的补丁后，生成构建脚本：

```bash
cd /work/src
NOCONFIGURE=1 ./autogen.sh
```

如果只改 `configure.ac`，本次记录还使用了单独重新生成 configure 的方式：

```bash
cd /work/src
autoconf
```

5. 使用 out-of-tree 目录 configure，并显式指定 `--sysconfdir=/etc`：

```bash
mkdir -p /work/build && cd /work/build
/work/src/configure --prefix=/usr --sysconfdir=/etc --disable-boehm \
    --with-csc=mcs --with-mcs-docs=no --disable-nls \
    --enable-btls --disable-llvm --disable-system-aot
```

本次 configure 摘要实测：

- `Host/Target: loongarch64-unknown-linux-gnu`
- `GC: sgen (concurrent by default)`
- `TLS: pthread`
- `Engine: Building and using the JIT`
- `LLVM Back End: no`
- `BTLS: yes (loongarch64)`
- `Windows AOT: no`

6. 编译与安装：

```bash
cd /work/build
make -j16
make install DESTDIR=/work/build/image
```

7. 打 deb：

```bash
bash /work/build/mkdebs.sh
```

本次记录中该脚本拆出 `mono-runtime` 与 `mono-devel` 两个包，并对 ELF 使用 `strip --strip-unneeded`、对 `.a` 使用 `strip --debug`、删除 `.mdb`，最终使用：

```bash
dpkg-deb --root-owner-group --build
```

8. 验证：

本次验证脚本为：

```bash
/work/verify/verify.sh
```

结果为：

```text
RESULT pass=28 fail=0
```

## 踩坑点

1. **loongnix 源没有 mono/dotnet 包，官方 monolite 引导 URL 是 x86-only**

- 现象：`make get-monolite-latest` 的 URL 是 x86-only 的 monolite；`apt-cache pkgnames | grep -i mono` 只有 `monopd/fonts-*` 等无关项。
- 根因：Loongnix 25 源中没有可直接用于引导 mono 构建的 mono/dotnet 包。
- 规避：手工下载 monolite 的 `mcs.exe + BCL dll` 包，并放到 `mcs/class/lib/monolite-linux/<MONO_CORLIB_VERSION>` 下。目录名 GUID 必须与 `configure.ac` 中 `MONO_CORLIB_VERSION` 一致，否则 `build.make` 的 `use_monolite` 探测不生效。

2. **glibc 2.41 的 `mcontext_t` 布局导致 runtime 编译失败**

错误原文：

```text
/work/src/mono/utils/mono-sigcontext.h:586:79: error: 'mcontext_t' has no member named '__fpregs'; did you mean '__gregs'?
mono-context.c:684: error: invalid use of undefined type 'union __loongarch_mc_fp_state'
```

- 根因：上游 LoongArch 代码按老 glibc（<2.36）的 `mcontext_t.__fpregs` 扁平成员写；Loongnix 25 的 glibc 2.41 已改成内核 extcontext 风格，`mcontext_t` 只有 `__pc/__gregs/__flags/__extcontext[]`，FP 状态挂在 `__extcontext` 块链表里。
- 实测：`sizeof(mcontext_t)=272`，`offsetof(__extcontext)=272`。
- 规避：
  - 删除 `mono-sigcontext.h` 中的 `UCONTEXT_FPREGS(ctx)` 宏。
  - 在 `mono-context.c` 新增 `loongarch_ucopy_fpregs()`，遍历 extcontext 块链表，按 magic 判定 FPU/LSX/LASX/LBT 的 stride，并取每寄存器低 64 位。
  - 将结果写入 `MonoContext.has_fregs`。
  - 使用 `#if defined(SC_USED_FP) && defined(FPU_CTX_MAGIC)` 兜底，老 glibc 上退化为“不取 FP 寄存器”，避免编不过。
  - `mono_monoctx_to_sigctx()` 保持只回写 GPR/PC/SP。

3. **mono 自带 BoringSSL fork 不认 loongarch64，BTLS 编不出来**

错误原文：

```text
include/openssl/base.h:109: error: "Unknown target CPU"
CMakeLists.txt:188: FATAL_ERROR "Unknown processor:loongarch64"
```

- 根因：mono 自带 boringssl fork 只枚举到 riscv64，没有 loongarch 分支，也没有 loongarch 汇编核。
- 规避（本次记录中的 3 处最小改动）：
  - `external/boringssl/include/openssl/base.h` 增加：

```c
#elif defined(__loongarch__) && defined(__loongarch_lp64)
```

并设置 `OPENSSL_64_BIT` 与 `OPENSSL_LOONGARCH64`。本次 `gcc -E` 实测该 gcc 只定义 `__loongarch_lp64` / `__loongarch__`，不定义 `__loongarch64__`。

  - `external/boringssl/CMakeLists.txt` 增加：

```cmake
elseif (CMAKE_SYSTEM_PROCESSOR STREQUAL "loongarch64")
  set(ARCH "loongarch64")
```

  - `configure.ac` 的 loongarch64 分支增加：

```bash
BTLS_CMAKE_ARGS="$BTLS_CMAKE_ARGS -DOPENSSL_NO_ASM=1"
```

该 fork 无 loongarch 汇编，走纯 C 通用路径；`OPENSSL_NO_ASM` 时 boringssl 将 ARCH 置 generic，同时避免 `base.h` 未知 arch 分支碰汇编。

- 结果：`libmono-btls-shared.so` 编出，导出 211 个 `mono_btls_*` icall；`System.dll` 带 `MONO_FEATURE_BTLS`；HTTPS 实测 200。

4. **默认 `sysconfdir` 导致 mono 内嵌 `/usr/etc`，找不到 machine.config**

错误原文：

```text
The assembly mscorlib.dll was not found ... /usr/lib/mono/4.5/
```

- 根因：第一轮 configure 未显式给 `--sysconfdir`，`configure.ac` 把 `sysconfdir=$prefix/etc` 写进 `mcs/build/config.make` 并 `-DMONO_CFG_DIR=...`；Debian 惯例需要显式 `/etc`。
- 规避：重新 configure 时加入：

```bash
--sysconfdir=/etc
```

然后增量 `make -j16` 与 `make install`。本次记录中增量编译耗时约 6 分钟。

5. **内核未放行 `fchmodat(AT_SYMLINK_NOFOLLOW)` / `fchmodat2`，`dpkg -x` / `tar` 解包失败**

错误原文：

```text
tar: ./usr/lib/mono/4.5/System.dll: Cannot change mode to rwxrwxrwx: Operation not permitted
dpkg-deb: error: tar subprocess returned error exit status 2
```

- 根因：本机内核未放行 `fchmodat(AT_SYMLINK_NOFOLLOW)` / `fchmodat2`；单条 `chmod` 正常，但 tar 恢复归档里的目录/dll 模式时逐个失败并中断解压。
- 规避：
  - 验证脚本统一改用 `fakeroot dpkg -x ...`。
  - 打包时使用 `dpkg-deb --root-owner-group`。
  - 自制 monolite 时显式 `chmod 644 *.dll`、`chmod 755 目录`。
  - 最终两个 deb 内无 0777 文件。

## 验证方法

平台验收只要求基本三件套，完整套件验证非必需。本次记录中最轻可复用的验证口径如下：

1. **干净环境装产物**

对交付的两个 deb 使用 `fakeroot dpkg -x` 解到独立目录，确认解包 rc=0、文件齐全：

```bash
fakeroot dpkg -x mono-runtime_6.13.0-1_loong64.deb /tmp/mono-run
fakeroot dpkg -x mono-devel_6.13.0-1_loong64.deb /tmp/mono-devel
```

本次记录中验证脚本采用 `fakeroot dpkg -x`，用于规避 `fchmodat` 权限问题。

2. **导入 / 加载**

使用产物中的 `mcs` / `csc` 编译最小 C# 程序，并确认能加载 `mscorlib.dll`。本次记录中曾出现 `/usr/lib/mono/4.5/` 下找不到 `mscorlib.dll` 的症状，根因是 `sysconfdir` 内嵌 `/usr/etc`；加入 `--sysconfdir=/etc` 后验证通过。

3. **一次最小调用**

执行验证脚本中最小的 mono 运行调用。本次记录最终结果为：

```text
RESULT pass=28 fail=0
```

完整套件验证非必需；上述“装产物 / 加载 / 最小调用”已覆盖本次成功交付的核心验收口径。