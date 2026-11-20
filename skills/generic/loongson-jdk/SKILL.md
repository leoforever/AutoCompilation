---
name: loongson-jdk
type: layer1-infra
allowed-tools: [execute, read_file]
description: |
  龙芯 Java 工具链选择与下载：Loongson JDK 25 两种打包（普通版 vs glibc2.34 版）差异与
  陷阱，apt openjdk-25 的 Zero VM 性能提示。以下场景读这条：下载 JDK、java/gradle/maven
  项目构建、报 cannot execute required file not found、Loongson JDK、Zero VM、
  interpreted mode 慢、需要 JDK 25。
---

# 龙芯 JDK 选择与下载（loongarch64）

## 简介（是什么）
龙芯上跑 Java 大项目（Ghidra/elasticsearch/gradle maven 工程）需要 JDK 时，
两条路：apt 装 openjdk-25（简单但是 Zero VM），或下载 Loongson JDK 25（Server VM，
性能正常——**但必须选对 glibc2.34 版**）。以下事实均来自 Ghidra 任务 T-20261119145756-A0FC37 容器实测。

## 下载与使用
1. **Loongson JDK 25（推荐，Server VM）**——下载页 `https://www.loongnix.cn/zh/api/java/downloads-jdk25/`，
   同目录两种包：
   ```bash
   # glibc2.34 版（✅ 选这个）
   wget https://ftp.loongnix.cn/Java/openjdk25/loongson25.5.22-fx-jdk25.0.4_7-linux-loongarch64-glibc2.34.tar.gz
   tar xzf loongson25.5.22-fx-jdk25.0.4_7-linux-loongarch64-glibc2.34.tar.gz -C jdk/
   jdk/jdk-25.0.4/bin/java -version   # openjdk 25.0.4, Server VM, mixed mode
   ```
   验收两条：`java -version` 出 **Server VM**（不是 Zero）；
   `java -XshowSettings:properties -version` 里 **os.arch = loongarch64**。

2. **apt 版（兜底/CI 用）**：`openjdk-25-jdk-headless`（实测 Candidate 25~32ea-1+b2）。
   它可用但为 **Zero VM（interpreted mode）**——能跑、正确性没问题、性能差一截；
   构建期编译（javac）不受影响，重活（gradle 全量编译/大型分析跑）才感知差距。

## 注意事项（真实踩坑）
1. **普通版 Loongson JDK 在 loongnix-25.1-rc1 容器里起不来**：
   `bin/java: cannot execute: required file not found`。
   根因：普通版解释器硬编码 `/lib64/ld.so.1`，该镜像里不存在——
   loongarch 的 loader 实名是 `/lib64/ld-linux-loongarch-lp64d.so.1`（glibc 2.34+ 规范）。
   解法就是直接用 glibc2.34 版；**别用 symlink 骗 loader**（治标，换镜像复发）。
2. **下载前一条命令自检解释器**（省一次 300MB 白下）：
   ```bash
   readelf -l <pkg>/bin/java | grep Requesting
   # 出现 /lib64/ld.so.1 = 旧 ABI 构建，本容器不可用；lp64d 全名 = 可用
   ```
   拿不到 bin/java 时可 `strings <pkg>/bin/java | grep ld-` 预查。
3. 解包时 `tar: Cannot change mode ...: Operation not permitted` 大量告警是
   容器内核限制，树内容与权限实际正确（`ls -la` 抽查 + 能跑即可放行），不是错误。
4. JDK 路径用法：`export JAVA_HOME=<解包路径>/jdk-25.0.4`，PATH 带 `$JAVA_HOME/bin`；
   已装应用找 JDK 走 `launch.properties` 里 `JAVA_HOME_OVERRIDE` 类配置（具体看各项目）。

## 不要做
- 不要下载普通版再想办法"修"它——glibc2.34 版就是同一发布版的正解构建；
- 不要在需要性能的验证/压测环节默认用 apt Zero VM 而不自查——`java -version` 第二行见真章；
- 不要凭包名猜 glibc 适配，readelf 一条命令的事，别赌。
