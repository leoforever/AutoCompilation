---
name: nodejs-toolchain
type: layer1-infra
description: 龙芯平台 Node.js 工具链下载地址、版本选择、镜像源。
   安装 Node.js、npm/yarn 配置、Node 编译型项目（Electron、Next.js 等）时读这条。
allowed-tools: [execute, read_file]
---

# Node.js 工具链（龙芯 LoongArch）

## 下载地址
- 官方: https://unofficial-builds.nodejs.org/download/release/
- 这是 Node.js 官方维护的非官方构建，专门覆盖小众架构（含 loong64）
- 选择版本：node-vX.Y.Z-linux-loong64.tar.xz 或 .tar.gz

## 版本选择
- 推荐 LTS 系列（v20.x / v22.x）
- 不要用 EOL 版本（v18 以下多数已不再维护）

## 国内镜像
- npmmirror: https://registry.npmmirror.com
- 配置: npm config set registry https://registry.npmmirror.com

## 安装方式
```
curl -L -o /tmp/node.tar.xz https://unofficial-builds.nodejs.org/download/release/v22.0.0/node-v22.0.0-linux-loong64.tar.xz
tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
ln -sf /usr/local/bin/node /usr/bin/node
ln -sf /usr/local/bin/npm /usr/bin/npm
ln -sf /usr/local/bin/npx /usr/bin/npx
```

## 已编译原生模块
- 很多 npm 包含 native binding（如 sqlite3、bcrypt）
- 用 prebuilt 时认准 loong64 标签；没 prebuilt 需本地 node-gyp 重编

## 注意事项
- Electron / node-gyp 项目需要 C++ 工具链（见 toolchain-paths）
- yarn / pnpm 同样走 npmmirror

## 不要做
- 不要直接 apt/dnf install nodejs（版本通常严重过时）
- 不要混用 nvm 和系统 node（容易污染 PATH）
