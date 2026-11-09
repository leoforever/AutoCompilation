---
name: rust-toolchain
type: layer1-infra
description: |
  龙芯平台 Rust 工具链：三条安装路线的实测取舍（dnf 系统包 / 货架 rust 母包解包 / rustup 官方脚本含 loongarch 缺 cargo 深坑）。
  编译 Rust 项目（cargo build）、装 rust/cargo、tokio/clap/tikv/rust 绑定时读这条。
allowed-tools: [execute, read_file]
---

# Rust 工具链（龙芯 LoongArch）

## 选路（一行决策）

**Rust 版本需求 ≤1.84.1 → dnf；要新版 → 解货架 rust 母包；rustup 是最后选择且必须验货。**

## 路线 A · dnf 系统包（默认，秒级零下载）

```bash
dnf install -y rust cargo
# cargo 1.84.1 / rustc 1.84.1 (Loongnix OS 1.84.1-1.1.lns23)
```
- 实测案例：clap（+42.6s 装完即编）、cryptography（rust cargo python3-devel 一条命令，maturin 出 wheel 全程顺）。
- 版本锁死 1.84.1：项目 `rust-version` 更高、或依赖 crate 用了新语法时顶不住，转路线 B。

## 路线 B · 货架 rust 母包（要新版时首选，确定性最高）

公共池备有官方完整发行包 `rust-1.99.0-loongarch64-unknown-linux-gnu.tar.xz`（『通用·rust』行）：

```bash
tar xJf /work/cache/rust/rust-1.99.0-loongarch64-unknown-linux-gnu.tar.xz -C /tmp
cd /tmp/rust-1.99.0-loongarch64-unknown-linux-gnu && ./install.sh
cargo --version && rustc --version          # 必须实测确认
```
- 解包即含 cargo/rustc/rust-std/clippy/rustfmt 全家，不经过 rustup 的 manifest 层，无缺件风险。
- 280MB 本地秒取，对比外网下载省一轮网络风险。

## 路线 C · rustup 官方脚本（可用但带坑，非首选）

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
source "$HOME/.cargo/env"
cargo --version        # ← 铁律：装完必验，rustup 退出码 0 ≠ cargo 在位
```
- **loongarch 专属深坑（tokio 案烧 250s/6 轮实锤）**：rustup 可能把 cargo 组件"谎报已装"——
  `component list --installed` 显示在、`component add` 回"up to date"，但
  `toolchains/stable-loongarch64-*/bin/` 里根本没有 cargo 文件；跑 cargo 报
  `the 'cargo' binary … is not applicable to the 'stable-loongarch64-unknown-linux-gnu' toolchain`。
- 修补组件的全部姿势（component remove/add、--force、--verbose、直调路径）实测**均无效**；
  唯一解法：`rm -rf ~/.cargo ~/.rustup`（连 settings.toml）整目录清掉重装。
- 版本时效注记：10-16 实测 1.85~1.90 的 loongarch dist 全 404；**11-09 复检 stable 通道
  `cargo-loongarch64` 已 available=true**（1.99.x 有货）——此坑对新版理论上自愈，
  但没人跑过真任务复检，照旧按"必验货"执行。

## 独立 tarball 直拉（网络好时的路线 B 备胎）

```
https://static.rust-lang.org/dist/rust-<版本>-loongarch64-unknown-linux-gnu.tar.xz   # 280MB，有 sha256 附属文件
```
仅当货架没有你需要的主版本时用；旧版本（1.85~1.90）loongarch 无包，404 别硬试。

## 不要做

- 不要 `rustup` 装完不验 cargo 就往下跑构建（错误会漂移到"编译失败"假象）。
- 不要给 cargo 缺件打 component 补丁（实测无效，浪费时间）。
- 不要猜 dist URL 版本号——货架 INDEX 里有啥用啥，或先 curl -sI 探 200。

## crates 拉取补充

crates.io 直连实测可用（cryptography 案 `cargo metadata --locked` 顺）；暂无需专用 mirror。
git 型依赖（Cargo.toml 里 url=github 的）受 GitHub 速度摆布，量大时走取货降级链。
