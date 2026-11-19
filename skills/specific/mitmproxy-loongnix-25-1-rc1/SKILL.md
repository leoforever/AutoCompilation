---
name: mitmproxy-loongnix-25-1-rc1
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  mitmproxy 13.0.0.dev 的 loongarch64 wheel 构建与离线交付记录。龙芯 Loongnix 25.1-rc1、编译 mitmproxy、mitmdump、mitmweb、mitmproxy_rs、mitmproxy_linux、maturin、brotli、cryptography、tornado、urwid、eBPF、lpypi。
---

## 简要场景
- 软件：mitmproxy 13.0.0.dev，源码 `https://github.com/mitmproxy/mitmproxy`（branch 未记录）；`mitmproxy/version.py` 为 `VERSION = "13.0.0.dev"`，wheel 版本为 `13.0.0.dev0`。
- 系统：Loongnix GNU/Linux 25 / `loongnix-25.1-rc1`，deb/apt 系，内核 `Linux 6.6.52-1.7.lns23.loongarch64`，架构 loongarch64。
- 结果：编译成功；交付形态为 `/work/output/wheels` 下的 Python wheel 集合。BUILD_NOTES 记录 42 个 wheel，含 8 个 loongarch64 原生件；本项目按 Python 规范交付 wheel，不包成 deb。

## 构建步骤
1. 先探测环境，确认 Python、gcc、glibc、OpenSSL 与发行版类型：
   ```bash
   uname -a
   cat /etc/os-release
   python3 -V
   gcc --version
   ldd --version
   ```
   本次实测：Python 3.13.5、gcc 14.2.0、glibc 2.41；`mitmdump --version` 报出 OpenSSL 3.5.4 30 Sep 2025。

2. 裸 base 容器无 `python3`、`gcc`、`git`、`pip`，先 apt 自举：
   ```bash
   DEBIAN_FRONTEND=noninteractive apt-get update
   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     build-essential python3 python3-dev python3-venv python3-pip pkg-config \
     libssl-dev zlib1g-dev libffi-dev
   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     llvm-dev clang libelf-dev make
   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     libpolly-19-dev llvm-19-dev
   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends rustup
   ```

3. 创建 venv，并从龙芯专源 `lpypi` 安装构建工具：
   ```bash
   python3 -m venv /work/venv
   /work/venv/bin/pip install -q --index-url https://lpypi.loongnix.cn/loongson/pypi \
     build setuptools wheel setuptools-rust cffi
   ```

4. 复用货架 Rust 工具母包 `rust-1.99.0-loongarch64-unknown-linux-gnu.tar.xz`，安装到 `/opt/rust`：
   ```bash
   yes | ./install.sh --prefix=/opt/rust
   ```
   随后将 `/opt/rust/lib` 加入 `/etc/ld.so.conf.d` 下的 ld 配置，并执行 `ldconfig`。

5. 自举 maturin。lpypi 上 maturin 只有 sdist，本次使用：
   ```bash
   pip install --no-build-isolation /tmp/m16/maturin-1.16.0
   ```
   得到 `maturin-1.16.0-cp313-cp313-linux_loongarch64.whl`。

6. 构建 Rust/PyO3 核心扩展 `mitmproxy_rs-0.12.16`：
   ```bash
   maturin build --release
   ```
   产物为 `mitmproxy_rs-0.12.16-cp312-abi3-manylinux_2_39_loongarch64.whl`。

7. 逐个构建 C/Rust 扩展 wheel：
   ```bash
   pip wheel --no-build-isolation --no-deps
   ```
   本次涉及 `cryptography-50.0.2`、`brotli-1.2.1`、`tornado-6.5.1`、`aioquic-1.8.0`、`pylsqpack-1.1.1`、`bcrypt-5.0.0`、`backports_zstd-1.7.0`、`markupsafe-3.0.4`。其中 `cryptography` 需先装 `cffi`；`brotli` 需打补丁，但本次日志未给出补丁内容。

8. 构建 mitmproxy 本体：
   ```bash
   pip wheel .
   ```
   产物为 `mitmproxy-13.0.0.dev0-py3-none-any.whl`。

9. 构建 `mitmproxy_linux-0.12.16`（redirector + eBPF）：
   ```bash
   rustup toolchain install nightly-2025-05-01 --component rust-src
   cargo install bpf-linker@0.10.4 --no-default-features --features rust-llvm-20
   ```
   并修改 `build.rs` 指定 `Toolchain::Custom`、降低 workspace MSRV；产物为 `mitmproxy_linux-0.12.16-py3-none-linux_loongarch64.whl`。

10. 使用 `/work/build/depfix.sh` 做依赖闭环，策略为“缺啥拉啥，wheel 优先、sdist 就地编”，最终将 wheel 归档到 `/work/output/wheels`。

## 踩坑点
1. **裸 base 没有 Python / gcc / pip**
   - 实测：`which python3 gcc git pip` 全部没有。
   - 规避：先 `apt-get update` 并安装 `build-essential python3 python3-dev python3-venv python3-pip pkg-config libssl-dev zlib1g-dev libffi-dev`。

2. **Loongnix 25 是 deb/apt，不是 dnf**
   - 实际包名为 `python3-dev`、`libssl-dev`，不是 `python3-devel`。
   - 规避：使用 `apt-get install`，不要照搬 dnf 包名。

3. **系统 pip 受 PEP668 限制**
   - 现象：对系统 pip 直装会被 Debian PEP668 `externally-managed` 拒绝。
   - 规避：必须 `python3 -m venv /work/venv` 后在 venv 内安装。

4. **lpypi 缺 loongarch64 wheel，需回退 sdist**
   - 原日志：
     ```text
     ERROR: Could not find a version that satisfies the requirement mitmproxy_rs (from versions: none)
     ERROR: No matching distribution found for mitmproxy_rs
     ```
   - 涉及：`mitmproxy_rs`、`mitmproxy_linux`、`brotli`、`backports.zstd`、`tornado`。
   - 规避：这些包从 lpypi 下载 sdist 后源码编译。

5. **maturin 后端缺失**
   - 原日志：
     ```text
     pip._vendor.pyproject_hooks._impl.BackendUnavailable: Cannot import 'maturin'
     ```
   - 规避：先自举 `maturin-1.16.0`，再构建 `mitmproxy_rs`。

6. **货架 Rust 包在 `/tmp` 解包权限失败**
   - 原日志：
     ```text
     tar: rust-1.99.0-loongarch64-unknown-linux-gnu/cargo/bin: Cannot change mode to rwxr-xr-x: Operation not permitted
     ```
   - 规避：使用 `yes | ./install.sh --prefix=/opt/rust` 正式安装，并把 `/opt/rust/lib` 加入 ld 配置后执行 `ldconfig`。

7. **Rust 找不到 std/core**
   - 原日志：
     ```text
     error[E0463]: can't find crate for 'std'
     the loongarch64-unknown-linux-gnu target may not be installed
     ```
   - 根因：直接把 tar 解包内容当工具链使用，`install.sh` 未成功安装，导致 `rustc/lib/rustlib/<triplet>/lib` 未就位。
   - 规避：走 `install.sh` 正式安装到 `/opt/rust`。

8. **brotli 1.2.1 在 loongarch gcc14 上编译失败**
   - 原日志：
     ```text
     c/common/constants.h:196:23: error: invalid argument of 'model' attribute
     ```
   - 规避：BUILD_NOTES 只记录“brotli 需打补丁”，未提供补丁内容；不要臆造修复命令，应保留该报错并扩大日志或获取对应补丁。

9. **专源版本比 mitmproxy pin 更新，需要降级**
   - lpypi 上 `tornado 6.6.1`、`urwid 5.1.3` 可装，但违反 mitmproxy 的 pin：`tornado ≤6.5.10`、`urwid ≤4.2.5`。
   - 规避：回退到 `tornado-6.5.1`、`urwid-4.2.5`。

10. **容器内没有 curl**
   - 原日志：
     ```text
     timeout: failed to run command 'curl': No such file or directory
     ```
   - 规避：索引探测不要依赖 curl；本次使用 `pip download` / `pip index versions` 探测 lpypi 可用包。

## 验证方法
完整套件验证非必需；本次验收采用三件套。

1. **干净环境安装产物**
   ```bash
   cd /work/verify
   rm -rf out-venv
   /work/venv/bin/python -m venv out-venv
   ./out-venv/bin/pip install --no-index --find-links=/work/build/wheels mitmproxy
   ```
   日志显示安装 `RC=0`，干净 venv 离线安装通过。

2. **导入 / 加载检查**
   ```bash
   ./out-venv/bin/mitmdump --version
   ldd /work/verify/out-venv/lib/python3.13/site-packages/mitmproxy_rs/*.so \
     /work/verify/out-venv/lib/python3.13/site-packages/cryptography/hazmat/bindings/*.so 2>&1 | grep -c 'not found'
   ```
   本次输出：
   ```text
   Mitmproxy: 13.0.0.dev
   Python:    3.13.5
   OpenSSL:   OpenSSL 3.5.4 30 Sep 2025
   Platform:  Linux-6.6.52-1.7.lns23.loongarch64-loongarch64-with-glibc2.41
   NOT_FOUND_COUNT=0
   ```

3. **一次最小调用**
   起 `mitmdump`，走一次真实 HTTP 代理请求并拿到 200 body。BUILD_NOTES 记录三件套验证结果为 `RESULT pass=9 fail=0`。