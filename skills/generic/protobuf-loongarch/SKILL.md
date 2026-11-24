---
name: protobuf-loongarch
type: layer1-infra
allowed-tools: [execute, read_file]
description: |
  protoc 在龙芯 loongarch64 上的获取方式：无官方预编译件时的源码自建流程。以下场景读这条：
  protobuf 编译报缺 protoc、Maven 坐标 linux-loongarch64 不存在、apt 的 protobuf-compiler
  版本太旧不匹配 protobuf-java、需要特定版本 protoc、grpc/.proto 工程构建、
  cmake -S . 报 does not contain CMakeLists.txt（老版 protobuf 入口在 cmake/ 子目录）、
  protoc-gen-grpc-java 无本架构 exe（抽 java_plugin.cpp 直链自建 .a，无需 bazel）。
---

# loongarch64 上的 protoc 获取（实测可用）

## 简介（是什么）
需要 protoc 生成代码的项目（protobuf/grp c/.proto 工程）在龙芯上的三条获取路，
按"能用现成的就不自建"排序。实测来源：Ghidra 任务 T-20261119145756-A0FC37
（需要 protoc 31.0 配 protobuf-java 4.31.0，前两条都不满足走了自建）。

## 方法（按序试）
1. **apt 现成**（版本够用就别折腾）：
   ```bash
   apt-get install -y protobuf-compiler && protoc --version
   # 实测 loongnix-25.1-rc1 给 libprotoc 3.21.12——注意与 protobuf-java 4.x 有年代差
   ```
   版本匹配判据：protoc 生成的代码要能被项目依赖的 protobuf 运行时编译；
   跨大版本（如 3.21 生成 + 4.31 运行时）roundtrip 实测能过，但版本不一致
   会被构建脚本的版本校验拒（Ghidra hasProtobuf.gradle 即按版本拼坐标），
   此时走下面两条。
2. **货架/专源找预编译件**：先查 /work/cache INDEX 有没有历史自建的 protoc；
   Maven Central / GitHub releases 均**无 loongarch64 预编译** protoc 坐标
   （实测 4.31.0 目录只有 x86/arm/windows）——不要在这类源上再花时间找。
3. **源码自建（最终兜底，实测 ~8 分钟）**：
   ```bash
   wget -O protobuf-31.0.tar.gz https://github.com/protocolbuffers/protobuf/archive/refs/tags/v31.0.tar.gz
   tar xzf protobuf-31.0.tar.gz && cd protobuf-31.0
   cmake -S . -B pbuild -DCMAKE_BUILD_TYPE=Release -Dprotobuf_BUILD_TESTS=OFF \
         -DBUILD_SHARED_LIBS=OFF -DCMAKE_C_FLAGS=-O1 -DCMAKE_CXX_FLAGS=-O1
   cmake --build pbuild --target protoc -j 24
   pbuild/protoc --version   # libprotoc 31.0
   ```
   ⚠ 老版本（≤3.19.x，含 3.16.1）的 CMake 入口在 **`cmake/` 子目录**且是
   out-of-source 老式写法：`cmake -S .` 在根目录报
   `does not appear to contain CMakeLists.txt`——改
   `cmake -S cmake -B build ...`（nacos 案 3.16.1 实测）；新版（≥22.x）才在根目录。
   两个实测细节：`-O1` 是刻意的——loongarch gcc14 对 protobuf 重模板文件 -O2/-O3
   编译极慢且吃内存，-O1 全量约 8 分钟，别按习惯抠编译优化；
   依赖走系统（zlib/abseil 由 apt 满足，见 debian-packaging 的装依赖姿势）。
3b. **protoc-gen-grpc-java 插件同样无 loongarch 件（nacos 案实测，路子极短）**：
   Maven `io.grpc:protoc-gen-grpc-java:<ver>:exe:<classifier>` 目录只有
   x86/aarch64/ppc64le 等；**不需要 bazel**——取 grpc-java 同版本源码 tar 里
   `compiler/src/java_plugin/cpp/{java_plugin,java_generator}.cpp` 三个文件，
   直接链自建的 libprotoc.a + libprotobuf.a：
   ```bash
   g++ -O1 -std=c++11 java_plugin.cpp java_generator.cpp \
       <pb>/pbuild/libprotoc.a <pb>/pbuild/libprotobuf.a -lz -lpthread \
       -o protoc-gen-grpc-java
   ```
   前提：protoc 那步用 `-DBUILD_SHARED_LIBS=OFF` 已产出两个 .a；插件必须与
   项目 pin 的 grpc-java/protobuf 版本同源匹配。摆进本地 Maven 仓的坐标方法
   见 `generic/java-native-jni`。
4. 项目侧接线：把 protoc 路径以项目支持的"本地文件"方式传入（Ghidra 是
   `-Dghidra.protoc.path=…`；通用做法是改依赖声明为 files(...) 本地件，
   具体看各项目构建脚本的参数）。
5. 自建产物值得晋升 cache_out（binary 类，配 meta 记 gcc/glibc）——
   下一台机器/下一个任务直接复用，8 分钟变 8 秒。

## 不要做
- 不要反复刷新 Maven/GitHub 找 loongarch64 protoc 二进制（上游没有，实测过）；
- 不要为 protoc 给 protobuf 源码打功能补丁（-O1 是构建参数不是源码问题）；
- 不要把 3.21 的 protoc 硬塞给需要 4.x 版本校验的项目再改校验代码——
  自建对应版本更干净，改校验是给将来升级埋雷。
