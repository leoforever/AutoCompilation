---
name: ilspy-loongnix-25
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  ILSpy 与 ilspycmd 在龙芯 loongnix-25.1-rc1 上的 .NET 构建、self-contained 发布与 deb 打包流程。编译 ILSpy、ilspycmd、dotnet publish、生成 deb 时读这条。
---

> 通用件指针：deb 构造见 `generic/debian-packaging`。本篇只留 dotnet publish/self-contained 特有流程。
## 简要场景

- 软件：ILSpy 11.3.0，源码 commit `5b269993`
- 系统：Loongnix GNU/Linux 25 / loongnix-25.1-rc1，loongarch64
- 架构相关：`dpkg --print-architecture` 为 `loong64`，.NET RID 为 `linux-loongarch64`
- 实测环境：
  - 内核：`6.6.52-1.7.lns23.loongarch64`
  - glibc：`2.41-13.lnd.1`
  - apt 源：`https://pkg.loongnix.cn/loongnix/25 loongnix-stable main/contrib/non-free`
- 结果：
  - Release 构建成功
  - 产出 GUI deb：`ilspy_11.3.0.0-1_loong64.deb`
  - 产出 CLI deb：`ilspycmd_11.3.0.0-1_loong64.deb`
  - 产出 NuGet 工具包：`ilspycmd.11.3.0.nupkg`
  - 验证结果：12 PASS / 0 FAIL
- 关键前提：
  - 微软官方 `.NET` 发行中没有 `linux-loongarch64` 产物
  - 实际使用社区 CI 发行包 `chenguohui/dotnet-loongarch` 的 `abi2.0` 变体

## 构建步骤

1. 安装基础依赖

```bash
apt-get update
apt-get install -y curl ca-certificates tar xz-utils
apt-get install -y binutils file
apt-get install -y libicu76
apt-get install -y git
apt-get install -y xvfb procps
```

说明：
- `libicu76` 是 `dotnet` CLI 启动必需
- `git` 用于读取 commit，生成版本信息
- `xvfb`、`procps` 用于 GUI 无头冒烟和进程检查

2. 下载 `.NET` 社区 CI SDK 与目标包

```bash
mkdir -p /work/build /work/dotnet /work/dotnet10

curl -sSL -o /work/build/sdk-abi2.tar.gz \
  "https://github.com/chenguohui/dotnet-loongarch/releases/download/v11.0.100-abi2.0/dotnet-sdk-11.0.100-linux-loongarch64.tar.gz"

curl -sSL -o /work/build/sdk10-abi2.tar.gz \
  "https://github.com/chenguohui/dotnet-loongarch/releases/download/v10.0.115-abi2.0/dotnet-sdk-10.0.115-linux-loongarch64.tar.gz"
```

3. 解包 SDK

```bash
tar xzf /work/build/sdk-abi2.tar.gz -C /work/dotnet
tar xzf /work/build/sdk10-abi2.tar.gz -C /work/dotnet10
```

注意：
- 在 `/work` 解包时可能出现 `Cannot change mode to rwxr-xr-x: Operation not permitted`
- 该错误来自挂载权限限制，文件内容本身正常，可用 `ls`、`dotnet --info` 复核后忽略

4. 合并 `net10.0` 的 `packs` 到 SDK 11

```bash
for p in Microsoft.NETCore.App.Runtime.linux-loongarch64 Microsoft.NETCore.App.Host.linux-loongarch64 \
         Microsoft.AspNetCore.App.Runtime.linux-loongarch64 Microsoft.NETCore.App.Ref Microsoft.AspNetCore.App.Ref; do
  for d in /work/dotnet10/packs/$p/*; do
    mkdir -p /work/dotnet/packs/$p
    [ -e /work/dotnet/packs/$p/$(basename $d) ] || cp -rL "$d" /work/dotnet/packs/$p/$(basename $d)
  done
done
```

说明：
- `cp -rL` 用于展平 SDK10 包内的相对软链
- 这一步解决 `dotnet publish -r linux-loongarch64 --self-contained` 时的 packs 软链问题

5. 构建 CLI 项目

```bash
dotnet build ICSharpCode.ILSpyCmd -c Release -p:UseAppHost=false
```

说明：
- 若直接 `dotnet restore ICSharpCode.ILSpyCmd`，可能报 `NU1101`，缺少 `Microsoft.NETCore.App.Host.linux-loongarch64`
- 可先用 `-p:UseAppHost=false` 验证包图，再补齐 SDK10 packs

6. 处理版本信息脚本依赖 `pwsh` 的问题

`ICSharpCode.Decompiler.csproj` 的 `ILSpyUpdateAssemblyInfo` target 会调用 `update-assemblyinfo.ps1`，本机没有 `pwsh` 时会失败。

日志中的错误：

```text
error : pwsh: not found
MSB3073: The command "pwsh … update-assemblyinfo.ps1 Release" exited with code 127
```

规避方式：
- 写入 `VERSION=11.3.0.0`
- 写入 `obj/update-assemblyinfo-last-commit-hash.txt`
- 用 shell 复刻模板替换：
  - `$INSERTREVISION$` → `0`
  - `$INSERTSHORTCOMMITHASH$` / `$INSERTCOMMITHASH$` 取自 git
- 上游脚本在“无 git / 找不到基线 commit”时 revision 本来就是 `0`，语义一致

7. 构建 GUI 项目

```bash
GITHUB_ACTIONS=true dotnet build ILSpy/ILSpy.csproj -c Release -p:UseAppHost=false
```

说明：
- `GITHUB_ACTIONS=true` 用于跳过 `SortResX` 的 `pwsh` 调用
- 上游 target 自带条件：`Condition="'$(GITHUB_ACTIONS)' != 'true'"`
- `ICSharpCode.Decompiler.PowerShell` 不在 `ILSpy.XPlat.slnf` 必建集内，本轮不构建

8. self-contained 发布

```bash
dotnet publish -r linux-loongarch64 --self-contained
```

实测产物目录：
- CLI：`/work/build/pub-sc`
- GUI：`/work/build/pub-gui`

9. 生成 deb

本机无 `pwsh`，不能直接运行上游 `BuildTools/package-linux.ps1`。实际做法是复刻其 deb 分支为 shell：

- 使用上游模板：
  - `BuildTools/packaging/linux/deb/control.template`
  - `ilspy.desktop`
  - `ilspy.png`
- 布局：
  - 文件安装到 `/opt/<pkg>`
  - `/usr/bin` 下创建软链
- 产出：
  - `ilspy_11.3.0.0-1_loong64.deb`
  - `ilspycmd_11.3.0.0-1_loong64.deb`

10. 校验 deb 元数据与依赖

```bash
dpkg-deb -f <deb-file>
apt-get install -s <deb-file>
```

## 踩坑点

1. 官方 `.NET` 安装脚本拿不到 loongarch64 SDK

错误原文：

```text
dotnet-install: Attempting to download using primary link https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.404/dotnet-sdk-10.0.404-linux-loongarch64.tar...
dotnet_install: Error: Could not find `.NET Core SDK` with version = 10.0.404
```

同时：
- `aka.ms/dotnet/<channel>/...loongarch64` 会 302 到 bing
- 官方 `release-metadata/*/releases.json` 全文 grep 无 `loongarch`

处置：
- 使用 `chenguohui/dotnet-loongarch` 社区 CI 发行包

2. `abi1.0` SDK 在 loongnix 25 上无法启动

错误原文：

```text
/lib/loongarch64-linux-gnu/libc.so.6: version 'GLIBC_2.27' not found
libpthread.so.0: version 'GLIBC_2.0'/'GLIBC_2.2' not found
```

根因：
- loongarch64 的 glibc 符号版本从 2.36 起
- 不存在 x86 基线中的 `GLIBC_2.27` / `GLIBC_2.2` / `GLIBC_2.0`
- `abi1.0` 变体按旧基线构建

处置：
- 改用同仓库 `abi2.0` 变体
- `abi2.0` 实测最高只要求 `GLIBC_2.38`，在 glibc 2.41 环境可用

3. `tar` 解包时出现权限错误

错误原文：

```text
Cannot change mode to rwxr-xr-x: Operation not permitted
```

根因：
- `/work` 挂载带 no-setuid / 权限限制
- `tar` 恢复目录 mode 时被拒

处置：
- 文件内容正常
- 用 `ls`、`dotnet --info` 复核后可忽略
- 不需要因此重下

4. `NU1101` 缺少 loongarch64 host pack

错误：
- `dotnet restore ICSharpCode.ILSpyCmd` 报 `NU1101`
- 缺少 `Microsoft.NETCore.App.Host.linux-loongarch64`

处置：
- 先使用 `-p:UseAppHost=false` 验证包图
- 再合并 SDK10 的 packs 到 SDK11

5. 构建期脚本依赖 `pwsh`

错误原文：

```text
error : pwsh: not found
MSB3073: The command "pwsh … update-assemblyinfo.ps1 Release" exited with code 127
```

涉及：
- `ICSharpCode.Decompiler.csproj` 的 `ILSpyUpdateAssemblyInfo`
- `ILSpy.csproj` 的 `SortResX`
- `ICSharpCode.Decompiler.PowerShell.csproj`

根因：
- loongarch64 上 PowerShell 官方无 `linux-loongarch64` 发行包
- v7.4 / v7.5 的 tar.gz 实测 404
- apt 源也没有 powershell

处置：
- 版本信息：shell 复刻 `update-assemblyinfo.ps1` 产物
- `SortResX`：设置 `GITHUB_ACTIONS=true`
- `ICSharpCode.Decompiler.PowerShell`：本轮不构建，记入已知限制

6. self-contained publish 报 packs 软链问题

错误片段：

```text
error MSB4018: System.
```

日志此处被截断，但 BUILD_NOTES 明确说明：
- 在修复 packs 软链问题后 publish 通过
- 修复方式：合并 SDK10 packs 时使用 `cp -rL` 展平相对软链

## 验证方法

完整套件验证非必需。验收口径使用基本三件套：

1. 干净环境安装产物

```bash
dpkg -i /work/output/ilspycmd_11.3.0.0-1_loong64.deb
dpkg -i /work/output/ilspy_11.3.0.0-1_loong64.deb
```

确认：
- `/usr/bin/ilspycmd` 软链存在
- `/usr/bin/ilspy` 软链存在
- 桌面文件安装到 `/usr/share/applications/ilspy.desktop`

2. 加载 / 依赖检查

对产物中的关键动态库执行 `ldd`，确认无 `not found`。

实测通过项：

```text
PASS: libSkiaSharp.so ldd 无 not found
```

3. 一次最小调用

CLI 版本检查：

```bash
ilspycmd --version
```

实测输出：

```text
ilspycmd: 11.3.0.0
ICSharpCode.Decompiler: 11.3.0.0
```

CLI 反编译冒烟：
- 日志显示安装后执行反编译成功
- 输出包含 C# 源码片段，例如：

```text
using System;
using System.Collections.Immutable;
using System.Linq;
using System.Reflection;
using System.Reflection.Metadata;

namespace ICSharpCode.
```

复原：

```bash
dpkg --purge ilspy ilspycmd
```

实测结果：
- `dpkg --purge` 后 `/opt` 中相关目录移除
- `which ilspy ilspycmd` 不再存在