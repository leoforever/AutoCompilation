---
name: autotools-cross-loongarch
type: layer1-infra
description: |
  autotools/configure 系项目在龙芯上构建的通用病与通用药。以下场景读这条：
  configure 报 cannot guess build type / unrecognised host，config.guess 太老，
  老项目交叉编译识别不出 loongarch64，autogen.sh/autoreconf 报错。
allowed-tools: [execute, read_file]
---

# autotools 项目的龙芯适配（configure/config.guess 通用药）

## 典型报错
```
configure: error: cannot guess build type; you must specify one
configure: error: <triplet>: unknown host/cpu/vendor
```
根因九成是：**源码里内嵌的 config.guess / config.sub 太老，不认 loongarch64**
（GNU 官方 2021+ 版本才加入支持；2015-2019 的源码树普遍没有）。

## 通用处置（按序试）
1. **显式传 build/host 双保险**（最快，先试这个）：
   ```bash
   ./configure --build=loongarch64-unknown-linux-gnu [其它项目参数]
   ```
   多数 configure 给了 --build 就不再猜，能过。
2. **替换 config 脚本**（第 1 步无效或子目录还报时）：
   ```bash
   cp /usr/share/misc/config.guess /usr/share/misc/config.sub /usr/local/share/ 2>/dev/null
   # 系统里没有就 wget 一份 GNU 2023+ 的；然后铺到所有报错目录：
   find . -name config.guess -exec cp /path/new-config.guess {} \;
   find . -name config.sub   -exec cp /path/new-config.sub   {} \;
   ```
   ⚠ 老项目里嵌套的第三方库（libevent/lua/libpcap 等 bundled 目录）各带一份
   旧脚本，**报错指到哪个目录就替换哪个**；一次全量 find 替换最省事。
3. **autogen/autoreconf 流**：项目用 `./autogen.sh` 的，替换 config 脚本后重跑
   autogen 让它重新注入；报缺 m4 宏（AX_*/AC_* not found）补 `automake libtool`
   或项目自带的 `--copy` 类 flag 多半是子模块没拉（git 源检查 `--recurse-submodules`）。
4. Makefile.in 已生成的项目改 configure 参数即可，不必 autoreconf（少动少错）。

## 别做的事
- 别 patch configure 主脚本硬编码 triplet（升级一次全丢）；
- 别用假参数强行跳过 host 检查——host 认错了编出废件。
