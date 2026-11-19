---
name: tikv-7-6-0-openeuler-2403-sp2-loongarch64
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  TiKV 7.6.0（tikv-server / tikv-ctl）：龙芯 loongarch64 openEuler-24.03 LTS-SP2 的构建流程与验证口径。编译 tikv 7.6.0、装 Rust 工具链、cargo fetch、make release、验证 tikv-server 时读这条。
---

## 简要场景

- 软件：TiKV `v7.6.0`
- 源码：`https://github.com/tikv/tikv.git`，branch/tag `v7.6.0`，commit `4e32aff5d7f13db42bf856aacf32d1896b023aa3`
- 目标系统：openEuler 24.03 LTS-SP2，架构 `loongarch64`
- 内核：`6.6.52-1.7.lns23.loongarch64`
- 结果：编译成功，`make release` rc=0
- 产物：
  - `tikv-server`：TiKV 存储节点主程序，release，LoongArch ELF，PIE，动态链接
  - `tikv-ctl`：TiKV 运维控制工具，release，LoongArch ELF，PIE，动态链接
- 关键约束：TiKV `7.6.0` 源码 `rust-toolchain.toml` 指定 `nightly-2023-12-10`，本次按该版本安装 Rust 工具链；不要按较新攻略随意换 nightly。

## 构建步骤

### 1. 安装系统构建依赖

容器初始状态缺少 `g++`、`cmake`、`ninja`、`protoc`、`cargo`、`rustc`、`rustup`、`libclang`，本次安装：

```sh
dnf install -y --allowerasing gcc-c++ make cmake ninja-build protobuf-compiler protobuf-devel \
  openssl-devel autoconf automake libtool libzstd-devel lz4-devel snappy-devel
```

`clang/llvm` 提供 bindgen 需要的 `libclang.so.17`：

```sh
dnf install -y --allowerasing clang clang-devel clang-libs llvm llvm-devel
```

本次实测版本：

- gcc/g++：`12.3.1 (openEuler 12.3.1-93.oe2403sp2)`
- cmake：`3.27.9`
- ninja：`1.11.1`
- protoc：`libprotoc 25.1`
- clang/llvm：`17.0.6`，提供 `/usr/lib64/libclang.so.17`
- 系统 OpenSSL：`openssl-libs/openssl-devel 3.0.12-17.oe2403sp2`
- `PAGE_SIZE`：`16384`

### 2. 安装 Rust 工具链

本次使用独立目录，避免污染默认 HOME：

```sh
export RUSTUP_HOME=/work/rustup
export CARGO_HOME=/work/cargo
export PATH=/work/cargo/bin:$PATH
```

安装 rustup 与源码要求的 nightly：

```sh
curl -sSf --proto '=https' --tlsv1.2 \
  https://static.rust-lang.org/rustup/dist/loongarch64-unknown-linux-gnu/rustup-init -o rustup-init
./rustup-init -y --no-modify-path --default-toolchain none

rustup toolchain install nightly-2023-12-10 --profile minimal -c rustfmt -c clippy -c rust-src
```

得到：

- `rustc 1.76.0-nightly (06e02d5b2 2023-12-09)`
- host：`loongarch64-unknown-linux-gnu`
- `cargo 1.76.0-nightly (978722961 2023-12-06)`

### 3. 配置 cargo 使用系统 git 拉取依赖

在 `/work/cargo/config.toml` 中启用：

```toml
[net]
git-fetch-with-cli = true
```

该配置用于规避容器内 libgit2 HTTPS 栈拉取 GitHub 依赖失败的问题。

### 4. 准备 RocksDB CMake 初缓存

本次 RocksDB 来自 git 依赖 `tikv/rust-rocksdb.git#73ba736`，内含 RocksDB `6.29.5`。BUILD_NOTES 中创建了：

- `/work/build/loongarch64-init.cmake`
- 预置 `HAS_LOONGARCH64=FALSE`

本次 TiKV `7.6.0` 使用的 RocksDB `6.29.5` CMakeLists 没有 loongarch64 分支，因此该初缓存实际未触发旧攻略中的 `-mcpu/-mtune` 问题，但保留无害。

### 5. 拉取依赖

```sh
cd /work/src
cargo fetch --locked
```

本次直接 `cargo fetch --locked` 先失败，按“踩坑点”处理后才成功。成功后依赖规模：

- registry：约 832M
- git：约 1.6G
- 509 crate

### 6. 依赖补丁

本次在 `/work/cargo/registry/src/...` 与 git checkout 上打补丁，原文件均留 `.orig` 备份。

需要处理的依赖包括：

- `nix-0.24.1` / `nix-0.26.2`
  - 文件：`src/sys/ioctl/linux.rs`
  - 操作：通用 ioctl `cfg(any(...))` 中增加 `target_arch = "loongarch64"`

- `grpcio-sys-0.10.3+1.44.0-patched`
  - 文件：`build.rs`
  - 操作：`config_binding_path()` target 白名单增加 `loongarch64-unknown-linux-gnu`
  - 操作：两处 `cfg(any(target_arch="x86_64","aarch64"))` 增加 `loongarch64`
  - 目的：复用 crate 自带预生成 `bindings/bindings.rs`，绕开 bindgen 0.59 panic

- `openssl-src-111.25.0+1.1.1t`
  - 文件：`src/lib.rs`
  - 操作：增加映射：
    ```text
    "loongarch64-unknown-linux-gnu" => "linux64-loongarch64"
    ```

- `tikv-jemallocator-0.5.0`
  - 文件：`src/lib.rs`
  - 操作：`ALIGNOF_MAX_ALIGN_T` arch 列表增加 `loongarch64`
  - 本次 gcc 实测值：`16`

- `linux-raw-sys-0.1.4`
  - 新增：
    ```text
    src/loongarch64/errno.rs
    src/loongarch64/general.rs
    src/loongarch64/ioctl.rs
    src/loongarch64/netlink.rs
    ```
  - 在 `lib.rs` 中注册上述 4 个模块

- `pprof-0.11.1`
  - loongarch64 分支字段名从内核 sigcontext 风格：
    ```text
    sc_pc
    sc_regs[22]
    ```
    调整为 glibc `mcontext_t` 字段：
    ```text
    __pc
    __gregs[22]
    ```

- RocksDB `6.29.5`
  - 文件：`toku_time.h`
  - 操作：增加 `__loongarch__` 计时分支，使用：
    ```text
    clock_gettime(CLOCK_MONOTONIC)
    ```

### 7. 执行 release 构建

本次 `make release` 实际等价命令：

```sh
cargo build --release --no-default-features --features "jemalloc mem-profiling portable \
  test-engine-kv-rocksdb test-engine-raft-raft-engine cloud-aws cloud-gcp cloud-azure \
  trace-async-tasks openssl-vendored"
```

构建环境：

```sh
export TIKV_FRAME_POINTER=0
export ROCKSDB_SYS_SSE=0
export CMAKE_TOOLCHAIN_FILE=/work/build/loongarch64-init.cmake
export LIBCLANG_PATH=/usr/lib64
export CARGO_TARGET_DIR=/work/build/target
export CC=gcc
export CXX=g++
```

结果：

```text
make release rc=0
```

构建中间物：

- `/work/build/target`：约 4.0G
- `/work/cargo`：约 2.4G
- `/work/mirrors/google-protobuf.git`：本地 git 镜像，约 342M

### 8. 收集产物

```sh
cp -a /work/build/target/release/tikv-server /work/output/
cp -a /work/build/target/release/tikv-ctl /work/output/
```

交付目录：

- `/work/output/tikv-server`
- `/work/output/tikv-ctl`
- `/work/output/BUILD_NOTES.md`

## 踩坑点

### 1. `cargo fetch --locked` 使用 libgit2 时 SSL error

原日志：

```text
error: failed to load source for dependency `protobuf`
Caused by: Unable to update https://github.com/pingcap/rust-protobuf?branch=v2.8#6642ebaa
Caused by: failed to fetch submodule `google-protobuf` from https://github.com/google/protobuf
Caused by: SSL error: unknown error; class=Ssl (16)
```

触发场景：

- TiKV `7.6.0`
- openEuler 24.03 SP2 loongarch64 容器
- `cargo fetch --locked` 拉取 `pingcap/rust-protobuf` 及其 `google-protobuf` 子模块

规避方法：

- 在 `/work/cargo/config.toml` 中启用：

```toml
[net]
git-fetch-with-cli = true
```

让 cargo 使用系统 git，而不是 libgit2。

### 2. 系统 git 拉取 `google/protobuf` 大仓时 early EOF

原日志：

```text
error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly: CANCEL (err 8)
fetch-pack: unexpected disconnect while reading sideband packet
fatal: early EOF
fatal: fetch-pack: invalid index-pack output
```

触发场景：

- 启用 `git-fetch-with-cli` 后，系统 git 拉取 `google/protobuf` 子模块
- 全量对象约 273410 objects，约 340 MiB
- 容器网络下多次断流

规避方法：

```sh
git config --global http.version HTTP/1.1
git config --global http.postBuffer 524288000

git clone --mirror https://github.com/google/protobuf /work/mirrors/google-protobuf.git

git config --global url."/work/mirrors/google-protobuf.git".insteadOf "https://github.com/google/protobuf"
```

之后：

```sh
cargo fetch --locked
```

本次一次通过 rc=0。

### 3. `openssl-src` vendored OpenSSL 不认识 loongarch64

原日志：

```text
thread 'main' panicked at .../openssl-src-111.25.0+1.1.1t/src/lib.rs:303:18:
don't know how to configure OpenSSL for loongarch64-unknown-linux-gnu
```

触发场景：

- TiKV `7.6.0` 使用 `openssl-vendored` feature
- `openssl-src-111.25.0+1.1.1t` 内置 OpenSSL `1.1.1t`
- 其 target 到 OpenSSL Configure 名字映射没有 `loongarch64-unknown-linux-gnu`

规避方法：

- 修改 `openssl-src-111.25.0+1.1.1t/src/lib.rs`
- 增加映射：

```text
"loongarch64-unknown-linux-gnu" => "linux64-loongarch64"
```

### 4. `grpcio-sys` bindgen 路径与 loongarch64 target 不匹配

触发场景：

- `grpcio-sys-0.10.3+1.44.0-patched`
- 默认逻辑未把 `loongarch64-unknown-linux-gnu` 纳入预生成 bindings 路径
- 旧攻略提到 bindgen 0.59 panic，本次通过复用预生成 bindings 规避

规避方法：

- 修改 `grpcio-sys-0.10.3+1.44.0-patched/build.rs`
- `config_binding_path()` target 白名单增加：

```text
loongarch64-unknown-linux-gnu
```

- 两处：

```text
cfg(any(target_arch="x86_64","aarch64"))
```

增加：

```text
loongarch64
```

- 复用 crate 自带：

```text
bindings/bindings.rs
```

### 5. `nix` ioctl 通用分支未覆盖 loongarch64

触发场景：

- `nix-0.24.1` / `nix-0.26.2`
- `src/sys/ioctl/linux.rs` 的通用 ioctl `cfg(any(...))` 未包含 `loongarch64`

规避方法：

- 在对应 `cfg(any(...))` 中增加：

```text
target_arch = "loongarch64"
```

### 6. `tikv-jemallocator` 缺少 loongarch64 对齐常量

触发场景：

- `tikv-jemallocator-0.5.0/src/lib.rs`
- `ALIGNOF_MAX_ALIGN_T` arch 白名单没有 `loongarch64`

规避方法：

- 增加 `loongarch64` 分支
- 本次 gcc 实测值为：

```text
16
```

### 7. `linux-raw-sys 0.1.4` 缺少 loongarch64 模块

触发场景：

- `linux-raw-sys-0.1.4`，来自 `rustix 0.36.7`
- 无 loongarch64 架构模块

规避方法：

- 新增：

```text
src/loongarch64/errno.rs
src/loongarch64/general.rs
src/loongarch64/ioctl.rs
src/loongarch64/netlink.rs
```

- 在 `lib.rs` 注册上述模块

### 8. `pprof 0.11.1` loongarch64 sigcontext 字段名与 glibc 不一致

触发场景：

- `pprof-0.11.1`
- loongarch64 分支使用内核 sigcontext 字段名：
  - `sc_pc`
  - `sc_regs[22]`
- glibc `mcontext_t` 实际字段为：
  - `__pc`
  - `__gregs[22]`

规避方法：

- 将 loongarch64 分支字段名改为 glibc `mcontext_t` 字段。

### 9. RocksDB `6.29.5` 的 `toku_time.h` 缺少 loongarch64 计时实现

触发场景：

- RocksDB `6.29.5`
- PerconaFT `toku_time.h` 无 `__loongarch__` 计时分支
- BUILD_NOTES 记录该问题会在 `-Werror` 下导致失败

规避方法：

- 增加 `__loongarch__` 分支，使用：

```text
clock_gettime(CLOCK_MONOTONIC)
```

## 验证方法

平台验收口径只要求基本三件套，秒级到分钟级即可。完整套件验证非必需；本次日志中的 `RESULT pass=18 fail=0` 是平台验证脚本结果，不要求扩展到全量测试。

### 1. 干净环境装产物

将交付目录中的产物放到 openEuler 24.03 SP2 loongarch64 运行环境：

- `/work/output/tikv-server`
- `/work/output/tikv-ctl`

本次产物为动态链接 ELF，验证时确认依赖可解析。

### 2. 导入/加载

执行：

```sh
ldd /work/output/tikv-server | grep -c "not found"
ldd /work/output/tikv-ctl | grep -c "not found"
```

预期：

```text
0
0
```

本次日志结果：

```text
PASS: tikv-server ldd all deps resolved
PASS: tikv-ctl ldd all deps resolved
```

### 3. 一次最小调用

执行：

```sh
timeout 30 /work/output/tikv-server --version | head -3
timeout 30 /work/output/tikv-ctl --version | head -2
```

预期：

```text
TiKV
Release Version:   7.6.0
Edition:           Community
```

以及：

```text
TiKV Control (tikv-ctl)
Release Version:   7.6.0
```

本次日志最终 sanity 输出：

```text
TiKV
Release Version:   7.6.0
Edition:           Community
TiKV Control (tikv-ctl)
Release Version:   7.6.0
0
```

其中最后的 `0` 为 `ldd ... | grep -c "not found"` 结果。