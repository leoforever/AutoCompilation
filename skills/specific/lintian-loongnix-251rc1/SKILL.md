---
name: lintian-loongnix-251rc1
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  lintian 2.144.1 在龙芯 Loongnix 25.1-rc1 上构建 deb 包的步骤与验证。编译 lintian、打包 lintian、dpkg-buildpackage、fakeroot、chmod EPERM、man 页缺失、libdistro-info-perl get_all_series 时读这条。
---

## 简要场景

- 软件：lintian 2.144.1，Debian package checker，纯 Perl 项目，`Architecture: all`。
- 系统：Loongnix GNU/Linux 25（loongnix-stable），目标系统为 loongnix-25.1-rc1，`uname -m` 为 loongarch64。
- 结果：编译成功，交付 `lintian_2.144.1_all.deb` 与 `lintian-doc_2.144.1_all.deb`。
- 货架：`/work/cache/INDEX.md` 仅有 `通用·rust-1.99.0`，与本项目无关，未复用；依赖全部走 loongnix apt 源。

## 构建步骤

1. 探测环境与货架：

```bash
cat /work/cache/INDEX.md 2>/dev/null | head -50
cat /etc/os-release
uname -m
perl -v | head -3
dpkg --version | head -1
```

2. 安装构建依赖：

```bash
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  debhelper dpkg-dev fakeroot build-essential patch make python3 python3-docutils \
  libconst-fast-perl libdistro-info-perl libfile-basedir-perl libfile-libmagic-perl \
  libipc-run3-perl libpath-tiny-perl libunicode-utf8-perl libcrypt-dev file
```

3. 安装 `Package: lintian` 的运行依赖，共 76 个，从 `debian/control` 的 Depends 提取后一次性安装：

```bash
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $(cat /work/build/rtdeps.txt)
```

另装文档站点生成依赖：

```bash
apt-get install -y --no-install-recommends lintian-ssg
```

`default-jdk-headless` 是 Build-Depends 里唯一未带 `<!nocheck>` 的项，但 binary 阶段不用 JDK，本次未安装，通过 `-d` 或 `nocheck` 绕过依赖核对。

4. 放宽 `debian/control` 中高于 loongnix 底库的版本要求：

```text
debhelper-compat (= 14)  ->  debhelper-compat (= 13)
dpkg (>= 1.23.4)         ->  dpkg (>= 1.22.0)
libdistro-info-perl (>= 1.16~) -> libdistro-info-perl
```

5. 为 loongnix 源内 `libdistro-info-perl 1.13` 缺 `get_all_series` 增加兼容 shim：

- 新增 `lib/Lintian/DistroInfo.pm`。
- 修改 `lib/Lintian/Data/JoinedLines.pm:29`、`lib/Lintian/Check/Debian/Changelog.pm:46,145`。
- 逻辑：若 `Debian::DistroInfo->can('get_all_series')` 可用则委托上游，否则回退读取 `/usr/share/distro-info/<distro>.csv` 的 series 列。

6. 最终构建命令：

```bash
cd /work/src
export DEB_BUILD_OPTIONS="nocheck parallel=8"
fakeroot debian/rules binary
```

产物落在源码目录上级，形如：

```text
lintian_2.144.1_all.deb
lintian-doc_2.144.1_all.deb
```

7. 生成 `.changes`：

```bash
dpkg-genchanges -b
```

## 踩坑点

1. 上游 Build-Depends 高于 loongnix 底库。

症状：

```text
dpkg-checkbuilddeps: error: unmet build dependencies: debhelper-compat (= 14) dpkg (>= 1.23.4) libdistro-info-perl (>= 1.16~) lintian lintian-ssg ...
```

根因：源码是 sid/forky WIP 的 lintian 2.144.1，loongnix 25 对应 debhelper 13、dpkg 1.22。

规避：按上面步骤修改 `debian/control` 的 compat、dpkg、libdistro-info-perl 版本要求；`lintian` 自身在 loongnix 无安装候选，用 `-d` / `nocheck` 绕过。

2. `nocheck` 后文档生成缺 `List::SomeUtils.pm`。

症状：

```text
Can't locate List/SomeUtils.pm in @INC ... at lib/Lintian/Util.pm line 68
make: *** [debian/rules:62: generate-docs-stamp] Error 2
```

根因：`List::SomeUtils` 在 Build-Depends 里带 `<!nocheck>`，被 `nocheck` 过滤后未安装，但文档生成脚本会 `use Lintian::Util` 用到它。

规避：安装 `Package: lintian` 的 76 个运行依赖。

3. `DEB_BUILD_OPTIONS=nodoc` 会导致产物缺 man 页。

症状：

```text
dpkg-deb -c lintian_*.deb | grep -c 'man[13]/' = 2
```

根因：`dh_installman` 中 `nodoc` 会跳过安装 man 页。

规避：不要使用 `nodoc`，只保留 `nocheck`：

```bash
export DEB_BUILD_OPTIONS="nocheck parallel=8"
```

最终 deb 内 man1 5 个、man3 121 个，合计 126 个：

```bash
dpkg -L lintian | grep -cE '/man[13]/.+'
```

4. 容器内核限制导致 `chmod -R` 失败。

症状：

```text
chmod: changing permissions of 'debian/lintian/usr/share/doc/lintian/api.html/index.html': Operation not permitted
dh_installdocs: error: chmod -R u\+rw,go=rX debian/lintian/usr/share/doc returned exit code 1
```

验证阶段也会遇到：

```text
tar: ./usr/bin/lintian: Cannot change mode to rwxr-xr-x: Operation not permitted
```

根因：本机内核未放行 `AT_SYMLINK_NOFOLLOW` 的 `fchmodat` 与 `fchmodat2`；单条 `chmod` 可成功，递归 `chmod -R` 会失败。

规避：显式使用：

```bash
fakeroot debian/rules binary
```

注意：`dpkg-buildpackage -rfakeroot` 不解决该问题，因为它只把 fakeroot 用在 `debian/rules clean`，`debian/rules binary` 仍会裸跑。

验证阶段如仍遇到 tar 的 chmod EPERM，可用：

```bash
fakeroot lintian
```

5. `libdistro-info-perl 1.13` 缺 `get_all_series`。

症状：

```text
Attempt to call undefined import method with arguments ("get_all_series") via package "Debian::DistroInfo" (Perhaps you forgot to load the package?) at /usr/share/lintian/bin/../lib/Lintian/Data/JoinedLines.pm line 29.
```

进一步表现为：

```text
Undefined subroutine &Lintian::Data::JoinedLines::get_all_series ... line 297
Warning ... cannot run fields/distribution check
skipping check of changes:...
```

根因：loongnix 源内 `libdistro-info-perl` 最高为 1.13，`get_all_series` 是 1.16 才加入的能力。

规避：保留 `libdistro-info-perl` 无版本界依赖，并通过 `lib/Lintian/DistroInfo.pm` shim 回退读取 `/usr/share/distro-info/<distro>.csv`。

6. 文档 stamp 陈旧导致 man 页未重新生成。

症状：修改后重打包，但 `generate-docs-stamp` 未触发重新生成 man 页。

规避：

```bash
rm -f debian/debhelper-build-stamp
fakeroot debian/rules binary
```

## 验证方法

验收口径为基本三件套，完整测试套件非必需；本次构建使用 `nocheck` 跳过 `private/runtests`。

1. 干净环境装产物：

将交付的 deb 包安装到干净 Loongnix 环境：

```text
lintian_2.144.1_all.deb
lintian-doc_2.144.1_all.deb
```

可用 `dpkg-deb -f` 核对包名与版本：

```bash
dpkg-deb -f /work/output/lintian_2.144.1_all.deb Package Version
dpkg-deb -f /work/output/lintian-doc_2.144.1_all.deb Package Version
```

2. 导入 / 加载：

```bash
lintian --version
```

期望输出：

```text
Lintian v2.144.1
```

3. 一次最小调用：

```bash
timeout 200 fakeroot lintian --allow-root /work/output/lintian-doc_2.144.1_all.deb
```

本次实测 `final_rc=0`，三件套验证 `RESULT pass=13 fail=0`。