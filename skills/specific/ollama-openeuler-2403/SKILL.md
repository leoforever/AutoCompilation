<!-- synced version: v20261014 -->
---
name: ollama-openeuler-2403
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  ollama 在龙芯 openEuler-24.03 上的完整编译步骤（Go 1.26 / libstdc++ 依赖）。编译 ollama、撞到 collect2 链接报错时读这条。
---

## 简要场景

编译 GitHub ollama 源码包，目标是生成 `/work/output/ollama` 可执行文件。

## 构建步骤

1. **安装 Go 工具链**（go.mod 要求 `go 1.26.0`，白名单中满足 ≥ 1.26.0 的最低版本是 1.26.9）：

```bash
VER=1.26.9
URL="https://go.dev/dl/go${VER}.linux-loong64.tar.gz"
curl -fLO "$URL"
tar -C /usr/local -xzf "go${VER}.linux-loong64.tar.gz"
rm "go${VER}.linux-loong64.tar.gz"
```

2. **安装 C++ 依赖**（openEuler 用 yum）：

```bash
yum clean all
yum install -y --nogpgcheck gcc-c++ libstdc++-devel
```

3. **编译**（显式指定 libstdc++ 搜索路径，因为库位于 `/usr/lib64`，gcc 默认搜索路径未覆盖）：

```bash
cd /work/src
PATH=/usr/local/go/bin:$PATH GOPROXY=https://goproxy.cn,direct \
    CGO_LDFLAGS="-L/usr/lib64" \
    go build -p 4 -o /work/output/ollama .
```

## 踩坑点

### 链接失败：collect2: error: ld returned 1 exit status

- **症状**：go build 在链接阶段报错，错误信息含 `ld returned 1 exit status`。
- **根因**：ollama 的 CGO 部分（如 go-tagger / go-patch / CGO 依赖）需要链接 `libstdc++`，但 `/usr/lib64` 不在 gcc 默认的库搜索路径中，导致链接器找不到 `-lstdc++`。
- **规避**：添加 `CGO_LDFLAGS="-L/usr/lib64"` 显式告知链接器去 `/usr/lib64` 目录搜索。

## 验证方法

```bash
ls -lh /work/output/ollama
/work/output/ollama --version
```

输出如 `Warning: could not connect to a running Ollama instance` 和 `Warning: client version is 0.0.0` 为正常行为（无后台服务时 `--version` 返回 0.0.0），二进制已正确构建。