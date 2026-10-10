---
name: go-toolchain
type: layer1-infra
description: 龙芯平台 Go 工具链。安装 Go、go install toolchain 时读这条。
allowed-tools: [execute, read_file]
---

# Go 工具链（龙芯 LoongArch）

## 下载地址
https://go.dev/dl/

这是一个**版本列表页面**，从里面挑需要的版本，下载对应平台的 tar.gz。

## 已知可用版本（2025-10 实测）
go.dev 稳定版 linux-loong64 当前只有：
- go1.27.2 (65 MB)
- go1.26.9 (62 MB)

**很多版本号没有 loong64 二进制**（1.25.x、1.24.x 部分、1.23.x 全没有）。不要凭印象拼 URL。

## 选版本
- 龙芯 64 位平台文件名后缀必须是 `linux-loong64.tar.gz`（**不是** `loongarch`，**不是** 32 位）
- 优先选项目 `go.mod` 里 `go` 或 `toolchain` 字段要求的版本（**精确匹配**）
- 如果该版本还没发 loong64 二进制（go.dev 上查不到），从已知可用版本里挑**最低的 ≥ 要求版本**：
  - 满足 go.mod 最低要求即可（Go 1.21+ 用 module graph，新版兼容旧 module 声明）
  - 选最低的而不是最新，避免跳大版本带来不必要的行为变化
  - 例：go.mod 要 1.26.0，已知 1.26.9 / 1.27.2 → 选 **1.26.9**（最低 ≥ 1.26.0）
- **不要**降级到 < 要求版本（go.mod `go 1.26.0` 用 1.23 编译会 fail）
- 选好后用 `curl -fLO https://go.dev/dl/<ver>.linux-loong64.tar.gz` 下载，**-f 让 HTTP 错误返回非零**避免下到 404 HTML

## 版本号比较（重要，避免误判）
**Go 版本号是数字分段比较，不是字符串比较**：
- `1.24.13` < `1.26.0`（24 < 26，patch 号不参与 major/minor 决策）
- `1.26.0` < `1.26.9`（major.minor 同，比 patch）
- `1.26.9` < `1.27.2`（minor 26 < 27）

**比较时按 major → minor → patch 逐段比**，不要看 `.13` 比 `.0` 大就以为整体大。

**判断当前 Go 是否满足 go.mod 要求**：
1. `go version` 拿到当前版本字符串（如 `go1.24.13 linux/loong64`）
2. 提取数字部分（`1.24.13`），按 `.` 拆成元组 `(1, 24, 13)`
3. go.mod 要求的也拆成元组（如 `(1, 26, 0)`）
4. 逐段比：当前 ≥ 要求？不行就下新版本

**安全做法**：如果当前版本 < 要求版本，**无脑下 go.mod 要求版本对应的 loong64 二进制**（精确匹配或 ≥ 它），不要试图用旧版编译。

## 怎么找可用版本（稳定路径）
- **首选**：HTML 页面 `curl -s https://go.dev/dl/ | grep -oE 'go[0-9.]+\.linux-loong64\.tar\.gz' | sort -u`
- 备选：JSON API `curl -s https://go.dev/dl/?mode=json`（有时返空，**注意校验**：`curl -s ... | python3 -c "import json,sys; json.load(sys.stdin)"`，失败就别用）
- **不要**用 `?mode=json` 当唯一来源——这台机器上发现它经常返空响应

## 模块代理（GOPROXY）
- `GOPROXY=https://goproxy.cn,direct`
- 默认 `proxy.golang.org` **不可用**（loong64 二进制会回 HTML/404，**不要**用它下工具链）

## 不要做
- 不要下 32 位（loongarch 而不是 loong64）
- 不要从 `proxy.golang.org` 下 Go 工具链
- 不要凭印象拼 `go1.X.Y.linux-loong64.tar.gz` URL（很可能 404）
- 不要相信 `go.dev/dl/?mode=json` 没校验过的输出
- **不要**降级到 < go.mod 要求版本（编译必失败）
