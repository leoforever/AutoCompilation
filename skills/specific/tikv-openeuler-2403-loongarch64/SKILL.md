---
name: tikv-openeuler-2403-loongarch64
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  Tikv 9.0.0-beta.2 在龙芯 openEuler 24.03 LTS loongarch64 上的 release 编译步骤与补丁记录。编译 tikv-server、tikv-ctl、cargo build、make release、Rust 工具链、nix、grpcio-sys、RocksDB 时读这条。
---

## 简要场景

- 软件：Tikv 9.0.0-beta.2（master，git `9d30d0a9ffdaebd53b4df8cf77abf174e4610756`）
- 系统：openEuler 24.03 (LTS-SP4) loongarch64
- 内核：`6.6.52-1.7.lns23.loongarch64`
- CPU：Loongson-3C5000 ×32
- 页大小：`getconf PAGE_SIZE` = `16384`，jemalloc 自动探测出 `LG_PAGE 14`
- 结果：`make release` 编译成功，产物为 `tikv-server`、`tikv-ctl`
- 现场验证：`RESULT pass=13 fail=0`

## 构建步骤

1. 安装 C/C++、构建工具和依赖：

```sh
dnf install -y --allowerasing gcc-c++ make cmake ninja-build protobuf-compiler protobuf-devel openssl-devel autoconf automake libtool libzstd-devel lz4-devel snappy-devel
dnf install -y clang clang-devel clang-libs llvm llvm-devel
```

说明：`bindgen` 需要系统 `libclang.so.17`，因此安装 `clang clang-devel clang-libs llvm llvm-devel`。

2. 安装 Rust 工具链：

```sh
curl -sSf --proto '=https' --tlsv1.2 https://static.rust-lang.org/rustup/dist/loongarch64-unknown-linux-gnu/rustup-init -o rustup-init
RUSTUP_HOME=/work/rustup CARGO_HOME=/work/cargo ./rustup-init -y --no-modify-path --default-toolchain none
rustup toolchain install nightly-2026-01-30 --profile minimal -c rustfmt -c clippy -c rust-src
```

本次得到：

- `rustc 1.95.0-nightly (842bd5be2 2026-01-29)`
- `cargo 1.95.0-nightly (efcd9f586 2026-01-23)`

3. 拉取 Rust 依赖：

```sh
cd /work/src
cargo fetch --locked
```

本次 `cargo fetch --locked` 约 40 分钟，包含 `kvproto`、`raft-rs`、`rust-rocksdb`、`protobuf` 等 git 依赖。

4. 创建 RocksDB CMake 初缓存文件，规避 loongarch64 分支追加 gcc 不认的 `-mcpu/-mtune`：

```cmake
# /work/build/loongarch64-init.cmake
# rocksdb CMakeLists 的 loongarch64 分支：CHECK_C_COMPILER_FLAG("-march=loongarch64" HAS_LOONGARCH64)
# 探测成功后会追加 -mcpu=loongarch64 -mtune=loongarch64，openEuler gcc12 不认这两个选项。
# 预置缓存变量为 FALSE，CHECK_C_COMPILER_FLAG 见已定义即跳过测试，从而不追加。
set(HAS_LOONGARCH64 FALSE CACHE BOOL "avoid unsupported -mcpu/-mtune on loongarch64 gcc" FORCE)
```

5. 对 Rust 依赖打补丁：

- 给 `nix-0.24.1` 与 `nix-0.26.2` 的 `src/sys/ioctl/linux.rs` 通用 ioctl `cfg` 列表加入 `target_arch = "loongarch64"`。
- 给 `grpcio-sys` 打补丁，让它在 loongarch64 上复用 crate 自带预生成 `bindings/bindings.rs`。
- 打完 `grpcio-sys` 补丁后清理其 fingerprint，再重新构建。

6. 构建：

```sh
export CMAKE_TOOLCHAIN_FILE=/work/build/loongarch64-init.cmake
export LIBCLANG_PATH=/usr/lib64
export CARGO_TARGET_DIR=/work/build/target
export CC=gcc
export CXX=g++
export TIKV_FRAME_POINTER=0
export ROCKSDB_SYS_SSE=0
cd /work/src
make release
```

本次最终由 `make` 生成的实际 cargo 命令为：

```sh
cargo build --release --no-default-features --features "memory-engine jemalloc mem-profiling portable test-engine-kv-rocksdb test-engine-raft-raft-engine trace-async-tasks openssl-vendored"
```

7. 收集产物：

```sh
cp -a /work/build/target/release/tikv-server /work/build/target/release/tikv-ctl /work/output/
```

## 踩坑点

### 1. `dnf` 安装 `gcc-c++` 时 gcc 版本冲突

症状：

```text
Error: cannot install both gcc-12.3.1-110.oe2403sp4 and gcc-12.3.1-111.oe2403sp4 from @System
```

现场还看到：

```text
cannot install both gcc-12.3.1-110.oe2403sp4.loongarch64 from OS and gcc-12.3.1-111.oe2403sp4.loongarch64 from @System
```

根因：镜像里 OS 仓库的 `gcc-c++` 只到 `-110`，系统 `gcc` 已被 update 仓库升到 `-111`，默认策略不肯降级 `gcc`。

规避：

```sh
dnf install -y --allowerasing gcc-c++ make cmake ninja-build protobuf-compiler protobuf-devel openssl-devel autoconf automake libtool libzstd-devel lz4-devel snappy-devel
```

本次实际安装 `gcc-c++-12.3.1-110`，与已装 `gcc-12.3.1-111` 混用正常。

### 2. `nix` 0.24.1 在 loongarch64 编译失败

症状：

```text
error[E0432]: unresolved import `self::consts`
  --> .../nix-0.24.1/src/sys/ioctl/linux.rs:52:15
   | pub use self::consts::*;
```

根因：`src/sys/ioctl/linux.rs` 里通用 ioctl protocol 的 `mod consts` 的 `cfg(any(target_arch = ...))` 列表没有 `loongarch64`。

规避：给 `nix-0.24.1` 与 `nix-0.26.2` 的 `src/sys/ioctl/linux.rs` 通用 ioctl `cfg` 列表加：

```text
target_arch = "loongarch64"
```

### 3. `make release` 命令行传变量导致 jemalloc 报错

症状：

```text
No rule to make target '-j'
```

规避：构建脚本改为用环境变量传：

```sh
export TIKV_FRAME_POINTER=0
export ROCKSDB_SYS_SSE=0
```

不要写在 `make release` 命令行上。

### 4. RocksDB 在 loongarch64 上误开 SSE

事实：上游 Makefile 的 `ROCKSDB_SYS_SSE` 判断只对 `uname -p` 为 `aarch64`、`arm`、`arm64` 时置 0。

现场 `uname -p` 返回：

```text
loongarch64
```

因此 loongarch64 会落到默认分支，可能加 `sse` feature，并触发：

```text
CMake Error: FORCE_SSE42=ON but unable to compile with SSE4.2 enabled
```

规避：显式设置：

```sh
export ROCKSDB_SYS_SSE=0
```

### 5. RocksDB loongarch64 分支追加 gcc 不认的 `-mcpu/-mtune`

事实：RocksDB CMakeLists 的 loongarch64 分支探测 `-march=loongarch64` 成功后会追加：

```text
-mcpu=loongarch64 -mtune=loongarch64
```

openEuler gcc 12 不认这两个选项。

规避：使用 `/work/build/loongarch64-init.cmake` 预置：

```cmake
set(HAS_LOONGARCH64 FALSE CACHE BOOL "avoid unsupported -mcpu/-mtune on loongarch64 gcc" FORCE)
```

并设置：

```sh
export CMAKE_TOOLCHAIN_FILE=/work/build/loongarch64-init.cmake
```

### 6. `grpcio-sys` 在 loongarch64 上 bindgen panic

症状：`grpcio-sys` build.rs 在 loongarch64 上跑 `bindgen 0.59.2` panic。

规避：打补丁让它复用 crate 自带的预生成：

```text
bindings/bindings.rs
```

清理 `grpcio-sys` 的 fingerprint 后重新执行 `make release`。

### 7. 默认 `TIKV_FRAME_POINTER=1` 会走 build-std

事实：上游 Makefile 默认 `TIKV_FRAME_POINTER=1` 会走：

```text
-Z build-std=core,std,... --target <triple>
```

需要 `rust-src` 且重编标准库，时间翻倍，且 `-Z build-std` 在新 nightly 上仍属不稳定项。

规避：显式设置：

```sh
export TIKV_FRAME_POINTER=0
```

## 验证方法

基本三件套即可验收，完整 `verify.sh` 的 `pass=13 fail=0` 不是必需。

1. 干净环境装产物：

将产物复制到空目录：

```sh
mkdir -p /tmp/tikv-clean
cp /work/output/tikv-server /work/output/tikv-ctl /tmp/tikv-clean/
```

2. 加载 / 版本检查：

```sh
timeout 30 /tmp/tikv-clean/tikv-server --version
timeout 30 /tmp/tikv-clean/tikv-ctl --version
```

预期出现：

```text
Release Version:   9.0.0-beta.2
```

3. 一次最小启动调用：

```sh
D=/tmp/tikv-min-run
rm -rf "$D"
mkdir -p "$D"

MALLOC_CONF="prof:true,prof_active:false" timeout 20 /tmp/tikv-clean/tikv-server --pd-endpoints=127.0.0.1:12379 -L info > "$D/log" 2>&1

echo -n 'heap-prof-error count='
grep -c 'failed to enable heap profiling' "$D/log"

grep -m2 'Welcome to TiKV\|Release Version' "$D/log"
ls "$D/data"
```

预期：

- 日志出现 `Welcome to TiKV`
- 日志出现 `Release Version:   9.0.0-beta.2`
- `heap-prof-error count=0`
- 生成 `rocksdb.info`、`raftdb.info`
- 因无 PD 导致 `timeout` 返回 `rc=124` 属正常