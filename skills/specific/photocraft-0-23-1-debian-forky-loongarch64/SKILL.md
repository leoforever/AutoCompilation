---
name: photocraft-0-23-1-debian-forky-loongarch64
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  PhotoCraft 0.23.1（Rust 图像编辑器 GUI/CLI）在龙芯 Debian forky/sid、Python 3.12 环境中的构建与打包步骤（cargo 1.99.0、apt -dev 依赖、craft-i18n 快照、lld 链接）。编译 PhotoCraft、photocraft-cli、生成 deb/tar.gz、或撞到 R_LARCH_B26 overflow 时读这条。
---

## 简要场景

- 软件：PhotoCraft 0.23.1，纯 Rust 图像编辑器；GUI 使用 egui/eframe + wgpu，另有 headless CLI。
- 源码：`/work/src/photocraft-main`，来自 GitHub tarball 快照，无 `.git`。
- 系统：Debian forky/sid loongarch64；内核 `6.6.52-1.7.lns23.loongarch64`。
- 实测环境：python3 3.12.16；gcc/g++ 15.3.0；glibc 2.43；make 4.4.1；pkg-config 2.5.1。
- Rust：货架件 `/work/cache/rust/rust-1.99.0-loongarch64-unknown-linux-gnu.tar.xz`，安装后得到 `cargo 1.99.0` / `rustc 1.99.0`，host `loongarch64-unknown-linux-gnu`。
- 项目门槛：`rust-version = "1.95"`、`edition = "2024"`，Rust 1.99.0 满足。
- 结果：成功，产出 `photocraft` GUI 二进制、`photocraft-cli` 二进制、deb 包、tar.gz 包。

## 构建步骤

1. 安装 Rust 工具链：

```sh
tar xJf /work/cache/rust/rust-1.99.0-loongarch64-unknown-linux-gnu.tar.xz -C /work/build
cd /work/build/rust-1.99.0-loongarch64-unknown-linux-gnu
./install.sh --prefix=/work/rusttool
export PATH=/work/rusttool/bin:$PATH
```

2. 安装系统依赖。本容器是 Debian，使用 `apt-get`，包名按 Debian 命名：

```sh
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  libpipewire-0.3-dev libspa-0.2-dev libwayland-dev libxkbcommon-dev \
  libdrm-dev libgbm-dev libx11-dev libxcursor-dev libxi-dev libxrandr-dev libxinerama-dev \
  libasound2-dev libfontconfig-dev libclang-dev clang cmake pkg-config libgtk-3-dev
```

3. 配置 cargo。`CARGO_HOME` 使用 `/work/cargo`，并保留 `git-fetch-with-cli = true`：

```sh
mkdir -p /work/cargo
cat > /work/cargo/config.toml <<'EOF'
[net]
git-fetch-with-cli = true
EOF
export CARGO_HOME=/work/cargo
```

4. 准备 `craft-i18n` 翻译目录。build.rs 需要 `CRAFT_I18N_DIR` 输入；本次按上游 Dockerfile 与 `craft-i18n.rev` 指定的快照拉取到 `/work/opt/craft-i18n`。

实际规避管道 tar 权限告警的方式：先 `curl -o /tmp/i18n.tgz`，再 `tar -xzf --strip-components=1` 解包。

```sh
export CRAFT_I18N_DIR=/work/opt/craft-i18n
```

可选输入 `CRAFT_FONTS_DIR` 未提供，因此只编入仓库自带 Inter/JetBrainsMono。

5. 拉取依赖：

```sh
cd /work/src/photocraft-main
cargo fetch --locked
```

本次 97 个 git 依赖按 `Cargo.lock` 一次拉齐。crates.io 走默认 `index.crates.io` + `static.crates.io`，实测可用；曾尝试 `rsproxy.cn`，其 `crates.io-index` 路径返回 404，未采用。

6. 先构建 CLI：

```sh
cargo build --release --locked -j 32 -p photocraft-cli --features heif --bins
```

产物可执行：

```sh
./target/release/photocraft-cli --version
```

本次输出：

```text
photocraft-cli 0.23.1 (dev build)
```

7. 构建 GUI 前，先安装 lld/mold 并配置 linker。默认 bfd ld 链接 `photocraft` 会失败，见“踩坑点”。

```sh
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends lld mold

cat > /work/build/lld-gcc.sh <<'EOF'
#!/bin/sh
exec gcc -fuse-ld=lld "$@"
EOF
chmod +x /work/build/lld-gcc.sh

cat >> /work/cargo/config.toml <<'EOF'
[target.loongarch64-unknown-linux-gnu]
linker = "/work/build/lld-gcc.sh"
EOF
```

然后构建 GUI：

```sh
cargo build --release --locked -j 32 -p photocraft --features heif --bins
```

8. 生成 man page 与 shell 补全：

```sh
cargo build --release --locked -j 16 -p photocraft-cli --features heif --example man-and-completions
```

本次生成 9 个 man page 与 bash/zsh/fish/elv 补全。

9. 打包。上游 `packaging/linux/package.sh` 的架构白名单不包含 loongarch64，且 deb/rpm 腿依赖 nfpm；本次按上游 FHS 布局手工复刻：

- `photocraft-0.23.1-linux-loongarch64.deb`：用 `dpkg-deb` 构建，Depends 照抄上游 `nfpm.yaml` 的 deb 段，并带上游 `postinst.sh` 作 postinst/postrm 刷新 desktop/mime/icon 缓存。
- `photocraft-0.23.1-linux-loongarch64.tar.gz`：usr 树平铺，`bin/` 在顶层。

## 踩坑点

1. **loongarch64 bfd ld 链接大二进制时分支重定位越界**

`cargo build --release --locked -p photocraft --features heif --bins` 使用默认 bfd ld 时失败，关键原文：

```text
ld: libring-*.rlib(p256.o): relocation R_LARCH_B26 overflow 0xfffffffff5242860
relocation truncated to fit: R_LARCH_B26 against symbol 'memset@@GLIBC_2.36'
collect2: error: ld returned 1
```

本次根因：`ring` 的 loongarch64 目标码中，到 libc 符号（如 `memset` / `memcpy`）的分支距离超出 `R_LARCH_B26` 范围；`photocraft` 主程序链接单元极大，跨段距离越界。`photocraft-cli` 体积较小，先链接成功；`photocraft` 失败。

规避：安装 `lld` 与 `mold`，通过 cargo `target.loongarch64-unknown-linux-gnu.linker` 指向 `lld-gcc.sh`，让 gcc 使用 `-fuse-ld=lld`。不要替换 `/usr/bin/ld` 或 gcc。

2. **拉取 craft-i18n 快照时 tar 修改目录权限失败**

首次 `curl | tar -xz` 返回 rc=2，关键原文：

```text
tar: locales/xx: Cannot change mode ...: Operation not permitted
```

本次根因：容器内 tar 对已存在目录 chmod/chown 被拦，但文件内容已解出。

规避：改为 `curl -o /tmp/i18n.tgz` + `tar -xzf --strip-components=1`，忽略 mode 告警，并以 `locales/*/photocraft.tsv` 计数确认内容完整。

3. **解包交付物时同样出现 mode 告警**

`dpkg-deb -x` 或 `tar -xz` 解自己的包也可能报：

```text
Cannot change mode ...: Operation not permitted
```

本次不是包损坏，而是容器内 tar 恢复目录权限失败。

规避：验证脚本改用 `python3 tarfile.extractall`，避免对已存在目录 chmod。

4. **workspace patch 警告不是错误**

构建日志出现：

```text
warning: patch egui_demo_lib / egui_inspection / hayro-* / krilla ... was not used in the crate graph
```

本次判断：workspace `[patch.crates-io]` 声明的 fork 包只在某些成员或 wasm 腿里用到；`--locked` 下 Cargo.lock 未变，构建正常，不处理。

5. **CLI 参数名不能凭记忆猜**

曾执行：

```text
photocraft-cli run --new '{"fill":"transparent",...}'
```

报错：

```text
--new: invalid parameters for 'file.new': unknown parameter 'fill'
```

本次规避：不猜参数，改用真实文件驱动流程，例如 `info`、`convert png→jpg/psd`、`run --cmd filter.blur.gaussianBlur --params '{"radius":2}'`；参数名先用 `commands --filter blur` 和一次成功调用确认。

6. **上游打包脚本不直接支持 loongarch64**

上游 `packaging/linux/package.sh` 的架构白名单只有 x86_64/aarch64/riscv64，loongarch64 会 `exit 2`；且 deb/rpm 腿依赖 nfpm（Go 程序），本机无 Go。

本次规避：用 `dpkg-deb` 手工产 deb，布局和 Depends 照抄上游 `nfpm.yaml`；tar.gz 按上游 FHS 布局手工复刻。

## 验证方法

本次交付包验证脚本对 deb 与 tar.gz 各跑一轮，均 `RESULT pass=12 fail=0`。攻略只保留最轻三件套；完整套件验证非必需。

1. 干净环境装产物

- deb：`ar x` 取出 `data.tar.xz`，用 `python3 tarfile.extractall` 解包，确认 `usr/bin/photocraft` 与 `usr/bin/photocraft-cli` 就位。
- tar.gz：同样用 `python3 tarfile.extractall` 解包，确认二进制与 `usr/` 树就位。

2. 导入/加载

```sh
file usr/bin/photocraft
file usr/bin/photocraft-cli
```

应断言为：

```text
ELF 64-bit LSB pie executable, LoongArch
```

检查动态依赖：

```sh
ldd usr/bin/photocraft | grep -c "not found"
ldd usr/bin/photocraft-cli | grep -c "not found"
```

应均为 `0`。

检查 CLI 版本：

```sh
usr/bin/photocraft-cli --version
```

应输出：

```text
photocraft-cli 0.23.1 (dev build)
```

3. 一次最小调用

使用真实图片执行一次转换或滤镜命令，例如：

```sh
usr/bin/photocraft-cli run --cmd filter.blur.gaussianBlur --params '{"radius":2}'
```

本次还验证了 PNG → JPG/PSD 的真实转换命令链，出图正常。