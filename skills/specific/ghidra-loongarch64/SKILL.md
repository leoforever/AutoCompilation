---
name: ghidra-loongarch64
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  Ghidra 源码构建（loongarch64 移植版）专属流程：Gradle 9.1 + Loongson JDK 25 +
  buildGhidra 分发 + analyzeHeadless 验收 + deb 组包。以下场景读这条：编译 Ghidra、
  ghidraRun/analyzeHeadless、GhidraScript 验证、Ghidra 扩展打包、Ghidra 新版本复编。
  通用件指针：JDK 选择见 generic/loongson-jdk；平台枚举移植套路见
  generic/gradle-native-porting；protoc 自建见 generic/protobuf-loongarch；
  deb 组包见 generic/debian-packaging；长构建/验证纪律见系统提示与
  generic/build-notes-standard。
---

# Ghidra 龙芯构建（专属知识，通用坑已外指）

## 简要场景
- 软件：Ghidra 12.3 (DEV)，https://github.com/NationalSecurityAgency/ghidra
- 系统：loongnix-25.1-rc1（deb 系），任务实测全程 3.1h，同版本复编预期 ~1h
- 产物：ghidra_12.3-1_loong64.deb + 上游 dist zip（内含 5 个 LoongArch 原生件：
  decompile/sleigh/lzfse/demangler_gnu_v2_24/v2_41）

## 构建步骤（Ghidra 特有参数）
1. 依赖：`apt-get install openjdk-25-jdk-headless`（或 Loongson JDK 25，见 generic/loongson-jdk）；
   gradle 用官方 9.1.0 zip（系统 apt 只有 4.4.1，远低于 `application.gradle.min=9.1`，不可用）。
2. 第三方依赖：`gradle -I gradle/support/fetchDependencies.gradle init`（~979MB，17 分钟；
   报 `Project with path 'Doclets' could not be found` 只影响该辅助任务，
   实测可跳过直接 buildGhidra，分发不受影响）。
3. LoongArch 移植补丁（**两层四处，一次打全**——细节按 generic/gradle-native-porting
   的 checklist 走，这里只记 Ghidra 的具体落点）：
   - `GPL/nativePlatforms.gradle`：PLATFORMS 加
     `[name:"linux_loongarch_64", os:"linux", arch:"loongarch64"]` +
     switch 补 `case "loongarch64": arch = "loongarch_64"`
   - Java 运行时侧（应用认架构，与构建侧两层皮都要补）：
     `Ghidra/Framework/Generic/src/main/java/ghidra/framework/Architecture.java`
     加 `LOONGARCH_64("loongarch64","la64")`；
     同目录 `Platform.java` 加 `LINUX_LOONGARCH_64(...,"linux_loongarch_64",".so","")`
     并把 `/lib/loongarch64-linux-gnu`、`/usr/lib/loongarch64-linux-gnu` 加进 Linux 库搜索路径
   - `gradle/hasProtobuf.gradle`：loongarch 分支改走本地 protoc
     （`-Dghidra.protoc.path=`，protoc 获取见 generic/protobuf-loongarch）
   - targetPlatform 三模块五处（grep 定位法见 generic/gradle-native-porting）：
     GPL/DemanglerGnu/build.gradle ×2、Ghidra/Features/Decompiler/buildNatives.gradle ×2
     （decompile+sleigh）、Ghidra/Features/FileFormats/buildNatives.gradle ×1（lzfse）
4. 全量：`gradle --no-daemon buildGhidra`（Java 部分 824 任务实测 26 分钟；
   nohup+rc 轮询纪律照系统提示）。**验收看 zip 里 os/linux_loongarch_64/ 有没有真件**
   （假成功陷阱见 generic/gradle-native-porting）。
5. deb：dist zip 解入 /opt/ghidra/12.3，/usr/bin 放 ghidraRun、
   ghidra-analyzeHeadless、ghidra-pyghidraRun 三个 exec 包装；
   Depends: openjdk-25-jdk-headless（组树规范见 generic/debian-packaging）。

## 验证（Ghidra 特有的验收面）
1. headless 全链自证（导入→分析→反编译一个 loongarch ELF）：
   ```bash
   gcc -O0 -static -o hello_loongarch hello.c
   support/analyzeHeadless ws proj -import hello_loongarch -postScript Verify.java
   ```
2. **GhidraScript 的 API 陷阱（实测 6 轮试错换来）**：无 `getFunction(name)`、
   无 `getFunctions(String, TaskMonitor)`——找 main 的正确写法是
   `getFunctions(true)` 遍历 + `f.getName().equals("main")` 匹配；
   脚本文件里 public 类名必须与文件名一致，否则 ClassNotFoundException。
3. 断言输出：`LANG: Loongarch:LE:64:lp64d`、FUNC_COUNT 合理、
   `DECOMPILE_OK: true` + 正确 C 伪代码。

## 已知限制（Ghidra 特有）
- GnuDisassembler(gdis)：需 binutils-2.41 源码自编译，上游平台表无 loongarch，
  本次未做（扩展 zip 内只有源码无二进制）。
- PDB：上游仅 Windows 构建。
- BSim 的 PostgreSQL 服务器端未构建（postgres 源码 tar 已在 dependencies）。
- GUI（Swing）无显示环境未实测；dbgeng/x64dbg 调试器前端 Windows 专属。
- Eclipse 插件为预构建 zip 随包，未在 loongarch64 Eclipse 实测。
