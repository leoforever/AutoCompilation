---
name: metasploit-framework-loongnix-251rc1
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  Metasploit Framework Ruby 应用 deb 构建与验证流程。在龙芯 Loongnix 25.1-rc1 / loongnix-stable loong64 上编译 metasploit-framework、bundle install、sqlite3/nokogiri native gem、deb 解包验证时读这条。
---

> 通用件指针：deb 组树/Depends 见 `generic/debian-packaging`。本篇只留 ruby native gem 与 bundler 特有坑。
## 简要场景

- 软件：metasploit-framework 6.5.11-dev（git HEAD `4f30a91e`），Ruby 应用框架。
- 系统：Loongnix GNU/Linux 25 (loongnix-stable)，`uname -m` 为 loongarch64，`dpkg --print-architecture` 为 loong64；任务目标系统为 loongnix-25.1-rc1。
- 结果：成功产出 `/work/output/metasploit-framework_6.5.11-1_loong64.deb`。
- 交付格式：deb。
- 验收口径：轻量三件套（装产物 / 加载 / 最小真实调用），未跑上游全量测试。

## 构建步骤

1. 探测环境：确认 `ruby -v`、`gem -v`、`bundle -v`、`gcc --version`、`cmake --version`、`git --version`、`python3 --version`、`nproc`、`df`，并读取 `Gemfile`、`Gemfile.lock`、`metasploit-framework.gemspec`。

2. 安装系统依赖（实际执行两批 apt 安装）：

```bash
apt-get update

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ruby ruby-dev ruby-bundler bundler git build-essential pkg-config \
  zlib1g-dev libssl-dev libsqlite3-dev libpq-dev libpcap-dev libffi-dev \
  libyaml-dev cmake python3 python3-dev make autoconf libtool xz-utils ca-certificates

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  libxml2-dev libxslt1-dev bison libgmp-dev libreadline-dev libncurses-dev \
  liblzma-dev libtool-bin ruby-sdbm libgdbm-dev libkrb5-dev libsasl2-dev
```

第二批是在第一批后 `bundle install` 报缺头文件时补装的。

3. 配置 Bundler 本地项：

```bash
bundle config set --local path vendor/bundle
bundle config set --local without development:test:coverage
bundle config set --local force_ruby_platform true
bundle config set --local mirror.https://rubygems.org https://mirrors.cloud.tencent.com/rubygems
bundle config set --local build.nokogiri --use-system-libraries
bundle config set --local build.sqlite3 --enable-system-libraries
```

说明：
- 不安装 `development`、`test`、`coverage` 三组 gem。
- `nokogiri` 与 `sqlite3` 均改为使用系统库，避免 gem 自带 tarball 在容器内解包 chmod 失败。

4. 安装 Ruby gem 依赖：

```bash
bundle install --jobs 16 --retry 3
```

成功输出包含：

```text
Bundle complete! 20 Gemfile dependencies, 205 gems now installed.
```

随后执行：

```bash
bundle check
```

输出：

```text
The Gemfile's dependencies are satisfied
```

5. 核查 native 扩展：在 `vendor/bundle` 下确认存在 `.so` 文件，并用 `file` 抽查，例如 `ffi_c.so`、`sqlite3_native.so` 应识别为 LoongArch 相关 ELF 对象。

6. 源码树轻量验证：

```bash
./msfconsole --help
./msfvenom --list archs
printf 'version\nexit\n' | ./msfconsole -q -n
```

实测：
- `./msfconsole --help` 返回 rc0。
- `./msfvenom --list archs` 返回 rc0，且包含 `loongarch64`。
- `msfconsole` 输出版本信息，例如：

```text
Framework: 6.5.11-dev-4f30a91e
Console  : 6.5.11-dev-4f30a91e
```

7. 可迁移性验证：使用 tar 管道复制整树到 `/work/build/reloc` 后，再次运行 `msfvenom` 与 `msfconsole`，均返回 rc0。

8. 打包 deb：上游无 `debian/`，本次使用自写脚本 `/work/build/mkpkg.sh` 生成 deb。首版 deb 因 `version.yml` 时间戳未加引号导致解包树 gemspec 加载失败；将 `version.yml` 中所有值加双引号后重建通过。

9. 最终 deb 验证：从 `/work/verify/debroot2/opt/metasploit-framework` 解包树执行轻量三件套。

## 踩坑点

1. `bundle install` 直连 rubygems.org 卡死在 fetch 阶段

症状：
- 直连 rubygems.org 时，大 gem（如 `metasploit-payloads-2.0.257.gem`）下载极慢。
- 日志观察到大量 socket，但 `vendor` 目录不增长。

根因：
- 跨境链路带宽不足，不是编译错误。

规避方法：
- 杀掉卡住的 bundle 进程。
- 使用 Bundler 局部镜像配置，不改全局配置：

```bash
bundle config set --local mirror.https://rubygems.org https://mirrors.cloud.tencent.com/rubygems
```

实测腾讯 rubygems 镜像可用，`bundle install` 完成 205 个 gem。

补充：
- Loongnix 自带 rubygems 镜像的 `specs.4.8.gz` 返回 `504 Gateway Time-out`，本次不可用。

2. `sqlite3` gem 构建失败

关键错误原文：

```text
Extracting sqlite-autoconf-3540100.tar.gz ... ERROR
tar: ... Cannot change mode ...: Operation not permitted
```

根因：
- `sqlite3` gem 默认自带 `sqlite-autoconf` tarball，解包时需要 chmod。
- 容器内核拒收该 `fchmodat` 操作。

规避方法：

```bash
bundle config set --local build.sqlite3 --enable-system-libraries
```

改用系统包 `libsqlite3-dev`。

同类预防：

```bash
bundle config set --local build.nokogiri --use-system-libraries
```

3. deb 解包树中 `msfvenom` 启动失败（首版 deb 的真实缺陷）

关键错误原文：

```text
/usr/lib/ruby/3.3.0/bundler.rb:663:in `rescue in eval_gemspec': (Bundler::GemspecError)
[!] There was an error while loading `metasploit-framework.gemspec`:
    Tried to load unspecified class: Time. Bundler cannot continue.
Psych::DisallowedClass: Tried to load unspecified class: Time
```

根因：
- `lib/metasploit/framework/version.rb` 使用 `YAML.load_file` 读取 `version.yml`。
- Psych 5 下该 API 等价 `safe_load`，白名单不含 `Time`。
- 首版 `version.yml` 中 `built: 2026-11-18T09:19:04Z` 是裸 ISO8601，被 YAML 解析成 `Time`，导致 gemspec 加载失败。
- 源码树不复现，因为源码树没有 `version.yml`；只有打包产物才暴露该问题。

规避方法：
- 在生成 `version.yml` 时，将所有值加双引号，例如：

```yaml
built: "2026-11-18T09:19:04Z"
```

- 重建 deb 后，必须从 deb 解包树复验，不能只验源码树。

4. `tar` 复制 / `dpkg-deb -x` 出现权限告警

关键错误原文：

```text
tar: Cannot change mode to rwxr-xr-x: Operation not permitted
```

根因：
- 容器内核限制，chmod 类操作被拒绝。
- 与 lintian 攻略确认的容器内核 chmod/tar mode EPERM 行为一致。

规避方法：
- staging 使用 `cp -a`，实测能保留可执行位。
- `dpkg-deb -x` 的 rc=2 只来自这些 chmod 告警；解出的树权限正确、内容完整。
- 验证时以内容检查（如 `md5sum -c`）和脚本可执行位为准。

5. 非致命观察

- `./msfvenom -h` 返回 rc=1，但帮助文本正常打印；正式验证改用：

```bash
./msfvenom --list archs
```

- `msfconsole` 中 `load path` 可能报：

```text
Failed to load plugin from /work/src/plugins/path
```

原因是插件名不存在，进程仍返回 rc0。

## 验证方法

验收口径为基本三件套，全部从最终 deb 解包树执行：

```bash
cd /work/verify/debroot2/opt/metasploit-framework
```

1. 包元数据与产物结构

```bash
dpkg-deb -f /work/output/metasploit-framework_6.5.11-1_loong64.deb
dpkg-deb -I /work/output/metasploit-framework_6.5.11-1_loong64.deb
dpkg-deb -c /work/output/metasploit-framework_6.5.11-1_loong64.deb
```

实测：
- `Package` 为 `metasploit-framework`。
- `Version` 为 `6.5.11-1`。
- `Architecture` 为 `loong64`。
- `dpkg-deb -I` 含 `conffiles` 与 `md5sums`。
- `dpkg-deb -c` 条目数约 35670。
- `/usr/bin` 下存在 8 个软链：`msfconsole`、`msfvenom`、`msfdb`、`msfupdate`、`msfd`、`msfrpc`、`msfrpcd`、`msfmcpd`。

2. 干净解包与内容完整性

```bash
dpkg-deb -x /work/output/metasploit-framework_6.5.11-1_loong64.deb /work/verify/debroot2
```

实测：
- 解包树约 570M。
- `msf*` 脚本权限为 `-rwxr-xr-x`。
- 抽查 `md5sums` 前 300 条：

```bash
md5sum -c <(head -300 /work/verify/debroot2/DEBIAN/md5sums)
```

无输出，表示全部匹配。

3. 导入 / 加载

```bash
./msfconsole --help
printf 'version\nexit\n' | ./msfconsole -q -n
```

实测：
- `./msfconsole --help` rc0。
- `msfconsole` 输出版本信息：

```text
Framework: 6.5.11-dev-4f30a91e
Console  : 6.5.11-dev-4f30a91e
```

4. 最小真实调用：架构表

```bash
./msfvenom --list archs
```

实测：
- rc0。
- 输出包含 `loongarch64`。

5. 最小真实调用：通用 payload

```bash
./msfvenom -p linux/x64/meterpreter/reverse_tcp LHOST=10.0.0.1 LPORT=4444 -f elf -o <output.elf>
```

实测：
- rc0。
- 输出类似：

```text
Payload size: 130 bytes
Final size of elf file: 250 bytes
```

6. 最小真实调用：龙芯原生 payload

```bash
./msfvenom -p linux/loongarch64/shell_reverse_tcp LHOST=127.0.0.1 LPORT=4444 -f raw -o <output.raw>
```

实测：
- rc0。
- 输出类似：

```text
No arch selected, selecting arch: loongarch64 from the payload
Payload size: 144 bytes
```

7. loongarch64 payload 数量

```bash
./msfvenom --list payloads | grep -c loongarch64
```

实测输出：

```text
35
```

8. 未执行项

未跑上游全量测试套件；完整套件验证非必需。