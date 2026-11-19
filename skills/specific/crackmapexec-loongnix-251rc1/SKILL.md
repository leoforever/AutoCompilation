---
name: crackmapexec-loongnix-251rc1
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  CrackMapExec 6.1.0 在龙芯 Loongnix 25.1-rc1（loongnix-stable）loongarch64 上的 Python wheel、依赖安装与 shiv zipapp 构建记录。编译 CrackMapExec、安装 cme/cmedb、处理 Python C/Rust 依赖时读这条。
---

## 简要场景

- 软件：CrackMapExec 6.1.0
- 系统：Loongnix GNU/Linux 25（loongnix-stable），任务目标系统为 loongnix-25.1-rc1
- 架构：loongarch64
- 包格式：deb，使用 apt/dpkg
- 容器初始状态：`python3` / `gcc` / `git` / `pip` 全部不存在，需要从零安装
- 实测 Python：3.13.5，pip 25.1.1
- 实测 gcc：14.2.0
- 实测 glibc：2.41
- pip 已预置龙芯专源：`index-url = https://lpypi.loongnix.cn/loongson/pypi/+simple`
- 最终状态：success
- 主要产物：
  - `crackmapexec-6.1.0-py3-none-any.whl`
  - `aardwolf-0.2.17-cp313-cp313-linux_loongarch64.whl`
  - `cme`
  - `cmedb`
  - `requirements-linux_loongarch64.txt`

## 构建步骤

1. 先安装基础系统包。容器初始缺少 Python、GCC、Git 等构建工具。

   ```bash
   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     python3 python3-pip python3-dev python3-venv python3-setuptools build-essential \
     git ca-certificates pkg-config libffi-dev libssl-dev zlib1g-dev

   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     libxml2-dev libxslt1-dev libxml2-utils

   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     libsodium-dev libgmp-dev libsqlite3-dev

   DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
     libjpeg-dev libtiff-dev libfreetype-dev liblcms2-dev libwebp-dev libbz2-dev liblzma-dev
   ```

   首批 apt 安装可能因镜像索引过期失败，失败后先执行：

   ```bash
   apt-get update
   ```

   再重跑同一条 install 命令。

2. 创建构建后端虚拟环境，安装 `poetry-core` 和 `build`。

   ```bash
   python3 -m venv /work/build/venv-build
   /work/build/venv-build/bin/pip install -q --upgrade pip setuptools wheel "poetry-core>=1.2.0" build
   ```

   实测 `poetry-core` 为 2.5.0。

3. 构建 CrackMapExec 主 wheel。主包本身是纯 Python 包，产物为 `py3-none-any` wheel。

   ```bash
   cd /work/src
   /work/build/venv-build/bin/python -m build --wheel --no-isolation --outdir /work/build/dist
   ```

   必须使用 `--no-isolation`，因为隔离构建环境会联网拉取 `poetry-core`。

4. 优先从龙芯专源安装已有 loongarch64 二进制轮，绕开部分源码构建问题。

   ```bash
   pip install --only-binary :all: cryptography==50.0.1 cffi lxml==6.0.3 pillow==12.3.0 \
               bcrypt==4.2.1 greenlet msgpack pydantic-core rpds-py psutil pyyaml
   ```

5. 安装纯 Python 依赖和 git 依赖。

   ```bash
   pip install requests beautifulsoup4 lsassy termcolor neo4j pylnk3 pypsrp paramiko \
     dsinternals xmltodict terminaltables aioconsole pywerview minikerberos pypykatz \
     bloodhound asyauth masky sqlalchemy aiosqlite pyasn1-modules rich python-libnmap \
     resource "git+https://github.com/mpgn/impacket.git@gkdi" "git+https://github.com/NeffIsBack/oscrypto"
   ```

6. 安装需要 Rust 的原生扩展包。`dploot` 需要跳过其死 pin 的 `lxml==4.9.3`。

   ```bash
   pip install "aardwolf>=0.2.7,<0.3"
   pip install --no-deps "dploot>=2.2.1,<3"
   ```

7. 安装 Rust 工具链。本次使用货架：

   ```text
   /work/cache/rust/rust-1.99.0-loongarch64-unknown-linux-gnu.tar.xz
   ```

   将 Rust 发行包解压到 `/work` 下，再执行其 `install.sh`：

   ```bash
   ./install.sh --prefix=/work/build/rust-toolchain
   ```

   安装后可验证 `rustc --version` 和 `rustc --print sysroot`。实测 Rust 1.99.0 满足 `aardwolf` 的 `requires Rust >=1.64.0`。

8. 安装 CrackMapExec 本体时，不要直接让 pip 全量解析 wheel 依赖。干净复验中应使用 `--no-deps` 安装主 wheel，依赖按前面步骤单独安装。

   ```bash
   pip install --no-deps /work/build/dist/crackmapexec-6.1.0-py3-none-any.whl
   ```

9. 按上游 shiv 打包路线生成 zipapp 单文件 `cme` / `cmedb`。实测可用形式：

   ```bash
   shiv --site-packages <site-packages> -e cme.crackmapexec:main -E --reproducible -o bin/cme
   ```

   `cmedb` 按上游 `build_collector.py` 等价流程生成。

## 踩坑点

1. 首批 apt 安装失败

   原日志：

   ```text
   E: Failed to fetch .../linux-libc-dev_6.6.52-1.8-lnd.1_all.deb  404  Not Found
   ```

   根因是镜像索引过期，包版本已变更。规避方法是先 `apt-get update`，再重跑同一条 `apt-get install`。

2. 直接 `pip install crackmapexec-6.1.0-py3-none-any.whl` 全量解依赖时，多个原生扩展包源码构建失败。

3. `lxml 4.9.3` 源码构建失败

   原日志：

   ```text
   error: too few arguments to function '_PyLong_AsByteArray'
   ```

   根因是 `dploot` 把 `lxml` 死 pin 成 `==4.9.3`，该版本随包的 Cython 生成 C 与 Python 3.13 的 `_PyLong_AsByteArray` 新签名不兼容。仅安装 `libxml2-dev` / `libxslt1-dev` 不能解决，因为不是缺头文件。

   规避方法：

   - 使用龙芯专源上的 loongarch64 二进制轮：`lxml==6.0.3`
   - 安装 `dploot` 时使用 `--no-deps`
   - 安装 CrackMapExec 本体时使用 `--no-deps`

4. `Pillow 13.0.0` 源码构建失败

   原日志：

   ```text
   RequiredDependencyException: The headers or library files could not be found for jpeg
   ```

   规避方法：

   - 补装 `libjpeg-dev libtiff-dev libfreetype-dev liblcms2-dev libwebp-dev libbz2-dev liblzma-dev`
   - 优先使用龙芯专源上的 loongarch64 二进制轮：`pillow==12.3.0`

5. `bcrypt 5.0.0` / `aardwolf 0.2.17` 源码构建失败

   原日志：

   ```text
   error: can't find Rust compiler
   ```

   规避方法是安装 Rust 1.99.0 到 `/work/build/rust-toolchain` 后，再安装 `aardwolf`。

6. Rust 发行包解压到 `/tmp` 后工具链半残

   症状：

   ```text
   error[E0463]: can't find crate for 'core'
   note: the loongarch64-unknown-linux-gnu target may not be installed
   ```

   但 `rustc --version` 正常，且 `rustc/lib/rustlib/loongarch64-unknown-linux-gnu/lib` 不存在。

   根因：容器内核禁止 tar 改目录权限，出现：

   ```text
   Cannot change mode ...: Operation not permitted
   ```

   导致 `install.sh` 没被完整执行，标准库组件没有落到 sysroot。

   规避方法：把 Rust 发行包解压到 `/work` 下，再执行 `./install.sh --prefix=/work/build/rust-toolchain`。

7. `shiv` 打 zipapp 失败

   原日志：

   ```text
   ERROR: You must give at least one requirement to install
   ```

   根因：shiv 1.0.8 的 `--site-packages` 只在同时给了 pip 参数时才复制进暂存区；只给 `--site-packages` 时它会把该目录当 source，而 `-v` 被 `ignore_unknown_options` 吞成 pip 参数，于是走 pip 分支且 requirement 为空。

   规避方法：去掉 `-v`，使用：

   ```bash
   shiv --site-packages <site-packages> -e cme.crackmapexec:main -E --reproducible -o bin/cme
   ```

8. `cme --version` 退出码不是 0

   实测 `cme --version` 会打印：

   ```text
   6.1.0 - John Wick
   ```

   但 rc=1。根因是上游 `cme/cli.py:199-201` 就是 `sys.exit(1)`，venv 安装版与 zipapp 版实测一致，不是本次构建缺陷。

   规避方法：验收脚本按输出文本判定，不按 rc 判定。

9. 验证脚本首轮夹具失败

   原日志：

   ```text
   FileNotFoundError: '/work/verify/run-$$/hv/.cme'
   ```

   根因：`first_run_setup()` 里 `mkdir(CME_PATH)` 不递归创建父目录，而验证时给 HOME 指定了还不存在的目录。

   规避方法：验证前先创建 HOME 与验证目录：

   ```bash
   mkdir -p $RUN/hv $RUN/hh
   ```

10. DB 表名断言失败

    BUILD_NOTES 记录 `smb.db` 实际有 11 张表，但日志尾部截断，未给出完整表名。验收脚本应按实际 DB 表名断言，不要假设固定表名。

11. 检查 wheel 内容时容器内无 `unzip`

    原日志：

    ```text
    bash: line 1: unzip: command not found
    ```

    实测可用 Python `zipfile` 模块检查 wheel 条目。

## 验证方法

1. 干净环境安装产物

   新建 venv，先安装运行依赖，再用 `--no-deps` 安装 CrackMapExec 主 wheel。

   ```bash
   python3 -m venv /work/verify/venv-clean
   /work/verify/venv-clean/bin/pip install --only-binary :all: cryptography==50.0.1 cffi lxml==6.0.3 pillow==12.3.0 \
     bcrypt==4.2.1 greenlet msgpack pydantic-core rpds-py psutil pyyaml
   /work/verify/venv-clean/bin/pip install requests beautifulsoup4 lsassy termcolor neo4j pylnk3 pypsrp paramiko \
     dsinternals xmltodict terminaltables aioconsole pywerview minikerberos pypykatz \
     bloodhound asyauth masky sqlalchemy aiosqlite pyasn1-modules rich python-libnmap resource
   /work/verify/venv-clean/bin/pip install "aardwolf>=0.2.7,<0.3"
   /work/verify/venv-clean/bin/pip install --no-deps "dploot>=2.2.1,<3"
   /work/verify/venv-clean/bin/pip install --no-deps /work/build/dist/crackmapexec-6.1.0-py3-none-any.whl
   ```

   实测干净环境复验中 `CME_NODEPS_RC=0`。

2. 导入 / 加载

   对 cme 全部协议模块和第三方原生扩展做 import 冒烟。实测 35 个模块 import 冒烟全 OK，包括 `aardwolf`、`lxml`、`Pillow`、`bcrypt`、`pynacl` 等。

   已知 `import PIL.ImageFont` 仍 rc=0，只影响字体实际绘制。

3. 一次最小调用

   验证 zipapp 版 `cme`：

   ```bash
   mkdir -p /work/verify/zlast
   HOME=/work/verify/zlast /work/output/cme --version
   ```

   预期输出包含：

   ```text
   6.1.0 - John Wick
   ```

   注意 `cme --version` 实测 rc=1，上游行为如此，验收按输出文本判定。

   最小 smb 调用：

   ```bash
   mkdir -p /work/verify/zlast
   HOME=/work/verify/zlast timeout 180 /work/output/cme smb 127.0.0.1 -u test -p test >/dev/null 2>&1
   ```

   实测 rc=0，并生成：

   ```text
   .cme/workspaces/default/smb.db
   ```