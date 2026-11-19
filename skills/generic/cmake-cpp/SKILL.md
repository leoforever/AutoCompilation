---
name: cmake-cpp
type: layer1-infra
description: |
  C/C++ cmake 项目龙芯构建通用姿势。以下场景读这条：cmake configure 失败、
  选 generator/并行度/构建类型、系统 cmake 与 pip cmake 之争、
  CMAKE_INSTALL_PREFIX 怎么给、ninja/make 构建慢。
allowed-tools: [execute, read_file]
---

# cmake C/C++ 项目通用构建姿势（龙芯）

## 工具链（先系统包，别走 pip）
```bash
dnf install -y gcc-c++ make cmake ninja-build        # rpm 系
# apt 系对应 build-essential cmake ninja-build（装法见 debian-packaging）
```
- **不要把 cmake/ninja 放进 pip 依赖列表**（实测：lpypi 与 pypi 的 cmake 轮子
  在 loongarch64 上要 sdist 现编，白烧 10-20 分钟；系统 cmake 3.30+/3.31 已够新）。
  例外：项目硬钉 cmake≥4.x 特性才考虑，且优先货架/专源找 loongarch 件。

## 配置与构建（默认三板斧）
```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --parallel $(nproc)        # 或 -j 同义
cmake --install build --prefix /work/output    # 交付根固定 /work/output
```
- generator 优先 Ninja（报错更干净、增量快）；项目自带 Makefile 才用 make。
- BUILD_TYPE 默认 RelWithDebInfo；调试崩栈才 Debug（体积翻倍）。
- 前缀交给 `--install --prefix`，**不要在 configure 时硬编**（很多项目
  DESTDIR 打包与 INSTALL_PREFIX 冲突；mysql 类要 RPM layout 的除外——听项目 README）。

## configure 常见失败定位
| 症状 | 方向 |
|---|---|
| `could not find XXX`（find_package 失败） | 装 -dev/-devel 包；或 `-DXXX_DIR=` 指到实际目录；pkg-config 缺则装 pkgconf |
| OpenSSL/ZLIB 等找不到 | rpm 系 openssl-devel zlib-devel；deb 系 libssl-dev zlib1g-dev |
| 报 unknown processor/CPU | 龙芯识别问题：`-DCMAKE_SYSTEM_PROCESSOR=loongarch64`；autotools 混用见 autotools-cross-loongarch |
| 构建极慢/超时 | 先看是否误开 -O0 全量调试、LTO；并行度按 nproc；长构建 nohup 后台+rc 文件（系统铁律） |
| 内存爆炸（链接期 OOM） | 降并行只链目标 `-j4`；大 C++ 项目（llvm 类）单链文件可能需 8G+ |

## 验证薄口径
产物二进制 `<bin> --version` timeout 包一发；库类 nm/ldd 断言导出与依赖闭包。
功能级验证听各项目攻略，别为 cmake 通用件发明新仪式。
