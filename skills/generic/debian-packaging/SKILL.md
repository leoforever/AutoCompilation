---
name: debian-packaging
type: layer1-infra
description: |
  Debian/Loongnix25 系打包通用流程。以下场景读这条：需要 apt 装依赖（loongnix 25 源配置）、
  把编译产物打成 .deb、dpkg-buildpackage 报 build-dependency 不满足、debhelper compat 版本过高、
  lintian 自检、control 文件 Depends 怎么写。
allowed-tools: [execute, read_file]
---

# Debian 打包（龙芯 Loongnix 25 / apt 系通用）

## apt 源与安装姿势
- Loongnix 25 默认源 `pkg.loongnix.cn/loongnix/25`（loongnix-stable main/contrib/non-free）；装包失败先 `apt-get update`（容器里 lists 常过期，报 404 也先 update 再判断——镜像 pool 会滚动更新）。
- 非交互固定写法：`DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends <pkgs>`。
- 构建机基础件：`build-essential pkg-config dpkg-dev debhelper fakeroot file binutils`。

## 有 debian/ 目录的上游：dpkg-buildpackage 路线
1. 直接 `dpkg-buildpackage -us -uc -b`（fakeroot 环境按上游规则来）。
2. 报 `dpkg-checkbuilddeps: unmet build dependencies` → 两类处理：
   - 缺普通依赖：apt 装齐（从报错串提取包名一次装）；
   - 版本要求超出本发行版（如源码是 sid 的、compat=14 而本系统 debhelper 13）：
     改 `debian/compat`(或 control 的 debhelper-compat) 降到本系统档位，
     同步下调 control 里带 (= 精确版本) 的依赖要求；
   - 构建工具本身在环依赖（如打 lintian 要 lintian）：`-d` 跳过依赖检查或 `nocheck`，
     文档类缺失件按报错补 perl/python 模块。
3. 产出在当前目录 `../*.deb`，拷 /work/output。

## 无 debian/ 目录的自产二进制：手组 deb（平台兜底同款）
```
目录树：data/usr/local/bin/<bin> + data/usr/local/share/<软件>/  （前缀听上游）
control 必备：Package/Version/Architecture/Maintainer/Description（Description 不能空行断）
Architecture：uname -m 映射 loongarch64→loong64（deb13 系）
打包：dpkg-deb --build --root-owner-group <dir> <out>.deb
自验：dpkg-deb -I（control 解析）+ dpkg-deb -c（内容表）——⚠两项过≠装对，
     数据树必须直接挂包根（出现 ./data/ 前缀=布局 bug，装完命令找不到），
     终验用 apt-get install 真装后 PATH 跑 --version。
```

## Depends 写法（别猜包名）
1. 对产物 ELF 跑 `ldd`，剔除 libc/libm/libpthread 等必带项，得外部库清单；
2. 库名→包名：`dpkg-shlibdeps`（有 dpkg-dev 时权威）；没有就查本发行版
   `apt-cache search <lib前缀>` 拿真名——**deb13/loongnix25 有 t64 后缀陷阱**：
   libssl3→libssl3t64、libpcap0.8→libpcap0.8t64、libssh2-1→libssh2-1t64；
   libexslt 已并入 libxslt1.1。**查不到确切包名就只写库名进 Description，不瞎猜**。
3. 运行时数据路径（share/nmap 这类）随包安装并在 Description 注明位置。

## 验证（薄口径）
- `dpkg-deb -I/-c` 结构与 control；
- 有条件时 `apt-get install -s <deb>` 干跑解析依赖；
- 主命令 `--version`/`--help` 真跑（timeout 包裹）。
