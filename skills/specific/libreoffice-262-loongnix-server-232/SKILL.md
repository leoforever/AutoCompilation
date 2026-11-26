---
name: libreoffice-262-loongnix-server-232
type: layer2-build
allowed-tools: [execute, read_file, edit_file, write_file]
description: |
  LibreOffice 26.2.x 在 Loongnix Server 23.2 (loongarch64, rpm 交付) 的一次通过全流程。
  以下场景读这条：编译 libreoffice/LO、autogen 报 unrecognized options、
  make 报 used LinkTarget libfrmlo/libhelplinker not defined、链接 undefined inflate*、
  rpmbuild 卡 check-desktop-files、soffice --version 探 JDK、152 个外部 tarball 预取、
  libreoffice rpm 装完找不到 soffice.bin。换大版本（25.x/26.8+/7.x 老式命名）
  先重读"版本矩阵"节，补丁分版本钉/环境钉两类勿整块 apply。
---

# LibreOffice 26.2.7.3 @ Loongnix Server 23.2（实测 T-20261126093044-BB20F2，success，验证 9/9，全程 2h50m）

## 简要场景

- 软件：LibreOffice/core tag `libreoffice-26.2.7.3`（commit 08dfbf04d），源码 1.9GB。
- 系统：loongnix-server:23.2 镜像，32 核 254G；交付 31 个 rpm 共 168MB。
- 上游**原生支持 loongarch64**（configure.ac 有 LOONGARCH64 分支、自带
  config.guess 已认、skia 有 loong64 补丁）——不需要架构移植，纯构建工程。
- 耗时标尺：依赖安装 ~6min / tarball 预取 2min / configure 1.5min×5 /
  **make -j32 净 92min** / 验证 4min。墙钟预算 ≥4h。

## 照抄层：autogen.input 要点

```
--prefix=/usr --with-vendor=Loongnix --with-package-format=rpm --enable-epm
--with-lang=en-US --without-help
--enable-option-checking 由 autogen 固定 fatal——所有开关名必须
  ./autogen.sh --help 实测存在，凭记忆写 = configure 直接拒（坑2）
--enable-python=system          # 内部 CPython 无 loongarch64，必须系统 3.11.6
--with-jdk-home=/usr/lib/jvm/java-17-openjdk   # 裸 java 被 alternatives 指 1.8
--disable-skia --disable-pdfimport --disable-pdfium --disable-poppler
--disable-odbc 用 --with-system-odbc；--without-webdav（无 --disable-webdav）
--disable-scripting --disable-report-builder --disable-extensions(类) --disable-online-update
--disable-firebird-sdbc --disable-postgresql-sdbc --disable-mariadb-sdbc --disable-ldap
系统库开关只开"实测有 -devel + .pc"的 27 个（zlib/bzip2/expat/libpng/libtiff/
jpeg/lcms2/freetype/fontconfig/harfbuzz/graphite/hunspell/altlinuxhyph/icu/curl/
nss/openssl/cairo/epoxy/sqlite3/lzma/libwebp/boost/cppunit/libxml…）；
Loongnix 缺 devel 的（libcmis/redland/orcus/mdds/mythes/liblangtag/clucene…）
一律 internal 从 tarball 编——别硬开 system。
```

依赖两批 dnf（~330 包/400MB，清单见 BUILD_NOTES；关键提醒）：
镜像**不带 gcc**（先装 gcc-c++）；qt5 拆包（qtbase-devel 不含 QtX11Extras，
另装 qt5-qtx11extras-devel）；perl 主包不打包 Digest::MD5（另装 perl-Digest-MD5）；
clang 只有 clang-18 带后缀（LO 探不到就让它探不到，反正 skia/pdfium 已关）；
ant/icu(带 genbrk 工具)/meson/fakeroot 都要显式装。

## 补丁层（两类钉，**先判后打**）

```bash
# 通用前置：git apply --check source-changes.patch 先行；
# 但更按 hunk 判性质：

# ① 版本钉 26.2.x —— writerperfect 三 mk 加 zlib
#    （master 已修，26.8+ 不需要；打前 grep 判断，有就跳过）
grep -q zlib writerperfect/Library_wpftdraw.mk && echo "上游已修，跳过"
#    否则对 wpftdraw/wpftcalc/wpftimpress 三文件
#    gb_Library_use_externals 列表各加一行 `zlib \`
#    （wpftwriter 本来就有——对照兄弟 mk 即证是上游漏项）

# ② 环境钉 Loongnix —— sysui/CustomTarget_rpm.mk 的 rpmbuild 参数加
#    --define "__brp_check_desktop_files %nil"
#    与 LO 版本无关：只要 loongnix 镜像 + desktop 符号链接包就要打；
#    openEuler 不打（无此 BRP）。机理见 generic/rpm-packaging
#    "Loongnix 特有 BRP"节。
```

## 流程层（按序）

1. 子模块：`./g`/`make fetch` 的 submodule 流走不通——直接按 core 记录的
   commit 手工 `git clone --no-checkout ../help && git checkout <sha>`
   （helpcontent2 698MB、translations 193MB，与装依赖并行做省时间）。
   `git ls-tree HEAD` 查各子模块钉的 sha。
2. 外部件：写并发脚本按 download.lst 的 `X_SHA256SUM`+`X_TARBALL`
   抓 dev-www.libreoffice.org 的 src/ 与 extern/ 两域（URL 只拼
   "目录+文件名"，**裸目录 URL 会把 HTML 存成文件全数 BADSUM**——本案踩过），
   10 并发 152 件 830MB 约 2 分钟；然后
   `ln -sfn /work/tarballs $(grep TARFILE_LOCATION config_host.mk | 取路径)`
   （configure 默认指 external/tarballs，该目录不存在，软链复用免重下）。
3. configure：`./autogen.sh`（每次 ~1.5min）。**开关报错别硬记名字**，
   以 `./autogen.sh --help` 输出为准做 comm 校验，未知项 autogen 直接 fatal。
4. `make`（gbuild 自管 -j，自动并行 32；中断 3 次全部按补丁层/两阶段
   注册核对外，见 generic/autotools 三条纪律）。rpm 在 make 的
   `[build INS]` 阶段顺带产出，无独立打包步。
5. 产物：`workdir/installation/LibreOffice/rpm/.../RPMS/` 31 个 +
   `workdir/CustomTarget/sysui/rpm/libreoffice/*.noarch.rpm` 1 个（menus 包，
   在 CustomTarget 下不在 RPMS 下，别漏收）。

## 验收层

```bash
# 三件套（隔离 root，宿主机不真装）：
V=/tmp/loverify; mkdir -p $V/root $V/db; cp -a /usr/lib/sysimage/rpm/. $V/db
rpm -ivh --nodeps --root=$V/root --dbpath=$V/db \
  --define "_install_langs %_null" /work/output/*.rpm    # 31 个整组一起装！
ls $V/root/opt/libreoffice26.2/program/soffice.bin       # 主二进制在 libreoffice26.2 包
ldd .../soffice.bin | grep -c "not found"                 # 须为 0
timeout 120 instdir/program/soffice --headless --version  # 出 26.2.7.3 + commit
# 转换链：txt→pdf（%PDF- 头）、csv→xlsx（zip magic）、xlsx→csv 回读
```

- **包必须整组装**：包间 Requires 锁同版本；只装 ure 找不到 soffice.bin
  （它在主包 `libreoffice26.2` 里）。
- **AutoReqProv: no**：rpm 不声明 gtk3/qt5/icu 等系统库依赖，`rpm -ivh`
  不会自动拉——目标机用 `dnf -y install ./*.rpm` 或先按 RUN.md 装库。
- 运行期 soname 钉死：libicu.so.72 / boost 1.83 / nss 3.101 / gtk3 3.24 /
  qt5 5.15 / glib 2.78 —— 换发行版代际直接 ldd not found；货架 meta
  已记全 toolchain（复用判断照它核）。

## 版本矩阵（何时能照抄本篇）

```
26.2.x        本篇全量适用（补丁①②都要）
26.3+ /master 补丁①大概率已修（apply 前 grep 判断）；②照打（loongnix 上）
老 7.x/24.x   configure 选项名有漂移，autogen.input 别整抄，逐项 --help 校验
任何版本      墙钟 ≥4h；make 净时长 ~1.5h@32 核起
```

## 实测数据（供复用/预估）

- glibc 下限：soffice.bin 要求 GLIBC_2.36、库群 2.38（loongnix 23.2 基线）。
- instdir 572MB / workdir 峰值 4.0GB / 磁盘预算 ≥25GB 富余。
- LASX：CPU 有 lasx 标志、gcc 12.3 -mlasx 可用；默认未开（ENABLE_LASX 空），
  要性能加 `--enable-lasx` 重编（未实测提升幅度）。
- 裁剪面：无中文 UI/词典（仅 en-US）、无 PDF 导入、无 KF5/KF6/Qt6、
  无数据库驱动、无帮助文档——RUN.md ⑥ 节口径，交付说明里如实写。
