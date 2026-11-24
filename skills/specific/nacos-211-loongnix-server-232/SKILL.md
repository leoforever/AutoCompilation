---
name: nacos-211-loongnix-server-232
type: layer2-build
allowed-tools: [execute, read_file, edit_file, write_file]
description: |
  Nacos 2.1.1 在 Loongnix Server 23.2 (loongarch64, rpm 系) 的源码构建与打包全流程。
  以下场景读这条：编译 nacos、nacos-server.jar 启动崩
  can't load AMD 64-bit .so on a LoongArch-bit platform（rocksdb JNI）、
  mvn 报 unknown os.arch loongarch64、grpc/protoc 坐标取不到本架构件、
  Loongnix dnf 的 mvn 报 maven@{maven_version_suffix}.conf、nacos standalone
  验证配置读写/服务注册 REST。升级版本（2.2+/3.x）先重读版本矩阵。
---

# nacos 2.1.1 @ Loongnix Server 23.2（实测 T-20261124154526-3B2E8F，success，pass=10）

## 简要场景

- 软件：alibaba/nacos 2.1.1（tag `2.1.1`，HEAD 41859be），Java 25 模块 Maven 工程。
- 系统：Loongnix Server 23.2，rpm 交付；镜像 lcr.loongnix.cn/loongnix-server/loongnix-server:23.2。
- 全程 1h39m（大头：rocksdb JNI 自建+两返工约 30min）；源码改动仅 2 处 pom（见"源码改动"）。
- 通用件指针：原生件自建与 Maven 仓接线全部走 `generic/java-native-jni`；
  protoc/grpc 插件本体见 `generic/protobuf-loongarch`；打包纪律见 `generic/rpm-packaging`。
  本篇只留 nacos 特有口径与版本矩阵。

## 版本矩阵（本机实测装出来的组合）

| 件 | 版本 | 来源 |
|---|---|---|
| java | 1.8.0_472（Loongson 8.1.25，**Server VM** 非 Zero）| dnf java-1.8.0-openjdk-devel |
| maven | 3.9.9 | apache-maven tarball（系统 maven 包有占位符缺陷，见 java-native-jni 附节）|
| protoc | 3.16.1 自建 | protobuf-cpp-3.16.1 tar，cmake 入口在 **cmake/** 子目录 |
| protoc-gen-grpc-java | 1.30.2 自建 | grpc-java v1.30.2 源码 java_plugin.cpp 直链 |
| rocksdbjni | 5.18.4 自建 loongarch .so + Environment.java 补丁 | 见 java-native-jni 第三步 |
| os-maven-plugin | 1.5.0.Final → **1.7.1**（源码改动 2 处）| consistency/istio pom |
| 系统辅助 | gcc 12.3.0 / cmake 3.30.5 / glibc 2.38 / protobuf-devel 3.19.6（仅取库，protoc 不用它）| dnf |

## 步骤骨架（细节在通用篇，这里只留 nacos 特有）

1. dnf 装 java-1.8.0-openjdk-devel / rpm-build / gcc-c++ / cmake / 压缩库 devel；
   mvn 用 apache tarball。
2. 按 java-native-jni 第一步：os-maven-plugin 1.7.1（grep 全仓，2 处）→ classifier
   `linux-loongarch_64` 出得来再往下。
3. protoc 3.16.1 + grpc 插件自建（protobuf-loongarch 3/3b），按坐标摆 ~/.m2
   （注意删 _remote.repositories 和 .sha1）。
4. 主构建（上游 BUILDING 口径 + 关校验）：
   `mvn -B -Prelease-nacos -Dmaven.test.skip=true -Dcheckstyle.skip=true -Dpmd.skip=true -Drat.skip=true clean install`
   → distribution/target/nacos-server-2.1.1.tar.gz|zip。
5. rocksdb 自建替换（java-native-jni 第三步）→ 重打 `mvn -o -pl console,distribution clean install`
   → 确认 fat jar 内 rocksdbjni 里是 librocksdbjni-linux-loongarch64.so。
6. standalone 起服验证（下节口径）→ 自写 nacos.spec 打 rpm（装 /opt/nacos，
   %install 解包、%post 建 nacos 用户，systemd unit 直启 java）。

## 验证口径（verify.sh 三件套 + rpm，实测 pass=10 fail=0）

- 装：解 tar.gz → nacos/{bin,conf,target} + nacos-server.jar 就位。
- 加载：MANIFEST Main-Class=PropertiesLauncher；嵌套 nacos-naming jar 计数；
  **嵌套 rocksdbjni jar 内 loongarch64 .so 计数=1**（本架构原生件断言，核心）。
- 最小调用：`startup.sh -m standalone` → `Nacos started successfully in stand alone
  mode`；GET /nacos/=200；POST config → true，**回读要重试循环 ≤8×2s**（derby 落盘
  延迟 2-4s，秒查必假 fail）+ content 带 JSON 必须 `--data-urlencode`；
  服务注册 POST /v1/ns/instance → ok；raft 三组（naming_instance_metadata /
  naming_persistent_service_v2 / naming_service_metadata）become leader 于 7848；
  9848 grpc server started；start.out ERROR 计数=0。
- rpm：`rpm -ivh --nodeps --noscripts --root=<暂存根> --dbpath=<宿主 /usr/lib/sysimage/rpm 副本>`
  → rpm -ql 20 条含 /opt/nacos/target/nacos-server.jar + nacos.service。
  （暂存根无 /bin/sh 故 --noscripts；%post 逻辑以构建机等价手工命令实测覆盖，
  **升级 23.2 真机时要补 scriptlet 真装**——当前是已知缺口不是已验证项。）

## 源码改动清单（source-changes.patch 缘由，基线 41859be6）

| 文件 | 改动 | 理由 |
|---|---|---|
| consistency/pom.xml | os-maven-plugin 1.5.0.Final→1.7.1 | unknown os.arch；classifier 只影响取件坐标，最小侵入 |
| istio/pom.xml | 同上 | 两处必须一致，漏一模块照炸 |

非源码适配件（复现必须一并带，均已在本地仓/产物内）：自建 protoc / grpc 插件 /
loongarch 版 rocksdbjni jar（含 Environment.java 补丁 class）。

## 已知限制（诚实分级）

1. console-ui 未重编（用仓库已提交静态资源，上游 release-nacos 口径不跑 npm）。
2. grpc 走纯 Java NIO：grpc-netty-shaded 内 epoll/tcnative 仅 x86 → netty 探测
   失败回落。功能可用（9848 起、REST 全通），高并发吞吐未测，预期低于原生 epoll。
3. jraft 的 JNA 路径未覆盖：LibC（jna 5.5.0）无 loongarch libjnidispatch；
   standalone 实测不触发 madvise 分支（日志 0 命中），集群日志段预热路径可能触发
   UnsatisfiedLinkError——未验证。
4. istio/MCP、cluster 模式（多节点 raft）、MySQL 外置存储均未端到端验证；
   验收口径 = standalone + 内嵌 derby。
5. rpm 尾巴 .lns23 跟随构建镜像；未跑 mvn test 全量套件（-DWITH_UNIT_TESTS 口径同
   上游 release 说明）；rocksdb JNI -O1 非性能最优。

## 晋升货架件（已入 /work/cache_out 带 meta）

nacos rpm 本体、librocksdbjni-linux-loongarch64.so、rocksdbjni-5.18.4-loongarch64.jar、
protoc-3.16.1-loongarch64、protoc-gen-grpc-java-1.30.2-loongarch64
——下一个 Java 中间件单直接复用（jar 的晋升 kind 白名单 11-24 已放开）。
