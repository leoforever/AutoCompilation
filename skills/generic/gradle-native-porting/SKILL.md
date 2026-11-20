---
name: gradle-native-porting
type: layer1-infra
allowed-tools: [execute, read_file]
description: |
  Gradle native（c/cpp 插件）项目移植 loongarch64 的通用套路与假成功陷阱。以下场景读这条：
  上游按平台枚举构建原生件、报 Unrecognized platform architecture、BUILD SUCCESSFUL 但
  产物 zip 里没有本架构原生件、targetPlatform / NativeExecutableSpec / nativePlatforms.gradle、
  gradle native toolchain 不认 loongarch64。
---

# Gradle 原生模块移植新架构（loongarch64 实测）

## 简介（是什么）
很多 Gradle 大项目（Ghidra 为典型）的原生 C/C++ 件按**平台枚举表**构建。给这类项目
新增 loongarch64 支持时，"平台注册"有**两层**，只补一层就掉进假成功陷阱。
以下全部来自 Ghidra 任务 T-20261119145756-A0FC37 容器实测。

## 关键事实：两层都要补，缺一不可
```
层一 全局平台表（决定构建系统认识哪些平台 + 分发组装遍历谁）
  典型文件：nativePlatforms.gradle 之类——PLATFORMS 列表加
  [name:"linux_loongarch_64", os:"linux", arch:"loongarch64"]，
  且 getCurrentPlatformName() 的 case 分支补 loongarch64 → 映射
层二 每个原生模块的 targetPlatform 列表（决定该模块真为哪些平台编译）
  在各模块 buildNatives.gradle/build.gradle 的 NativeExecutableSpec/
  NativeLibrarySpec 块里，现有 targetPlatform "linux_arm_64" 行后加
  targetPlatform "linux_loongarch_64"
```
**只补层一的症状（实测烧了一整轮 31 分钟构建）：**
`BUILD SUCCESSFUL`、各模块 buildNatives 显示 `UP-TO-DATE`，但产物 zip 里
`os/linux_loongarch_64/` 一个都没有——枚举没它，分发自然组装不到。

## 使用（移植 checklist，按序）
1. **先找齐两层位置再动手**（一次打全，别增量试）：
   ```bash
   grep -rn "PLATFORMS\s*=\|Unrecognized platform" --include=*.gradle .
   grep -rln 'targetPlatform "linux_arm_64"' --include=*.gradle . | grep -v build/dist
   ```
   后一条列出的**每个文件**都要插 loongarch 行（Ghidra 是 3 个模块 5 处）。
   批量插：`sed -i '/targetPlatform "linux_arm_64"/a\\t\t\ttargetPlatform "linux_loongarch_64"'`
   插完 `grep -c linux_loongarch_64` 各文件计数留档，防漏。
2. **最小真实模块验证 toolchain**（10 分钟级，代替读源码考古）：
   建小工程 `apply plugin:'c'/'cpp'`，model 里定义目标 platform + `gcc(Gcc)`
   工具链，编一个 hello.c——`BUILD SUCCESSFUL` 且 `file` 显示
   `ELF ... LoongArch` 即通。实测 loongarch64 gcc 对未知平台 arch 不加
   -m64 类标志也能直接工作，通常**不需要**给 gradle 打工具链补丁。
3. **全量构建后验收看产物不看 rc**：
   ```bash
   unzip -l <dist.zip> | grep -oE "os/[a-z_0-9]+/" | sort | uniq -c
   # 必须出现 os/linux_loongarch_64/ 且下面有真文件；没有=层二没补全
   ```

## 注意事项
- 平台名要三层一致：全局表 name、模块 targetPlatform、产物目录 os/<name> 同源，
  拼写不一致=静默缺件（构建照样绿）。
- Java 代码侧可能还有架构枚举（如 Ghidra 的 Architecture.java/Platform.java）
  要同步补——那属于"应用运行时认平台"，与本 skill 的"构建期认平台"是两层皮，
  都要查。
- 上游平台表里缺 loongarch 一般不是 bug 是还没移植：改动集值得整理成 patch 留档
  （平台 source-changes.patch 自动随包）。

## 不要做
- **不要 javap 反编译 gradle 的 jar 研究工具链内部**（实测烧 80 分钟，结论
  "gcc 直接可用"用第 2 步十分钟就能得到）——遇疑先跑最小真实模块；
- 不要看到 BUILD SUCCESSFUL 就交付——原生件必须 unzip -l 数目录验收；
- 不要只补一个模块就重跑全量——grep 清单核全部模块，一次打全（增量重跑=双倍构建时间）。
