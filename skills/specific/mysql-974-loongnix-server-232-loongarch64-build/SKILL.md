---
name: mysql-974-loongnix-server-232-loongarch64-build
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  MySQL 9.7.4 源码构建与 RPM 打包流程：龙芯 Loongnix Server 23.2 / openEuler SP2、loongarch64。编译 mysql-9.7.4、cmake configure、ninja install、rpmbuild、缺 rpcgen/systemd-devel/libudev 时读这条；mysqld 启动报 MY-010455/bootstrap、验证断言 user.ibd 失败、semisync INSTALL PLUGIN 报 ERROR 1127、%files 两万五文件清单、--short-circuit 跳 %install、audit-libs-devel 找不到时读"11-24 二次实测增量"节。
---

> 通用件指针：configure/config.guess 类先查 `generic/autotools-cross-loongarch`；cmake 见 `generic/cmake-cpp`。本篇只留 mysql 布局与 rpmbuild 参数。
## 简要场景

- 软件：MySQL 9.7.4，源码分支 `mysql-9.7.4`，`MYSQL_VERSION` 为 `9.7.4`，Maturity 为 `LTS`。
- 系统：Loongnix Server 23.2，`ID=loongnix-server`，`PLATFORM_ID=platform:lns23`，包格式为 rpm。
- 架构：loongarch64。
- 最终结果：success。
- 产物：7 个 `.rpm` 加 spec，约 408M；`ninja -j 32` 完成 `4654/4654`，rc=0，零 `error:`；验证最终 `RESULT pass=25 fail=0`。
- 关键环境事实：
  - 开工时容器内无 `gcc` / `g++` / `cmake` / `ninja` / `bison` / `flex` / `openssl`，全部由 `dnf` 新装。
  - 编译器为 `gcc` / `g++` 12.3.0，GNU ld 2.41，glibc 2.38，openssl 3.0.12。
  - `CMakeLists.txt:521` 设置 `CMAKE_CXX_STANDARD 23`；`cmake/os/Linux.cmake` 要求 GCC ≥ 11，本次 12.3.0 满足，不需要 `FORCE_UNSUPPORTED_COMPILER`。
  - 源码树自带全套 bundled 第三方库，包括 boost 1.87.0、protobuf 24.4、abseil、ICU 77.1、lz4、zstd、zlib、xxhash、rapidjson、nlohmann-json、googletest、libedit、libcbor、libfido2、libbacktrace、opentelemetry 等；无 git submodule，无需外部下载。

## 构建步骤

### 1. 安装工具链和系统依赖

本次全程使用 Loongnix Server 23.2 的第一级 `dnf` 系统包，未使用 pip，也未复用货架中的 rust / numpy / torch / libtorch。

```bash
# 工具链
dnf install -y git patch wget curl make ninja-build gcc gcc-c++ cmake \
    python3-devel openssl-devel ncurses-devel readline-devel libaio-devel \
    zlib-devel libstdc++-devel bison flex perl perl-Getopt-Long \
    perl-Data-Dumper perl-IPC-Cmd

# MySQL 依赖库
dnf install -y libcurl-devel libevent-devel numactl-devel libtirpc-devel \
    pam-devel audit-libs-devel

# WebAuthn/FIDO：bundled libfido2 需要 libudev.h
dnf install -y systemd-devel

# group_replication 的 xcom 需要 rpcgen 生成 xdr
dnf install -y rpcgen
```

实测可用版本包括：

- `gcc` / `g++`：12.3.0-10
- `cmake`：3.30.5
- `ninja`：1.11.1
- `bison`：3.8.2
- `flex`：2.6.4
- `openssl-devel`：3.0.12
- `libcurl-devel`：8.4.0
- `libtirpc-devel`：1.3.4
- `libevent-devel`：2.1.12
- `numactl-devel`：2.0.16
- `systemd-devel`：255-5，Loongnix 上通过它提供 `libudev-devel`
- `rpcgen`：rpcsvc-proto 1.4.3
- `pam-devel`：1.5.3
- `libfido2-devel`：1.14.0
- `libcbor-devel`：0.10.2
- `audit-libs-devel`：3.1.2

说明：`libfido2-devel` / `libcbor-devel` 可安装，但本次最终走 bundled 路径。

### 2. CMake configure

最终可用的 configure 命令行如下。注意其中包含 `-DWITH_SYSTEMD=1`；第一轮未加时会导致 systemd 相关文件缺失，第二轮 configure 加入后增量 install 成功。

```bash
cmake ../ -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DINSTALL_LAYOUT=RPM -DCMAKE_INSTALL_PREFIX=/usr -DSYSCONFDIR=/etc \
  -DMYSQL_DATADIR=/var/lib/mysql \
  -DWITH_SSL=system -DWITH_ZLIB=bundled -DWITH_PROTOBUF=bundled -DWITH_ICU=bundled \
  -DWITH_LZ4=bundled -DWITH_ZSTD=bundled -DWITH_CURL=system -DWITH_NUMA=1 \
  -DWITH_SYSTEMD=1 -DWITH_UNIT_TESTS=OFF -DWITH_NDB=OFF -DWITH_ROUTER=1 \
  -DWITH_DEBUG=OFF -DMYSQL_MAINTAINER_MODE=OFF \
  '-DCOMPILATION_COMMENT=MySQL Community - GPL (loongarch64)' \
  '-DCOMPILATION_COMMENT_SERVER=MySQL Community Server - GPL (loongarch64)'
```

configure 过程中实际出现过：

- 第 1 次失败：缺 `libudev.h`。
- 第 2 次失败：安装 `systemd-devel` 后原地重跑仍失败，原因是 CMake cache 粘住了旧探测结果。
- 第 3 次失败：缺 `rpcgen`。
- 第 4 次成功：`Configuring done` / `Generating done`，rc=0。

### 3. 编译

```bash
ninja -j 32
```

本次实测：

- `4654/4654` 全部成功。
- rc=0。
- 零 `error:`。

### 4. 安装到 DESTDIR

```bash
DESTDIR=/work/build/image2 ninja install
```

本次实测安装产物约 25498 个文件 / 3.3G，未 strip。

### 5. 打 RPM

本次 RPM 交付不是简单打包安装树，而是 `rpmbuild -bb` 从源码完整重建。

关键步骤：

1. 使用 `git archive` 生成 `mysql-9.7.4.tar.gz`。
   - 本次实测 tarball 约 441M，62478 文件，包含 bundled 三方库。
2. 编写 spec。
   - 前缀为 `/usr`。
   - 使用 `INSTALL_LAYOUT=RPM`。
   - 分包布局参考上游 `packaging/rpm-oel/mysql.spec.in` 的 common / libs / client / server / devel / test / router 布局。
   - 上游 spec 依赖 `%{?rhel}` 条件块；Loongnix 上 `rhel` 未定义，直接复用会走 el7 老路径，因此本次重写为无 rhel 条件的精简 spec。
3. 执行：

```bash
rpmbuild -bb
```

最终收 7 个 `.rpm` 加 spec：

- `mysql-community-client-9.7.4-1.lns23.loongarch64.rpm`
- `mysql-community-common-9.7.4-1.lns23.loongarch64.rpm`
- `mysql-community-devel-9.7.4-1.lns23.loongarch64.rpm`
- `mysql-community-libs-9.7.4-1.lns23.loongarch64.rpm`
- `mysql-community-router-9.7.4-1.lns23.loongarch64.rpm`
- `mysql-community-server-9.7.4-1.lns23.loongarch64.rpm`
- `mysql-community-test-9.7.4-1.lns23.loongarch64.rpm`

## 踩坑点

### 1. configure 报 WebAuthn 缺库：缺 `libudev.h`

原文错误：

```text
CMake Error at CMakeLists.txt:2131: -DWITH_AUTHENTICATION_WEBAUTHN=ON, but missing required libraries
```

伴随：

```text
Looking for libudev.h - not found
```

根因：

- `WITH_FIDO` 默认走 `bundled`。
- bundled libfido2 在 Linux 上硬依赖 `libudev.h` 和 `libudev.so`。
- `cmake/fido2.cmake` 会执行 `FIND_SYSTEM_UDEV_OR_HID()`；找不到时 `MYSQL_USE_BUNDLED_FIDO()` 会把 `FIDO_FOUND` 置 FALSE 并触发 FATAL_ERROR。

规避：

```bash
dnf install -y systemd-devel
```

Loongnix Server 23.2 上 `systemd-devel` 提供 `libudev-devel`。

### 2. 安装 `systemd-devel` 后原地重跑 cmake，报错一字不差

症状：

安装 `systemd-devel` 后直接重跑 cmake，仍出现：

```text
CMake Error at CMakeLists.txt:2131: -DWITH_AUTHENTICATION_WEBAUTHN=ON, but missing required libraries
```

根因：

- `LIBUDEV_DEVEL_FOUND` 是 `SET(... CACHE INTERNAL "")`。
- 上一轮没找到时，`FIND_LIBRARY` / `CHECK_INCLUDE_FILE` 的空结果被 `CMakeCache.txt` 粘住。
- 只装依赖不清 cache 无效。

规避：

删除 build 目录后重新 configure。

```bash
rm -rf build目录
```

教训：MySQL 这类 `CACHE INTERNAL` 探测结果，改依赖后必须清缓存。

### 3. configure 报缺 `rpcgen`

原文错误：

```text
CMake Error at plugin/group_replication/libmysqlgcs/cmake/rpcgen.cmake:124: Could not find rpcgen
```

根因：

- group replication 的 xcom 需要 `rpcgen` 生成 `xdr_gen/xcom_vp.h`。
- Loongnix Server 23.2 上 `rpcgen` 不在 `glibc-devel` 里，是独立包 `rpcgen`，由 rpcsvc-proto 提供。

规避：

```bash
dnf install -y rpcgen
```

### 4. `WITH_SYSTEMD` 默认 OFF 会导致 RPM 缺 systemd 文件

症状：

第一轮 configure 未开启 `WITH_SYSTEMD` 时，RPM 布局会缺：

- `/usr/lib/systemd/system/mysqld.service`
- `mysqld_pre_systemd`

根因：

- `scripts/CMakeLists.txt:553` 附近 systemd 文件安装逻辑在 `IF(WITH_SYSTEMD)` 内。
- Loongnix Server 23.2 是 systemd 系统，RPM 交付中 `%post server` 使用 `%systemd_post`，因此必须开启 `WITH_SYSTEMD`。

规避：

第二轮 configure 加入：

```bash
-DWITH_SYSTEMD=1
```

实测 install 树只多 6 个文件：

- 3 个 systemd unit
- 2 个 tmpfiles
- `mysqld_pre_systemd`

同时少 6 个 SysV 文件，例如 `mysql.server` / `mysqld_multi` / `mysqld_multi.server` 与 3 个 man page。上游逻辑中二者互斥。

### 5. rpm 宏配置瑕疵：`macros.image-language-conf` 报错

原文错误：

```text
error: /etc/rpm/macros.image-language-conf: line 1: Macro %_install_langs has empty body
```

根因：

- 镜像里 `/etc/rpm/macros.image-language-conf` 写成 `%_install_langs `，body 为空。
- rpm 解析宏时报错。
- 属底库配置瑕疵，不影响 rpm / rpmbuild 功能，退出码仍为 0。

规避：

验证脚本里统一过滤：

```bash
grep -v macros.image-language-conf
```

不要修改 `/etc`。

注意：

- 不要试图用 `rpm --eval '%{...}'` 的多宏串写；`rpm --eval` 不解释 `\n`。

### 6. `rpmbuild` 的 `%install` 找不到 `my.cnf` 等文件

症状：

`rpmbuild` 的 `%install` 里类似命令找不到文件：

```text
install -D -m 0644 packaging/rpm-common/my.cnf ...
```

根因：

- `my.cnf`
- `mysql.logrotate`
- `mysqlrouter.conf`
- `Docs/INFO_BIN`

这些都是构建产物，由 `CONFIGURE_FILE` 生成在 build 目录，不在源码树中。本次 spec 的 build 目录是 `release/`。

规避：

spec 中路径改为：

```text
release/packaging/rpm-common/...
release/Docs/INFO_BIN
```

### 7. RPM 打包阶段出现 `File listed twice` 和 `Duplicate build-ids`

原文警告：

```text
warning: File listed twice: /usr/lib/mysqlrouter
```

以及大量：

```text
Duplicate build-ids
```

根因：

- `%dir %{_libdir}/mysql` 与 `%{_libdir}/mysql/plugin/` 目录重叠声明。
- `%{_libdir}/mysqlrouter/` 与 `%{_libdir}/mysqlrouter/private/` 目录重叠声明。
- `Duplicate build-ids` 是 server 与 router 各自带一份相同 abseil 静态转动态库，上游同样如此，属非致命警告。

规避：

- 去掉多余的 `%dir %{_libdir}/mysqlrouter`。
- `Duplicate build-ids` 警告可保留，不处理。

### 8. loongarch64 不在 MySQL 已知 64bit 架构列表，RPM 落到 `lib/`

事实：

loongarch64 不在 MySQL 的 `KNOWN_64BIT_ARCHITECTURES` 列表中。该列表包含：

```text
arm64 / aarch64 / ppc64 / ppc64le / s390x / x86_64
```

结果：

- RPM 布局自动落到 `lib/`，不是 `lib64/`。
- 本次 spec 按实测布局写：

```spec
%global _libdir /usr/lib
```

- 未强行改成 `lib64`。
- 这与 Loongnix 上 `%_lib=lib64` 的发行版默认不一致，已作为已知限制记录。

### 9. 验证脚本夹具期望值写错，不是产品缺陷

本次首跑验证：

```text
RESULT pass=22 fail=3
```

3 项均为脚本期望值写错：

- `semisync_master.so`：MySQL 8.0.26 起改名 `semisync_source.so`。
- `MAX(name)` 期望 `loongarch`：字母序应为 `nine`。
- `JSON_OBJECT` 期望 `"k":3.125`：实测为 `{"k": 1.500}`，冒号后带空格，且期望值本身抄错。

第二轮验证：

```text
RESULT pass=25 fail=0
```

## 验证方法

完整测试套件验证非必需。本次验收口径为基本三件套：

### 1. 干净环境安装 RPM

在干净 Loongnix Server 23.2 / loongarch64 环境中安装 `rpms/` 下的 MySQL RPM 包。

重点安装：

- `mysql-community-common`
- `mysql-community-libs`
- `mysql-community-client`
- `mysql-community-server`

本次最终交付为 7 个 rpm，验证通过。

### 2. 导入 / 加载检查

检查 MySQL 二进制可加载：

- `mysql` 版本报 `9.7.4`
- `mysqld` 版本报 `9.7.4`
- `ldd` 检查无 `not found`

本次实测均报 9.7.4，且 `ldd` 无 `not found`。

### 3. 一次最小调用

启动 `mysqld`，完成一次 SQL 往返，并执行一次 `mysqldump`。

本次实测真实调用出结果：

- 起 `mysqld`
- 完成 SQL 往返
- 完成 `mysqldump`

最终验证结果：

```text
RESULT pass=25 fail=0
```

## 11-24 二次实测增量（openEuler SP2 容器，success，pass=54）

### 10. datadir 断言：user.ibd 不存在是正常，别断言错
MySQL 8 起数据字典表统一进表空间 `$DD/mysql.ibd`（实测 27MB）；`mysql/`
子目录只剩 general_log.CSM/CSV、slow_log.* 等 CSV 引擎残留。验证脚本断言
`$DD/mysql/user.ibd` 必假失败，改断言 `mysql.ibd`。

### 11. 9.7 旧式 INSTALL PLUGIN 对 semisync_* 失效（上游行为，非移植缺陷）
`INSTALL PLUGIN semisync_source SONAME 'semisync_source.so'` →
`ERROR 1127 Can't find symbol 'semisync_source'`。根因：objdump -T 实测该
.so 只导出新式 `_mysql_plugin_declarations_[]`（plugin.h:157），无旧符号。
证明"动态装载能力"改用 `INSTALL COMPONENT 'file://component_log_sink_json'`
（实测 mysql.component 计数+1）；RUN.md 功能分级里如实标注 semisync 不可装。

### 12. %files 两万五文件的正解：Source1 清单生成器（本包交付物的一部分）
上游 spec %files 3200 行硬列不可照抄（File not found 与 unpackaged files
双向出错）。做法：`mysql-filelist-gen.py` 作 spec Source1，%install 末尾对
%{buildroot} 排除法分类出 7 份清单，spec 用 `%files <pkg> -f` 引用；内置
自检"清单合计 == buildroot 文件数 - 正文声明数"（实测 25497+1=25498）。
复用 spec 必须连脚本一起拿（Source1 已声明）。清单通配/转义细则见
`generic/rpm-packaging`（brp-compress 段）；文件名含 `#` 在 -f 清单无需转义（本篇实测）。

### 13. 服务包状态目录属主（首战已验证生效）
%post server 三行幂等掰正（见 generic/rpm-packaging 同名节）：
`install -d -m 0750 -o mysql -g mysql %{_localstatedir}/lib/mysql-files`
（keyring、/run/mysqld 同款）。目标机残留 mariadb 时该目录是 root:750，
不掰则 mysqld_pre_systemd 的 init-file 读不到 → MY-010455 → datadir
半成品 → [ -d datadir/mysql ] 让每次启动跳过重来=死循环。11-23 容器实锤，
11-24 本包按此处理后验证 systemd 全生命周期 PASS。

### 其他实测口径（省调试时间）
- openEuler 24.03 SP2 上开工先把 systemd-devel/rpcgen/audit-devel 装齐，
  configure **一次过**（攻略"连败三次"教训成立但不触发）。
- `%{?dist}` 在 openEuler 容器展开为空 → rpm 尾巴只有 `.loongarch64`，别等 .lns23。
- 128 核机 `ninja -j 64` 全量 14.5 分钟（-j32 基线是 32 核机）。
- 磁盘水位：编译树 23G+install 3.4G+rpmbuild 树，433G 盘峰值 88%——
  smoke 后删 b1/image/rpmbuild 三棵大树再跑正式 -bb（它自己重建）。
  快速迭代打包的 smoke spec 手法见 `generic/rpm-packaging`。
