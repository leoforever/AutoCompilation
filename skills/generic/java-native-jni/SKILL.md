---
name: java-native-jni
type: layer1-infra
allowed-tools: [execute, read_file, edit_file]
description: |
  Java/Maven 工程内嵌原生件（protoc、grpc 插件、rocksdb JNI、epoll/tcnative 类）
  在龙芯 loongarch64 无预编译件时的通杀流程。以下场景读这条：
  mvn 构建报 unknown os.arch: loongarch64（os-maven-plugin 1.5.x）、
  os.detected.classifier 取不到 linux-loongarch_64、fat jar 启动报
  can't load AMD 64-bit .so on a LoongArch-bit platform、
  UnsatisfiedLinkError librocksdbjni-linux64.so、自建原生件要按 Maven 坐标
  喂进本地仓库（install-file -Dpackaging=exe 落盘名毁扩展名）、
  Loongnix dnf 的 mvn 启动脚本报 maven@{maven_version_suffix}.conf 不存在。
---

# Java 工程自带原生件在 loongarch64 的自建与接线（nacos 案 T-20261124154526-3B2E8F 全链实测）

## 判定开场（30 秒决定走不走这条路）

```bash
ls /proc/sys/fs/binfmt_misc    # 空目录 = 无 qemu binfmt，
                               # Maven 里 x86 classifier 的 .exe 原生件全部不可用
```
binfmt 空 + 构建要取 `protoc/...-linux-x86_64.exe` 类坐标 → 必走本流程，别浪费时间
下载"试试看"。

## 第一步：classifier 打通（os-maven-plugin）

- **症状**：`[ERROR] unknown os.arch: loongarch64`（插件解析直接炸）。
- **根因**：os-maven-plugin 1.5.x 架构映射表无 loongarch64。
- **修法**：全项目搜 `kr.motd.maven:os-maven-plugin`，版本升 **1.7.1**
  （实测输出 `os.detected.classifier=linux-loongarch_64`）。注意每个用它的
  pom 都要一致升（nacos 是 consistency/istio 两处），漏一处那模块照炸。
- 验证探针：`mvn help:evaluate -Dexpression=os.detected.classifier -DforceStdout`。

## 第二步：自建原生件按 Maven 坐标摆本地仓（关键细节）

坐标规则：`linux-loongarch_64` classifier（注意是下划线 loongarch_64，
不是 loongarch64——那是 os.name 侧）。以 protoc 为例：

```
~/.m2/repository/com/google/protobuf/protoc/3.16.1/
  protoc-3.16.1-linux-loongarch_64.exe   ← 自建 ELF，改名就位
  protoc-3.16.1.pom                      ← 手写最小 pom（packaging=exe）
```
- **别用 `install:install-file -Dpackaging=exe`**：实测落盘名变成
  `...-linux-loongarch_64.0`（扩展名毁成 .0），xolstice 插件按 .exe 找必失败。
  手写 pom + 按约定命名摆放最干净。
- 摆完**删掉 `_remote.repositories` 和 `.sha1`**，否则 Maven 要回源校验，
  报 checksum/absent 类错。
- protoc 本体自建方法见 `generic/protobuf-loongarch`（老版入口在 cmake/ 子目录）；
  grpc-java 插件直链 java_plugin.cpp 同见该篇 3b。

## 第三步：fat jar 内嵌 JNI 库缺本架构（rocksdb 模板流程）

- **症状**：服务启动崩 `UnsatisfiedLinkError: can't load AMD 64-bit .so on a
  LoongArch-bit platform`（rocksdbjni jar 只带 x86/arm/ppc64le/osx/win）。
- 上游 Maven 最新版（11.x）同样无 loongarch .so——换版本无用，只能自建。
- 自建五关（rocksdb 5.18.4 + gcc12 实测全过，参数可抄）：
  1. cmake 主库：`-DWITH_JNI=0`（java 子项目会 file(DOWNLOAD) 拉 junit 且用
     已废弃的 create_javah，别碰），`-DFAIL_ON_WARNINGS=OFF`（gcc12 的
     deprecated-copy/array-bounds 全是 -Werror 致命），
     `-DCMAKE_CXX_FLAGS="-O1 -Wno-error -Wno-deprecated-copy -Wno-clobbered"`，
     **`-DCMAKE_POSITION_INDEPENDENT_CODE=ON` 必开**——静态库进共享库，
     不开 PIC 链接报 `relocation R_LARCH_B26 right shift 2 error`
     （photocraft 案同类症状的解是换 lld；那是可执行链接场景，本场景正解是 PIC）。
  2. JNI 层手工：javac 全 main 源码（含打过架构补丁的 Environment.java，见 4）
     → JDK8 的 `javah` 出头 → `g++ -std=c++11 -O1 -fPIC -shared` 链全部
     rocksjni .cc + librocksdb.a + 压缩系统库（-lsnappy -llz4 -lzstd -lbz2 -lz）。
  3. include 路径两个都要：`-I<java>/include`（rocksjni/portal.h）与
     `-I<自建头目录>`（org_rocksdb_*.h）；defines 补
     `-DROCKSDB_PLATFORM_POSIX -DOS_LINUX -DROCKSDB_SUPPORT_THREAD_LOCAL`。
  4. **Environment.java 架构补丁**：新增 `isLoongArch64()`
     （ARCH.contains("loongarch64")），并把 `getJniLibraryName()` 里
     `if(isPowerPC() || isAarch64())` 扩成 `if(isPowerPC() || isAarch64() || isLoongArch64())`
     ——让资源名落到 `librocksdbjni-linux-loongarch64.so`（与上游 aarch64 命名
     规则一致）而不是掉进 is64Bit() 分支请求 x86 的 linux64.so。
  5. `rocksjni/{native_comparator_wrapper_test,rocksdb_exception_test,write_batch_test}.cc`
     依赖 test 源码树的 javah 头——这 3 个 .cc 不参与链接（main 功能用不到）。
  - .so 链接若报 `undefined symbol: _ZTIN7rocksdb6LoggerE`（typeinfo）：
    rocksdb 主库 -fno-rtti 编的而 JNI 层默认开 rtti → g++ 命令加 `-fno-rtti`
    （动手前先 `grep -rn dynamic_cast java/rocksjni/` 确认为空，实测为空，安全）。
- 组 jar：原 jar 里 `librocksdbjni-*.so` 换成本架构 .so + 替换打过补丁的
  `org/rocksdb/util/Environment.class`，覆盖本地仓坐标 → 重编依赖它的模块
  （`mvn -o -pl <模块> clean install`）。
- 产物 .so/.jar 放 cache_out 晋升（meta 记 gcc/glibc/java 版本），下单一分钟复用。

## 附：Loongnix 23.2 系统 maven 包自带缺陷

`dnf install maven` 后 `mvn -v` 报
`/usr/bin/mvn: line 38: /etc/java/maven@{maven_version_suffix}.conf: No such file or directory`
——该发行版打包时启动脚本占位符没替换。别改系统包：取 apache-maven 官方
tarball（dlcdn 可能回 HTML 错误页，用 archive.apache.org 稳），export JAVA_HOME
后用其 bin/mvn。

## 不要做
- 不要给 x86 .exe 找 qemu 后路（binfmt 空就判死，本流程 20 分钟内走完）；
- 不要换依赖版本赌"新版有 loongarch .so"（rocksdb 11.x 实测仍没有）；
- 不要 install-file 摆 exe 类件（扩展名毁 .0）；
- 不要只补一处 os-maven-plugin（多模块必全量 grep）。
