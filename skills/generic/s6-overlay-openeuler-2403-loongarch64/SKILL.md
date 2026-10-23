---
name: s6-overlay-openeuler-2403-loongarch64
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  s6-overlay 容器 init 包在龙芯 openEuler-24.03 LTS 上的构建步骤与验收口径。编译 s6-overlay、装 musl 交叉工具链、拉 skaware 源码、生成 overlay tar.xz、遇到 skarnet 443 不通或 linux/kd.h 缺失时读这条。
---

## 简要场景

- 软件：s6-overlay 3.2.3.2，源码仓库为 `https://github.com/just-containers/s6-overlay.git`；本次现场为 `/work/src`，BUILD_NOTES 记录为 git master。
- 目标系统：openEuler 24.03 LTS，容器内 root，loongarch64。
- 结果：编译成功，产出 5 个 `.tar.xz` overlay 包：
  - `s6-overlay-loongarch64.tar.xz`
  - `s6-overlay-noarch.tar.xz`
  - `s6-overlay-symlinks-arch.tar.xz`
  - `s6-overlay-symlinks-noarch.tar.xz`
  - `syslogd-overlay-noarch.tar.xz`
- 验收：三件套 22/22 全绿，`pass=22 fail=0`。

## 构建步骤

1. 安装 openEuler 原生 musl 工具链包。容器内无 `wget`，上游默认 `DL_CMD := wget` 不可直接用：

```bash
dnf -y install musl-gcc musl-devel musl-libc musl-libc-static
```

本次安装到 musl `1.2.4-6.oe2403sp4`，提供：

```text
/usr/bin/musl-gcc
/usr/bin/loongarch64-linux-musl-gcc
/usr/musl/include
/usr/musl/lib
```

2. 获取源码。现场探测 `https://skarnet.org:443` 不通，但 `git://git.skarnet.org:9418` 可通，因此 skaware 源码走 `git://`：

```bash
git clone git://git.skarnet.org/{skalibs,execline,s6,s6-rc,s6-linux-init,s6-portable-utils,s6-linux-utils,s6-dns,s6-networking}
git clone https://github.com/just-containers/s6-overlay-helpers
git clone https://www.bearssl.org/git/BearSSL
```

按上游 `conf/versions` 钉住的 revision 逐个确认存在，例如：

```bash
git rev-parse --verify <revision>
```

3. 造本地 loongarch64 musl 工具链目录。上游文档示例依赖 skarnet 自动下载交叉工具链，但本环境 skarnet 443 不通，且 `conf/toolchains` 里没有 loongarch64，因此用 `TOOLCHAIN_PATH` 指向本地工具链：

```bash
mkdir -p /work/build/toolchain/bin
cd /work/build/toolchain/bin
ln -sf /usr/bin/musl-gcc loongarch64-linux-musl-gcc
ln -sf /usr/bin/loongarch64-linux-musl-gcc loongarch64-linux-musl-cc
ln -sf /usr/bin/ar loongarch64-linux-musl-ar
ln -sf /usr/bin/ranlib loongarch64-linux-musl-ranlib
ln -sf /usr/bin/strip loongarch64-linux-musl-strip
```

4. 为 musl 构建补内核头 shim。openEuler 的 `musl-devel` 只装 `/usr/musl/include`，而 musl specs 会屏蔽 glibc 侧 `/usr/include/linux`，导致 `linux/kd.h` 找不到。现场做法是创建：

```text
/work/build/kheaders/linux
/work/build/kheaders/asm
/work/build/kheaders/asm-generic
```

并软链到 kernel-headers 的同名目录，然后导出：

```bash
export CPPFLAGS=-I/work/build/kheaders
```

5. 使用上游支持的命令行变量覆盖，不改源码树。最终可复现构建命令：

```bash
export CPPFLAGS=-I/work/build/kheaders
make -j1 ARCH=loongarch64-linux-musl TOOLCHAIN_PATH=/work/build/toolchain \
     OUTPUT=/work/build/out DL_CMD="curl -LO" VERSION=3.2.3.2 all
```

必须 `-j1`。现场 `-j4` 会触发 skaware prepare 阶段的并行竞态。

6. 归档产物：

```bash
cp -a /work/build/out/*.tar.xz /work/output/
```

并生成 `.sha256`。

## 踩坑点

1. skarnet.org HTTPS 不通

原日志关键错误：

```text
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to skarnet.org:443
```

现场根因：容器出网白名单不含 skarnet.org 的 443，但 `git://git.skarnet.org:9418` 可通。

规避：

- skaware 源码改走 `git://git.skarnet.org`。
- 工具链放弃上游下载，用 openEuler 原生 musl 包自造本地工具链。
- 构建时传 `TOOLCHAIN_PATH=/work/build/toolchain`。

2. `make -j4` 并行竞态

原日志关键错误：

```text
rm: cannot remove '.../skalibs-v2.15.1.0/src/include/skalibs': Directory not empty
make: *** [mk/skaware.mk:92: .../Makefile] Error 1
No rule to make target 'src/include/skalibs/gccattributes.h'
```

现场根因：`mk/skaware.mk` 的 prepare 规则会对同一 `build-$(ARCH)` 目录树执行 `rm -rf`、`cp -a`、`git checkout`，多包并行时互相踩。

规避：

```bash
make -j1 ...
```

本次全量 `-j1` 构建约 120 秒，无需并行。

3. musl 构建缺 `linux/kd.h`

原日志关键错误：

```text
src/init/s6-linux-init.c:14:10: fatal error: linux/kd.h: No such file or directory
```

现场根因：openEuler `musl-devel` 只安装 `/usr/musl/include`，且 musl-gcc specs 为 `-nostdinc -isystem /usr/musl/include`，glibc 侧 `/usr/include/linux` 被屏蔽。

规避：

```bash
export CPPFLAGS=-I/work/build/kheaders
```

其中 `/work/build/kheaders` 下提供 `linux`、`asm`、`asm-generic` 的软链。

4. 删 `config.mak` 强制重配后首次 make 报 configure 缺失

原日志关键错误：

```text
Makefile:178: *** Please use ./configure first. Stop.
```

现场根因：构建/验证命令被并发执行两次，两个 make 同时写同一 OUTPUT。

规避：

- 构建脚本加互斥，或使用独立 `run-$$` 目录。
- 清 `build` / `staging` 后做一次干净全量重跑。

5. skaware clone 时 git 打印 ref backend BUG

原日志关键信息：

```text
BUG: refs/files-backend.c:3188 ... dumped core
```

现场判断：仓库内容完整、revision 可 checkout，是 loongarch64 上 git 2.54 的 ref 写入问题，可忽略。

6. 验收脚本里的 execline 管道写法错误

错误现象：

```bash
execlineb -c 's6-echo b a | s6-sort'
```

实际只是把 `|` 当参数传给 `s6-echo`，输出：

```text
b a | s6-sort
```

正确写法：

```bash
pipeline { s6-echo -n "b\na\n" } s6-sort
```

第二个命令不能再套花括号，否则报：

```text
pipeline: fatal: unable to exec  s6-sort
```

7. 验收脚本里的 `pkill -f "s6-svscan"` 会误杀发起命令的 shell

现象：前台命令挂死到硬闸。

规避：改用 `pgrep -x` 或精确 PID 收尾。

8. chroot 内 execline 找不到命令

原因：execlineb 按 PATH 找命令，chroot 继承宿主 PATH，不含 `/command`。

规避：显式设置容器内 PATH：

```bash
chroot $R /command/s6-env PATH=/command:/usr/bin:/bin /command/s6-svscan -t 3000 /tmp/vscan
```

9. `s6-pause` 没有时长参数

错误写法：

```bash
s6-pause 120
```

服务脚本应写：

```bash
s6-pause
```

10. `s6-rc-compile` / `s6-linux-init-maker` 要求输出目录不存在

重复跑会报：

```text
File exists
```

规避：每次使用新的输出目录。

11. `s6-linux-init-maker` 需要 overlay stage0 实际参数

现场可用参数形态：

```bash
s6-linux-init-maker -NC -D top -f <包内 skel> <outdir>
```

自造 skel 缺 `runlevel` 子目录会报：

```text
unable to copy .../runlevel
```

## 验证方法

验收口径为基本三件套：装得上 / 导入得动 / 一次最小真实调用。完整测试套件、CI matrix、全量 `conf-tests` 非必需。

1. 干净环境安装产物

在空 rootfs 中依次解包 5 个 tarball：

```text
s6-overlay-loongarch64.tar.xz
s6-overlay-noarch.tar.xz
s6-overlay-symlinks-arch.tar.xz
s6-overlay-symlinks-noarch.tar.xz
syslogd-overlay-noarch.tar.xz
```

检查二进制包：

```bash
file -L /command/s6-svscan
ldd /command/s6-svscan
```

预期：

```text
ELF 64-bit LSB executable, LoongArch, version 1 (SYSV), statically linked, stripped
not a dynamic executable
```

检查 init：

```bash
head -1 /init
wc -l /init
```

预期：

```text
#!/bin/sh -e
49
```

2. 导入/加载

在 chroot 内执行：

```bash
execlineb -c 's6-echo hello-loongarch64'
```

预期输出：

```text
hello-loongarch64
```

3. 一次最小真实调用

在 chroot 内、`PATH=/command:/usr/bin:/bin` 下执行：

```bash
s6-expr 2 + 40
pipeline { s6-echo -n "b\na\n" } s6-sort
pipeline { s6-echo -n "hello\n" } s6-cat
pipeline { s6-echo -n "@400000005f00000000000000\n" } s6-tai64nlocal
s6-rc-compile /tmp/compiled /tmp/sc.d
```

预期：

```text
42
a
hello
2020-07-04 04:04:43.000000000
s6-rc-compile 成功
```

监督生命周期最小检查：

```bash
chroot $R /command/s6-env PATH=/command:/usr/bin:/bin /command/s6-svscan -t 3000 /tmp/vscan
```

现场结果：

```text
PASS svscan lifecycle
=== RESULT pass=22 fail=0
```