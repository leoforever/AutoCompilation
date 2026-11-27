---
name: go-toolchain
type: layer1-infra
description: 龙芯平台 Go 工具链：选版顺序=货架 go1.* 件优先 ∪ go.dev 白名单 4 版（1.24.13/1.25.6/1.26.9/1.27.2），按 go.mod 要求自动选版本下载安装。安装 Go、go.mod 钉 toolchain、编译 Go 项目时读这条。
allowed-tools: [execute, read_file]
---

# Go 工具链（龙芯 LoongArch）

## 选版顺序：货架 ∪ 白名单

1. 先看货架：`ls /work/cache/go*/go1.*.linux-loong64.tar.gz /work/cache/_common/go*/go1.*.linux-loong64.tar.gz 2>/dev/null | sort -V`
   有 ≥ go.mod 要求的件 → 直接 `tar -C /usr/local -xzf <件>`，跳到验证
   （零网络，最快路径）。货架=活白名单：管理员传新版即入库，本篇不用改字。
2. 货架没有 → 下方 go.dev 白名单，规则原样（挑 ≥ 要求的最低版，只下这 4 个）。

## 已知可用版本（白名单，4 个）

| 版本 | URL | 大小 |
|---|---|---|
| 1.24.13 | https://go.dev/dl/go1.24.13.linux-loong64.tar.gz | ~60 MB |
| 1.25.6 | https://go.dev/dl/go1.25.6.linux-loong64.tar.gz | ~62 MB |
| 1.26.9 | https://go.dev/dl/go1.26.9.linux-loong64.tar.gz | ~62 MB |
| 1.27.2（当前最新稳定） | https://go.dev/dl/go1.27.2.linux-loong64.tar.gz | ~65 MB |

**只在这 4 个里选**。其他版本（1.25.x 部分、1.26.0/1.26.1 等）go.dev 没发 loong64 二进制，
不要凭印象拼 URL 或去 HTML 页面挑（避免选到 404）。

## 选版本（一行规则）

**在白名单里挑 ≥ go.mod 要求 的最低版本**。

- 例：go.mod 要 `go 1.26.0` → 白名单里 ≥ 1.26.0 的最低是 **1.26.9**（不是 1.27.2）
- 例：go.mod 要 `go 1.21.0` → 白名单里最低是 **1.24.13**
- 例：go.mod 要 `go 1.27.0` → 白名单里最低是 **1.27.2**
- 例：go.mod 要 `go 1.28.0`（超过白名单）→ 选白名单里最高 **1.27.2**（+告知用户 go.mod 要求超出已知版本，可能需要等新版）

判断步骤：
1. `head -5 /work/src/go.mod` 拿到 `go X.Y.Z`
2. 在白名单里找 ≥ X.Y.Z 的最低版本（major.minor 段比较）

## 下载安装（一段命令搞定）

把下面 `<VER>` 替换成选出来的版本号（例如 1.26.9）：

```bash
VER=1.26.9
URL="https://go.dev/dl/go${VER}.linux-loong64.tar.gz"
curl -fLO "$URL" && \
  tar -C /usr/local -xzf "go${VER}.linux-loong64.tar.gz" && \
  rm "go${VER}.linux-loong64.tar.gz" && \
  /usr/local/go/bin/go version
```

要点：
- `-f` 让 HTTP 错误返回非零，避免下到 404 HTML
- 解压到 `/usr/local`（标准位置），会自动覆盖旧版本
- 验证最后一行必须输出 `go version go1.26.9 linux/loong64`

安装后写入 PATH（每次新 shell 都需要，build 命令前 inline 一下就行）：
```bash
export PATH=/usr/local/go/bin:$PATH
export GOPROXY=https://goproxy.cn,direct
```

## 版本号比较（major.minor 比，patch 不参与）

按 major → minor → patch 逐段比：
- `1.24.13` < `1.26.0`（24 < 26）
- `1.26.0` < `1.26.9`（同 major.minor，比 patch）
- `1.26.9` < `1.27.2`（minor 26 < 27）

**不要**用字符串比较（`"1.24.13" > "1.26.0"` 是错的，会以为 1.24.13 更新）。

## 不要做

- 不要下白名单外的版本（很可能 404）
- 不要下 32 位（loongarch 而不是 loong64）
- 不要从 `proxy.golang.org` 下 Go 工具链（不可用）
- 不要用 `go.dev/dl/?mode=json`（这台机器上经常返空响应）
- **不要**降级到 < go.mod 要求版本（编译必失败）
- 不要跳过 `go version` 验证（解压失败 / 404 解压会得到奇怪目录）
- go.mod 有 `toolchain goX.Y.Z` 行且已装版本 < 钉版时，go 会自己去
  proxy.golang.org 拉（本环境不通，必挂）——解法：`export GOTOOLCHAIN=local`
  强制用已装版本，或按「选版顺序」从货架取 ≥ 钉版的件
- 货架件解压后照旧必须 `go version` 验证（同 404 HTML 防呆，一视同仁）

## 编译策略（先 go build，必要时再 cmake）

**Go 项目优先用 `go build`**，不要无脑走 README 推荐的 cmake 全量构建路径。
教训（2026-10-12 ollama 任务）：ollama 有 cmake 包装（可拉 llama.cpp 加速后端），
但 cmake --build 全量编译要 15+ 分钟，会被 stage5 30 分钟上限卡死。
**绝大多数 Go 项目用 `go build` 即可拿到可用二进制**，缺加速后端只是没 GPU 加速。

判断步骤：
1. **检查项目根有没有 `CMakeLists.txt`**：`ls /work/src/CMakeLists.txt 2>/dev/null`
2. **没有 CMakeLists.txt** → 直接 `go build -p {_jobs_per_worker} -o /work/output/<name> .`（最快路径）
3. **有 CMakeLists.txt**：
   - 先试 `go build -p N -o /work/output/<name> .` 拿到纯 Go 二进制（几分钟完成）
   - 仅当用户明确要求加速后端 / CUDA / ROCm / 完整功能时才走 `cmake -B build && cmake --build build`
   - 全量 cmake 编译要 15+ 分钟，stage5 整体才 30 分钟，**不要默认走这条**

构建命令模板：
- 纯 Go（推荐）：`cd /work/src && go build -p {_jobs_per_worker} -o /work/output/<name> .`
- 全量 cmake（谨慎）：`cd /work/src && cmake -B build . -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel {_jobs_per_worker}`
