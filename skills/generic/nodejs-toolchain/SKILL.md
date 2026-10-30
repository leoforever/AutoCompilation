---
name: nodejs-toolchain
type: layer1-infra
description: 龙芯平台 Node.js 工具链下载地址、版本选择、npm 源单一权威策略与原生件清点。   安装 Node.js、npm/yarn 配置、Node 编译型项目（Electron、Next.js 等）时读这条。
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

## npm 源（单一权威，强制）
容器默认已配龙芯专源：https://registry.loongnix.cn:5873
- 专源是 npmmirror 全量代理：普通包（纯 JS）同样可达；loong64 原生件（*-linux-loong64-gnu）独此一家
- 仅 HTTPS（5873 端口），HTTP 不通
- **不要用 npm/pnpm config set registry 改全局源**——改到公共源后，loong64 独门件必 404，
  会误诊成"上游没有 loong64 支持"（2026-10 dsh 案实锤路径）
异常处置（仅当专源本身不可达或明确缺该版本）：
- 单点拉取优先：curl -sk 专源 tgz 直链装指定包，不动全局配置
- 确需公共源时：`npm --registry https://registry.npmmirror.com install <包>` 单次传参，禁 config set
- 专源查不到 *-linux-loong64-gnu 件 = 上游真没发布，走源码编译/找社区件分支，不是再换个源试

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

## 安装 Node 项目后必做原生件清点（防延迟爆炸）
npm 按包内 os/cpu 元数据在 loong64 上**静默跳过**平台件（optionalDependencies 只留一行
Unsupported platform 警告），装程"成功"、运行期才炸 Cannot find module ...loong64：
1. grep 安装日志 Unsupported platform / EBADPLATFORM，列出被跳过的平台件
2. 对每个被跳过的件做 require/启动冒烟——别信"装完了"
3. 缺失件按名去专源找 loong64 变体（@rolldown/binding-linux-loong64-gnu 实测专源 200、
   npmmirror 404）：有 → tgz 拉下铺进 node_modules 对应位置；无 → 试源码编译
   （需 g++/make/python3，node-gyp headers 拿不到时回退 unofficial-builds 直连）；
   再无非标"该平台件缺"
4. Node 类产物验收**必须含一次真实启动**（profile boot / 服务起+接口断言）；
   只 dump 版本号或"包装上了"不构成验收（dsh 案：22 项 PASS 未盖 boot，交付即坏）

## 原生模块 / PTY·交互类库的验证（必读）
验证必须**非交互、能自然退出**，否则任务挂死：
- ❌ 反例（实证事故 2026-10-22 T-20261022110307-65072C，node-pty）：
  `node -e "pty.spawn('/bin/bash')"` 裸跑——shell 等输入永不退出，整任务挂 2.5 小时被硬闸强退，产物没归档。
- ✅ 正确姿势（两段式，带 timeout 保险）：
  ```bash
  timeout 30 node -e "
  const pty = require('node-pty');
  const p = pty.spawn('/bin/bash', ['-c', 'echo pty-ok'], {name:'xterm', cols:80, rows:24});
  let out=''; p.onData(d=>{out+=d; if(out.includes('pty-ok')){console.log('PTY_OK'); p.kill(); process.exit(0);}});
  setTimeout(()=>{console.log('PTY_TIMEOUT'); process.exit(1);}, 10000);
  "
  ```
  要点：起进程→onData 断言预期输出→kill；自身带 10s 兜底 + 外层 timeout 30。
- 一般原生模块验收 = 加载断言即可：`timeout 30 node -e "const m=require('模块'); console.log(Object.keys(m))"`。
- 前台服务器类（express listen 等）同理：后台起 → curl 断言 → kill。

## 注意事项
- Electron / node-gyp 项目需要 C++ 工具链（见 toolchain-paths）
- yarn / pnpm 同样以上述专源为准（pnpm 用 `pnpm config set registry` 指向专源，同样禁改公共源为全局）

## 不要做
- 不要直接 apt/dnf install nodejs（版本通常严重过时）
- 不要混用 nvm 和系统 node（容易污染 PATH）
- 不要把 npm/pnpm/yarn 的全局 registry 改成公共源（应急走 --registry 单次传参）
