---
name: lsx-missing-flag-v2
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  lsx-missing-flag-v2（loongarch-simd-demo）C 语言 LSX/LASX intrinsics 演示程序：龙芯 openEuler-24.03 LTS 构建记录（实际探测为 Loongnix OS）。编译 loongarch-simd-demo、configure Unsupported architecture: loongarch64、__m256i、-mlasx 时读这条。
---

## 简要场景

- 软件：loongarch-simd-demo，任务名 `lsx-missing-flag-v2`，C 语言 LSX/LASX intrinsics 演示程序。
- 目标系统：openEuler 24.03 LTS；实际环境探测为 Loongnix OS，`uname -a` 显示 `Linux 6.6.0-117.0.0.122.loongarch64`，`uname -m` 为 `loongarch64`。
- 编译器：系统原生 `gcc (GCC) 12.3.0 20230508 (Loongnix OS 12.3.0-10)`，无需交叉工具链。
- 结果：编译成功，产物为 `/work/output/lsxdemo`、`/work/output/verify.sh`、`/work/output/config.mk.loongarch64`；三件套验收 `RESULT pass=7 fail=0`。

## 构建步骤

1. 读源码并探测环境：
   - 源码目录：`/work/src`，包含 `configure`、`Makefile`、`src/{simd,lasx,main}.{c,h}`。
   - 探测命令包括：
     - `uname -a`
     - `gcc --version`
     - `gcc -Q --help=target`
     - `gcc -dM -E -`
     - 查看 `/proc/cpuinfo` 的 SIMD flags
   - 实测默认 ISA 为 `-march=la64v1.0`，预定义宏含：
     - `__loongarch_sx 1`
     - `__loongarch_simd 1`
     - `__loongarch_simd_width 128`
   - `/proc/cpuinfo` flags 含 `lsx`、`lasx`。
   - intrinsics 头文件位于：
     - `/usr/lib/gcc/loongarch64-loongnix-linux/12/include/lsxintrin.h`
     - `/usr/lib/gcc/loongarch64-loongnix-linux/12/include/lasxintrin.h`

2. 首次执行 `./configure`，命中架构支持缺陷：
   ```text
   configure: error: Unsupported architecture: loongarch64
            (arch support table has no entry for this machine)
   ```

3. 修改 `configure` 的 `case "$ARCH"`，新增 `loongarch64)` 分支。先不要改 `*)` 兜底分支，避免影响其他架构的报错行为。

4. 单文件定位 SIMD 标记缺失：
   - `gcc -O2 -Wall -mlsx -c src/simd.c` 可通过。
   - `gcc -O2 -Wall -mlsx -c src/lasx.c` 失败，说明缺的是 LASX 相关标记。

5. 将 `configure` 中 `loongarch64` 分支设置为同时声明 LSX 与 LASX：
   ```sh
   ARCH_CFLAGS="-mlsx -mlasx"
   ```
   然后重新执行：
   ```sh
   ./configure
   ```
   生成 `config.mk`，内容片段为：
   ```text
   ARCH_CFLAGS = -mlsx -mlasx
   ```

6. 执行构建：
   ```sh
   make clean && make
   ```
   编译通过并链接生成 `lsxdemo`，日志未出现 warning。

7. 运行程序：
   ```sh
   ./lsxdemo
   ```
   输出：
   ```text
   LSX add check: PASS
   LASX add check: PASS
   ```
   退出码为 `0`。

8. 归档产物：
   ```sh
   cp -a lsxdemo /work/output/
   cp -a config.mk /work/output/config.mk.loongarch64
   ```

9. 执行验收脚本：
   ```sh
   sh /work/output/verify.sh /work/output/lsxdemo
   ```

## 踩坑点

1. `configure` 未识别 `loongarch64`

   原日志症状：
   ```text
   configure: error: Unsupported architecture: loongarch64
            (arch support table has no entry for this machine)
   ```

   根因：`configure` 的架构 `case` 表缺少 `loongarch64` 条目，`*)` 分支直接失败。

   规避：在 `case "$ARCH"` 中新增 `loongarch64)` 分支，并设置 `ARCH_CFLAGS`。

2. 只加 `loongarch64` 分支但缺 `-mlasx` 时，LASX 代码编译失败

   原日志症状包括：
   ```text
   src/lasx.h:4:29: error: unknown type name '__m256i'
   ```

   以及：
   ```text
   warning: implicit declaration of function '__lasx_xvadd_w'; did you mean '__builtin_lasx_xvadd_w'?
   ```

   根因：`lasxintrin.h` 中的 `__m256i` 与 `__lasx_*` 原型受 `__loongarch_lasx` 宏门控。该 gcc 默认 `la64v1.0` 已隐含 128-bit SIMD，因此 `__m128i` / LSX 路径不一定报错；真正卡住 `src/lasx.c` 的是缺 `-mlasx`。

   规避：为 loongarch64 分支设置：
   ```sh
   ARCH_CFLAGS="-mlsx -mlasx"
   ```
   本项目 Makefile 通过 `$(ARCH_CFLAGS)` 同时作用于 CFLAGS 与链接行。

3. 手工写 `config.mk` 会被 `make` 重新调用 `./configure` 覆盖

   日志中 `Makefile` 的 `config.mk` 目标会再次执行 `./configure`。如果 `configure` 未修复，手工生成的 `config.mk` 被覆盖后仍会报架构错误。

   规避：先修 `configure`，再执行 `make clean && make`，保证可复现构建。

4. 验收脚本夹具问题导致首轮 `FAIL`

   日志中出现：
   ```text
   FAIL: ELF is LoongArch
   ```

   以及：
   ```text
   mktemp: too few X's in template '/work/build/run-612'
   ```

   根因：这是验收脚本夹具问题，不是产品缺陷。
   - 本机 `file` 输出为：
     ```text
     ELF 64-bit LSB executable, LoongArch, ...
     ```
     不含 `EM:` 前缀，脚本若 grep `EM: LoongArch` 会误判失败。
   - `mktemp` 模板 `$$` 展开后缺少 `X` 占位符。

   规避：
   - 将 ELF 检查改为 grep `LoongArch`。
   - 将临时目录模板改为：
     ```sh
     RUN=$(mktemp -d /work/build/run-$$.XXXXXX)
     ```

## 验证方法

本轮验收采用轻量三件套，完整测试套件非必需。`make test` 在本项目中即运行 `./lsxdemo`，未展开 CI 级测试。

1. 干净环境装产物

   检查产物可执行、ELF 架构与依赖：
   ```text
   PASS: artifact present & executable
   PASS: ELF is LoongArch
   PASS: ldd all deps resolved
   ```

   实测 `file` 输出：
   ```text
   /work/output/lsxdemo: ELF 64-bit LSB executable, LoongArch, version 1 (SYSV), dynamically linked, interpreter /lib64/ld-linux-loongarch-lp64d.so.1, ...
   ```

   `ldd` 仅依赖 `libc.so.6`、`ld-linux-loongarch-lp64d.so.1` 与 vdso，无 `not found`。

2. 导入 / 加载

   将二进制拷贝到 `mktemp -d` 创建的干净目录，脱离源码树执行：
   ```sh
   timeout 30 ./lsxdemo
   ```

   验收结果：
   ```text
   PASS: loads & exits in clean dir (rc=0)
   ```

3. 一次最小真实调用

   程序执行一次 LSX 128-bit 向量加法和一次 LASX 256-bit 向量加法，输出：
   ```text
   LSX add check: PASS
   LASX add check: PASS
   ```

   验收结果：
   ```text
   PASS: LSX 128-bit vector add result
   PASS: LASX 256-bit vector add result
   PASS: no FAIL line in output
   RESULT pass=7 fail=0
   ```