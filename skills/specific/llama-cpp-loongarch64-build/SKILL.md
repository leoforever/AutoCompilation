---
name: llama-cpp-loongarch64-build
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  llama.cpp 在龙芯 LoongArch64 上的 CMake 编译流程。llama.cpp 已从 Makefile 迁移到 CMake，构建时遇到测试程序链接 GLIBC_PRIVATE 符号报错，读这条。
---

## 简要场景

在龙芯 LoongArch64（loongarch64）系统上编译 llama.cpp 0.6.0-dev 源码，预期生成 `llama-cli`、`llama-bench`、`llama-server` 等可执行文件。

## 构建步骤

### 1. 查看构建文档（可选）

```bash
cat /work/src/docs/build.md
```

llama.cpp 已从 Makefile 迁移到 CMake 构建系统，**不要再使用 `make`**。

### 2. CMake 配置

```bash
cd /work/src
cmake -B build -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF
```

**关键选项**：
- `-DCMAKE_BUILD_TYPE=Release`：Release 模式编译
- `-DLLAMA_BUILD_TESTS=OFF`：关闭测试程序编译（见踩坑点）

### 3. 执行编译

```bash
cmake --build build --config Release -j4
```

### 4. 验证产物

```bash
/work/src/build/bin/llama-cli --version
```

预期输出：

```
version: 0.6.0-dev (build 1, commit df18bee)
built with GNU 12.3.0 for Linux loongarch64
```

## 踩坑点

### 测试程序链接 GLIBC_PRIVATE 符号报错

**症状**：

```
/usr/bin/ld: /usr/lib/gcc/loongarch64-linux-gnu/12.3.0/../../../../
libm.so: undefined reference to symbol 'round@@GLIBC_2.27'
/usr/bin/ld: /usr/lib/gcc/loongarch64-linux-gnu/12.3.0/../../../../
libm.so: undefined reference to symbol 'ceilf@@GLIBC_PRIVATE'
```

**触发场景**：默认编译时，`test-unicode` 测试程序链接系统 math 库失败。

**规避方法**：在 CMake 配置阶段关闭测试编译：

```bash
cmake -B build -DLLAMA_BUILD_TESTS=OFF
```

## 验证方法

```bash
ls /work/src/build/bin/llama*
```

应看到 `llama-cli`、`llama-bench`、`llama-server`、`llama-lookup` 等可执行文件。