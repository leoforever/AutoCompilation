---
name: rpm-packaging
type: layer1-infra
description: |
  rpm 系（openEuler/Loongnix Server）打包与验收通用流程，尤其 loongnix-build 系镜像内。
  以下场景读这条：rpmbuild 报 Macro _install_langs has empty body、debug_package 阶段
  gdb command not found、rpm --root 验证报 libc 依赖缺失、spec 的 %changelog 日期被拒、
  CFLAGS 传不进 rpmbuild、产物 release 尾巴(.lns23)与目标系统名不一致；
  rpmbuild 报 "Name field must be present in package"（spec 含中文注释+POSIX locale）、
  --short-circuit 后 %files 报 Could not open filelist、brp-compress 致 man 页 File not
  found、openEuler 找不到 audit-libs-devel、%prep 解包成功但 %install 报 chmod
  cannot access（buildroot 被自动清空）；
  打包带 systemd 服务的守护进程（Type=notify、ExecStartPre 预初始化）、服务启动报
  "Failed to open the bootstrap file"、MY-010455、datadir 半成品死循环、状态目录
  属主问题——一律先看本条"服务包目录属主"节。
allowed-tools: [execute, read_file]
---

# rpm 打包通用坑与验收（龙芯 loongnix-build / openEuler 镜像实测）

## 起手：rpmbuild 两个镜像级必修（任何项目都要）
1. **`_install_langs` 空宏**：症状
   `error: /etc/rpm/macros.image-language-conf: line 1: Macro %_install_langs has empty body`。
   根因是容器镜像里该文件写成了 `%_install_langs `（空值），影响所有 rpm 命令。
   规避：命令行加 `--define "_install_langs %{nil}"`。
   ⚠ 别试 spec 里 `%undef _install_langs`——rpm 报 `Unknown tag`，实测无效（lsx 案取证）。
2. **debug_package 缺 gdb**：症状 `gdb-add-index: gdb: command not found`。
   Loongnix/openEuler 镜像默认启用 debuginfo 子包并调 gdb 建索引，构建镜像通常无 gdb。
   规避：spec 首行 `%global debug_package %{nil}`（不装 gdb、不动系统包）。

## spec 写法要点
- `%changelog` 日期**先跑 `date` 再写**（容器年份与记忆年份翻过车： bogus date in %changelog）。
- 上游 Makefile 用普通赋值 `CFLAGS = ...` 时，rpmbuild 环境的 CFLAGS 注入**不生效**；
  spec 不要画蛇添足传 `CFLAGS=`，保持上游编译口径（引入 %optflags 反而可能带来
  -flto 与新架构 flag 交互的未知变量），记入已知限制即可。
- 源码 tarball 名对齐 `Name-Version.tar.gz` 惯例，Source0 指它，rpmbuild -bb 走标准流。
- **解包/生成 buildroot 内容只准放 %install，禁放 %prep**（11-24 nacos 案实测）：
  rpm 4.18 在 `Executing(%install)` 脚本开头自动 `rm -rf %{buildroot}` 重建——
  %prep 里辛苦解进 buildroot 的东西会被整个清空，症状是 %install 报
  `chmod: cannot access '.../*.sh'` 而 %prep 日志明明解压成功。正解：%prep 只留
  空 %setup 或干脆不解，`tar xzf %{SOURCE0} -C %{buildroot}%{destdir} --strip-components=1`
  放 %install 开头自己执行。
- **spec 全文 ASCII-only**（11-24 MySQL 案）：容器 locale 常为 `LC_CTYPE="POSIX"`，
  rpm 4.18 解析器遇中文注释会中断 header 解析——报错极具误导性：
  `Name field must be present in package`（像整个文件没被读）。自检法：
  `grep -vP '[^\x00-\x7F]' spec > /tmp/t.spec` 后 `rpmspec -P` 能过即坐实；
  处置=注释改英文并在文件头留 `NOTE: keep this file ASCII-only`。
  同族：%changelog 正文别出现 `%` 开头的词（被当段/宏，报 `second %install`）。
- man/info 页在 `%files` 必须带通配（`mysql.1*`）：brp-compress 在 %install 后
  把它们就地压成 `.gz` 改名，硬写原名必报 `File not found`。

## 服务包目录属主（daemon + systemd 预初始化必读，MySQL 9.7.4 案 2026-11-23 容器实锤）
凡打包对象是**降权运行的守护进程**（User=mysql 这类）且带首次启动自初始化
（ExecStartPre=+pre_script → `mysqld --initialize` 这种），必查两件事：

1. **rpm 不会重置装前已存在目录的属主**——目标机若残留过 mariadb/旧版本包，
   `/var/lib/mysql-files` 之类状态目录就是 root:root 750，包装上去纹丝不动。
2. 预初始化脚本常把临时 init-file 写进这类 root-only 目录，再让**降权后的
   daemon 去读** → 静默失败（如 `[ERROR][MY-010455] Failed to open the
   bootstrap file ...`），初始化留下"目录骨架在、系统表没建成"的半成品；
   而 pre 脚本判断是否初始化过只看数据目录存不存在 → **之后每次启动都跳过
   重来，永远卡死**，restart 无解。

修法（spec %post 里，幂等一行专治残留目录）：
```sh
# 对 pre 脚本会用到的每个状态目录，比照 /var/run/mysqld 的既有先例：
install -d -m 0750 -o mysql -g mysql /var/lib/mysql-files   # 例：MySQL
```
`install -d -o -g` 与 rpm 目录落盘不同：**已存在的目录也会把属主掰正**，
正好补上 rpm 那个"既存的不动"行为缺口。

交付前 30 秒冒烟（干净容器）：`mkdir -m750 <状态目录> && chown root:root`
模拟残留 → `rpm -Uvh --force` 重装 → 手跑 pre 脚本 → 断言初始化产物完整
（如 `<datadir>/mysql/user.ibd` 存在）且日志无 bootstrap 失败行。

已中招机器的救法（存量交付附带）：chown 归位 → **清空半成品数据目录**（必须，
否则 pre 永不重来）→ restart → 从 error log 取初始 root 密码。

## 验收口径（薄，rpm --root 隔离装）
裸 `rpm -ivh --root=暂存根` 会报 `libc.so.6()(64bit) is needed`——暂存根里没有
glibc 文件树，依赖探针扫不到，这是夹具局限不是包错。套路（lsx 案实测）：
```sh
# 借宿主 rpm 库做 dbpath（注意宿主路径是 /usr/lib/sysimage/rpm，不是 /var/lib/rpm）
cp -a /usr/lib/sysimage/rpm/. db-$$/
rpm -ivh --nodeps --root=/work/verify/root-$$ --dbpath=db-$$ <pkg>.rpm
rpm -ql --root=... --dbpath=db-... <pkg>          # 确认落点
# 依赖可满足性对宿主库判（不看暂存根）：
rpm -qp --requires <pkg>.rpm
rpm -q --whatprovides 'libc.so.6(GLIBC_2.36)(64bit)'
```

## release 尾巴与目标系统的关系
产物 rpm 的 release/dist 尾巴（如 `.lns23`）**跟随构建镜像**，不跟随任务标注的目标系统名。
用 loongnix-build:23.2 镜像编 openEuler 目标任务，出包就是 `.lns23`——同 glibc 基线（都 2.38）
时可正常安装使用，但验收/上架记录里要如实写构建镜像，别按目标名猜尾巴。

## 快速迭代打包：别信 --short-circuit（11-24 MySQL 案实测）
`rpmbuild --short-circuit -bb` 意图只跑 %files/打包，实测**连 %install 一起跳过**
（与 --help 里 "skip straight to specified stage (only for c,i)" 的预期不符），
症状=`%files` 报 `Could not open %files file .../filelist-*.txt` 且日志无
`Executing(%install)`。正解=派生 smoke spec：脚本把正式 spec 的 %build 段替换成
`ln -sfn <已建树> release`，正常 -bb——%install/%files/打包全部真实执行，只省编译。
正式交付仍用原 spec 完整跑一遍（smoke 通过≠交付路径验证过）。

## 发行版包名差异速查
- openEuler 24.03 系：`audit-libs-devel` 不存在，包名 **`audit-devel`**
  （Loongnix Server 23.x 用 audit-libs-devel）；dnf 报 No match 先互为镜像找一遍。

## 别做的事
- 不为消 debuginfo 报错去装 gdb（改 spec 一行 vs 动镜像）；
- 不手改 /etc/rpm/macros.* 系统文件（--define 传参即可，改镜像会漂）；
- 不在 rpmbuild 前省 `make clean`（残留 .o 会混进 debug 列表报怪错）。
