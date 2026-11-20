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
- 系统：loongnix-25.1-rc1（deb 系）。耗时两轮实测：首编 3.1h → 有攻略复编 2.0h
  （省的全是试错；剩大头=依赖下载 17-32min + buildGhidra 17-27min，计算硬成本）
- 产物：ghidra_12.3-1_loong64.deb + 上游 dist zip（内含 5 个 LoongArch 原生件：
  decompile/sleigh/lzfse/demangler_gnu_v2_24/v2_41）

## 构建步骤（Ghidra 特有参数）
1. 依赖：**开工先一条 apt 装齐**（11-20 实测镜像缺件会分四次撞上，每次烧一轮）：
   `apt-get install -y --no-install-recommends git wget gcc g++ make cmake unzip zip file patch cpio python3 python3-venv python3-pip python3-dev`
   （python3-dev 是 pyghidra 现场编译 jpype1 的硬前置，缺 Python.h 时 pip 报错很难看；
   openjdk-25-jdk-headless 可按 deb Depends 留给用户装，或现在装给验证用）。
   gradle 用官方 9.1.0 zip（系统 apt 只有 4.4.1，远低于 `application.gradle.min=9.1`，不可用）。
2. 第三方依赖：`gradle -I gradle/support/fetchDependencies.gradle init`（~979MB，
   17-32 分钟看 sourceforge 脸色；**跑完 rc=1 报 `Project with path 'Doclets'
   could not be found` 是预期内**——只影响辅助任务，别去修、别考古 settings.gradle，
   直接进 buildGhidra。注意 init 阶段若先报 NPE/no message，多半是缺 python3
   （hasPythonPackage.gradle 的 `Supported Python ... not found` 警告是真信号），
   补装 python3 再跑，顺序=装 python3 → init → buildGhidra）。
   **dependencies/ 整目录（~1.4GB）值得进货架**：有母包就先 `cp -a` 复用，
   省掉的正是 sourceforge 这段最慢的下载（校验靠 gradle sha256 自带，不怕脏件）。
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
   - **Zero VM 陷阱（11-20 实测，apt openjdk-25 是 Zero VM）**：上游
     support/analyzeHeadless 硬编码 `-XX:CICompilerCount=2`，Zero VM 无 JIT
     直接 `CICompilerCount (2) cannot be greater than 0 because there are no
     compilers` 拒起 JVM（连 -help 都跑不了）。ghidra-analyzeHeadless 包装必须
     探测：所选 java `-version` 第二行含 Zero VM → 注入
     `GHIDRA_HEADLESS_JAVA_OPTIONS="-XX:CICompilerCount=0"`（实测能起但自动分析
     极慢：280s 未完 vs Server VM 51s——包装里顺手提示生产建议 JAVA_HOME 指
     Loongson JDK）。Server VM 探针别用 CICompilerCount=1（同样被拒，"must be
     at least 2"），判据只用 `-version` 第二行。
   - 组树落点：`tar` 解 zip 会报 `Cannot change mode ... Operation not permitted`
     （zip 权限位），直接用 `unzip -q` 解进 STAGE 再 dpkg-deb --build
     --root-owner-group，别跟 tar chmod 较劲。

## 验证（Ghidra 特有的验收面）
1. headless 全链自证（导入→分析→反编译一个 loongarch ELF）：
   ```bash
   gcc -O0 -static -o hello_loongarch hello.c
   support/analyzeHeadless ws proj -import hello_loongarch -postScript Verify.java
   ```
2. **-scriptPath 必须是只放脚本的干净目录（11-20 实测坑）**：scriptPath 会被
   递归收进 bundle 当源码编译——把脚本放在含 dist/大树的目录下，GPL/DMG 的坏
   源码和 PyGhidra 测试件 BadPluginClass.java 会一起被编译，报错刷屏而你的脚本
   只显示 `The class could not be found ... not found by <bundle>`（误导性极强，
   跟真因八竿子打不着）。正确姿势：mkdir scripts/ 只放 .java 一个文件；
   排查时先 `javac -cp <dist 全部 jar 拼 CP> 脚本.java` 单编，秒级见真编译错。
3. **GhidraScript 的 API 正确写法（两轮共 10+ 轮试错换来，直接照抄）**：
   ```java
   FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
   // 不是 getFunctionManager()（GhidraScript 无此直通方法）
   Function f; Signature sig = f.getSignature();
   sig.getSourceType() != SourceType.IMPORTED   // getSourceType 在 Signature 上，
                                               // 不在 Function 上
   DecompileResults res = ifc.decompileFunction(main, 60, monitor);
   res.decompileCompleted() / res.getErrorMessage()  // 不是 errorMessage
   res.getDecompiledFunction().getC()
   ```
   脚本文件里 public 类名必须与文件名一致，否则 ClassNotFoundException。
4. 断言输出：`LANG: Loongarch:LE:64:lp64d`、FUNC_COUNT 合理（hello 静态约 900+）、
   `DECOMPILE_OK: true` + 正确 C 伪代码。
5. `-help` 返回 rc=1 是**上游设计行为**（AnalyzeHeadless 走 printHelpAndExit→
   System.exit(EXIT_CODE_ERROR)）——验证脚本断言"能起且输出含 usage"，别断言 rc=0。

## 已知限制（Ghidra 特有）
- GnuDisassembler(gdis)：需 binutils-2.41 源码自编译，上游平台表无 loongarch，
  本次未做（扩展 zip 内只有源码无二进制）。
- PDB：上游仅 Windows 构建。
- BSim 的 PostgreSQL 服务器端未构建（postgres 源码 tar 已在 dependencies）。
- GUI（Swing）无显示环境未实测；dbgeng/x64dbg 调试器前端 Windows 专属。
- Eclipse 插件为预构建 zip 随包，未在 loongarch64 Eclipse 实测。
