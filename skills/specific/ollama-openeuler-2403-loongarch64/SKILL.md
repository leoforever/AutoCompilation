---
name: ollama-openeuler-2403-loongarch64
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  ollama 在龙芯 openEuler-24.03 LTS 上的 loongarch64 构建步骤与 llama.cpp CPU 后端产物布局。编译 ollama、装 Go 工具链、撞到 -lstdc++、GGML_CPU_ALL_VARIANTS、llama-server binary not found 时读这条。
---

## 简要场景（软件 / 系统 / 结果）

- 软件：ollama，源码来自 `https://github.com/ollama/ollama.git`，日志未记录 branch。
- 系统：openEuler 24.03 LTS (LTS-SP4)，`uname -m` = `loongarch64`。
- 结果：编译成功。
- 产物：`/work/output/ollama` 与 `/work/output/lib/ollama/`，其中 `lib/ollama/` 包含 llama.cpp CPU 推理后端文件。
- 关键环境事实：
  - 初始无 Go、无 `g++`、无 `cmake`、无 `ninja`。
  - `go.mod` 要求 `go 1.26.10`，本次安装 `go1.27.2 linux/loong64`。
  - 预装 `gcc (GCC) 12.3.1`、`git 2.54.0`、`curl 8.4.0`、`libcurl-devel 8.4.0`、`python3`。
  - 本次新装 `gcc-c++`、`cmake 3.31.12`、`ninja 1.13.2`。
  - CPU 为 Loongson-3C5000，日志记录 Features 包含 `lsx lasx crc32 lvz lbt_*`。

## 构建步骤

以下命令按本次日志中的实际路径整理。

```bash
mkdir -p /work/build /work/output
cd /work/src
```

安装 Go 工具链。日志记录 `go.dev` 不可达，可用 USTC 镜像：

```bash
curl -fL -o /work/build/go1.27.2.linux-loong64.tar.gz \
  https://mirrors.ustc.edu.cn/golang/go1.27.2.linux-loong64.tar.gz

tar -C /usr/local -xzf /work/build/go1.27.2.linux-loong64.tar.gz

export PATH=/usr/local/go/bin:$PATH
export GOPROXY=https://goproxy.cn,direct
export GOTOOLCHAIN=local
```

安装本次构建缺失的 C++ 运行时与构建系统：

```bash
dnf install -y gcc-c++ cmake ninja-build
```

下载 Go module 依赖：

```bash
cd /work/src
go mod download all
```

先构建 ollama 主二进制：

```bash
go build -p 32 -o /work/output/ollama .
```

构建 llama.cpp CPU 后端。日志中 GitHub FetchContent 浅克隆极慢，本次改用 `codeload` tarball，tag 为 `b11845`：

```bash
curl -fL -o /work/build/llama_cpp-b11845.tar.gz \
  https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/tags/b11845

tar xzf /work/build/llama_cpp-b11845.tar.gz -C /work/build
```

本次成功产物路径使用了 `OLLAMA_LLAMA_CPP_SOURCE` 指向本地源码，并关闭 loongarch64 不支持的多变体：

```bash
OLLAMA_LLAMA_CPP_SOURCE=/work/build/llama.cpp-b11845 \
cmake -S llama/server --preset cpu -DGGML_CPU_ALL_VARIANTS=OFF

cmake --build build/llama-server-cpu --target llama-server --parallel 32

cmake --install build/llama-server-cpu --prefix /work/output
```

ollama 运行时只会在 `lib/ollama/` 查找 `llama-server`，但本次 `cmake --install` 将可执行文件放到 `bin/`，需要复制：

```bash
cp build/llama-server-cpu/bin/llama-server /work/output/lib/ollama/
```

整理为运行时目录：

```bash
rm -rf /work/output/include /work/output/lib64 /work/output/bin /work/output/share
```

按上游 `scripts/env.sh` 约定重新链接注入版本：

```bash
go build -p 32 \
  -ldflags="-s -w -X=github.com/ollama/ollama/version.Version=0.0.0-dev+d44c2581 -X=github.com/ollama/ollama/server.mode=release" \
  -o /work/output/ollama .
```

若需要自动应用 ollama 的 `llama/compat` 补丁，日志记录 `OLLAMA_LLAMA_CPP_SOURCE` 会跳过补丁，应改用：

```bash
cmake -S llama/server --preset cpu \
  -DGGML_CPU_ALL_VARIANTS=OFF \
  -DFETCHCONTENT_SOURCE_DIR_LLAMA_CPP=/work/build/llama.cpp-b11845
```

## 踩坑点

1. `go.dev` 下载 Go 不可达  
   日志症状：`curl -fLO https://go.dev/dl/go1.27.2.linux-loong64.tar.gz` 得到 0 字节，`rc=35/56`。  
   本次可用替代：`https://mirrors.ustc.edu.cn/golang/go1.27.2.linux-loong64.tar.gz`。

2. `go build` 链接阶段缺少 C++ 标准库  
   日志原文：

   ```text
   /usr/bin/ld: cannot find -lstdc++
   ```

   以及：

   ```text
   running gcc failed: exit status 1
   ```

   本次处置：

   ```bash
   dnf install -y gcc-c++
   ```

3. CMake FetchContent 从 GitHub 浅克隆 llama.cpp 极慢  
   日志症状：`cmake -S llama/server --preset cpu` 卡在：

   ```text
   git clone --depth 1 https://github.com/ggml-org/llama.cpp.git
   ```

   15 分钟后 `_deps/.git` 涨到 513MB 仍未完成。  
   本次处置：改下 `codeload` tarball：

   ```bash
   https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/tags/b11845
   ```

4. loongarch64 不支持 `GGML_CPU_ALL_VARIANTS`  
   日志原文：

   ```text
   GGML_CPU_ALL_VARIANTS not yet supported with loongarch64 on Linux
   ```

   本次处置：在保留 `--preset cpu` 其余变量的同时关闭多变体：

   ```bash
   -DGGML_CPU_ALL_VARIANTS=OFF
   ```

   configure 成功输出包含：

   ```text
   -- Adding CPU backend variant ggml-cpu: -march=loongarch64;-mlasx;-mlsx
   ```

5. `llama-server` 安装位置与 ollama 查找路径不一致  
   日志症状：

   ```text
   llama-server binary not found (checked: .../lib/ollama/llama-server, ...)
   ```

   并且：

   ```text
   ldd bin/llama-server
   ```

   显示：

   ```text
   libllama-server-impl.so => not found
   ```

   本次处置：把 `bin/llama-server` 复制到 ollama 实际查找目录：

   ```bash
   cp build/llama-server-cpu/bin/llama-server /work/output/lib/ollama/
   ```

6. `OLLAMA_LLAMA_CPP_SOURCE` 会跳过 compat 补丁  
   日志记录 `llama/server/CMakeLists.txt:141` 中有：

   ```cmake
   elseif(NOT DEFINED ENV{OLLAMA_LLAMA_CPP_SOURCE})
   ```

   本次结论：使用 `OLLAMA_LLAMA_CPP_SOURCE` 可指向本地源码，但会跳过补丁；需要补丁时使用：

   ```bash
   -DFETCHCONTENT_SOURCE_DIR_LLAMA_CPP=<dir>
   ```

7. 后台构建残留导致并发双跑  
   日志症状包括：

   ```text
   /usr/bin/ranlib: 'libllama-ui.a': No such file
   ```

   以及：

   ```text
   failed to create symbolic link 'bin/libggml-base.so.0': File exists
   ```

   根因：旧 configure 未真正终止，与新构建共享同一源码目录并发写。  
   本次处置：按 PID 精确 `kill -9`，改用独立构建目录与独立源码副本。

8. `pkill -f` 误杀自身 shell  
   日志症状：`pkill -f "ollama serve"` / `pkill -f "finaltest"` 把承载命令的 shell 自身杀掉，导致执行超时。  
   本次处置：改用精确进程名取 PID：

   ```bash
   pgrep -x ollama
   pgrep -x llama-server
   ```

9. 服务端口冲突  
   日志出现：

   ```text
   Error: listen tcp 127.0.0.1:11435: bind: address already in use
   ```

   本次验证使用空闲端口，并在服务验证后按 PID kill。

## 验证方法

按平台验收口径做三件套即可，完整 `go test ./...`、integration、bench 非必需。

### ① 干净环境装产物

```bash
mkdir -p /work/build/vfy
cp -a /work/output/* /work/build/vfy/
cd /work/build/vfy

file ollama
file lib/ollama/llama-server

ldd ollama | grep -c "not found"
ldd lib/ollama/llama-server | grep -c "not found"
```

本次日志记录两个 `grep -c "not found"` 均为 `0`。

### ② 导入 / 加载

```bash
cd /work/build/vfy
./ollama --version
```

本次输出包含：

```text
ollama version is 0.0.0
Warning: client version is 0.0.0-dev+d44c2581
```

后台启动服务并检查 API：

```bash
OLLAMA_MODELS=/work/build/mods OLLAMA_HOST=127.0.0.1:11435 ./ollama serve > /work/build/serve.log 2>&1 &
curl -sS http://127.0.0.1:11435/api/version
curl -sS http://127.0.0.1:11435/api/tags
```

若端口已被占用，换空闲端口；验证后按 `pgrep -x ollama` / `pgrep -x llama-server` 取 PID 清理。

### ③ 一次最小调用

本次 BUILD_NOTES 记录最终三件套包含真实模型推理：文本生成 + 向量化，并提到 pull 了 2 个模型，大小分别为 49MB / 486MB。日志尾部未保留具体模型名，因此复现时只需选择本次已验证的小模型完成一次最小文本生成与一次向量化调用即可，不需要跑全量测试套件。