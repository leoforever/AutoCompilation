---
name: python-package
type: layer2-build
description: Python 项目的完整构建流程。检测构建系统（setup.py/pyproject.toml），生成 wheel 包，处理 C 扩展依赖，确保产物符合 Python 打包标准。
allowed-tools: [execute, read_file]
---

## 简要场景

在龙芯 LoongArch64 系统上编译 Python 项目，生成可安装的 wheel 包（`.whl`）和/或源码包（`.tar.gz`）。适用于：
- 纯 Python 项目（无 C 扩展）
- 带 C/C++ 扩展的项目（如 psycopg2、cryptography、numpy）
- 需要 cross-compile 到 LoongArch 的项目

## 环境检查（必做）

```bash
# 检查 Python 环境
python3 --version
python3 -c "import sys; print(sys.executable, sys.platform)"

# 检查构建工具
python3 -m pip --version
python3 -m setuptools --version 2>/dev/null || echo "setuptools not found"
python3 -m build --version 2>/dev/null || echo "build not found"

# 检查编译器（如果有 C 扩展）
gcc --version 2>/dev/null || echo "gcc not found"
g++ --version 2>/dev/null || echo "g++ not found"

# 检查 pkg-config
pkg-config --version 2>/dev/null || echo "pkg-config not found"
```

## 依赖安装

**纯 Python 项目**：
```bash
pip3 install --upgrade pip setuptools wheel
```

**带 C 扩展的项目**（以 psycopg2 为例）：
```bash
# PostgreSQL 开发库
dnf install -y postgresql-devel python3-devel gcc

# 或 MySQL
dnf install -y mysql-devel python3-devel gcc

# 或通用 C 扩展依赖
dnf install -y openssl-devel zlib-devel libffi-devel
```

## 构建步骤

### 1. 检测项目结构
```bash
cd /work/src
ls -la
# 检查是否存在以下文件：
# - setup.py (经典)
# - pyproject.toml (现代)
# - setup.cfg
```

### 2. 选择构建方式

**优先使用 `pyproject.toml`（现代方式）**：
```bash
# 检查是否有 build 工具
python3 -m pip install build setuptools wheel

# 生成 wheel
python3 -m build --wheel --no-isolation
# 产物: dist/*.whl
```

**回退到 `setup.py`（经典方式）**：
```bash
# 基础构建
python3 setup.py bdist_wheel

# 如果需要 inplace 编译（开发模式）
python3 setup.py build_ext --inplace
```

### 3. C 扩展特殊处理

**psycopg2 类项目**（需要 pg_config）：
```bash
# 设置环境变量
export PATH="/usr/bin:$PATH"  # pg_config 在 /usr/bin

# 构建时指定 pg_config 路径
python3 setup.py bdist_wheel \
    --pg-config=/usr/bin/pg_config

# 或直接设置环境变量
export PG_CONFIG=/usr/bin/pg_config
python3 setup.py bdist_wheel
```

**cryptography 类项目**（需要 OpenSSL）：
```bash
export OPENSSL_DIR=/usr
export OPENSSL_INCLUDE_DIR=/usr/include/openssl
export OPENSSL_LIB_DIR=/usr/lib64

python3 setup.py bdist_wheel
```

### 4. 验证构建

```bash
# 查看生成的产物
ls -lh dist/

# 检查 wheel 内容（可选）
python3 -m zipfile -l dist/*.whl | head -20

# 测试导入（在容器内）
python3 -c "import psycopg2; print(psycopg2.__version__)"
```

## 产物输出（强制）

**所有 Python 项目必须将产物复制到 `/work/output/`**：

```bash
# 创建输出目录
mkdir -p /work/output

# 复制 wheel 包
cp dist/*.whl /work/output/ 2>/dev/null || true

# 如果有 C 扩展，额外复制 .so 文件
find /work/src -name "*.so" -exec cp {} /work/output/ \; 2>/dev/null || true

# 复制源码（可选）
cp -r /work/src/*.py /work/output/ 2>/dev/null || true

# 验证产物
ls -la /work/output/
```

## 产物结构

**理想产物**：
```
/work/output/
├── package_name-1.0.0-py3-none-any.whl      # 纯 Python wheel
├── package_name-1.0.0-cp311-loongarch64.whl # C 扩展 wheel
└── package_name-1.0.0.tar.gz               # 源码包
```

**最少产物**（C 扩展项目）：
```
/work/output/
├── _psycopg.cpython-311-loongarch64-linux-gnu.so  # 编译后的扩展
└── package_name-1.0.0-py3-none-any.whl            # 元数据 wheel
```

## 踩坑点

### 1. pg_config 找不到
**症状**：`Fatal error: PostgreSQL library not configured`
**解决**：
```bash
# 查找 pg_config
find / -name "pg_config" 2>/dev/null

# 安装 PostgreSQL 开发包
dnf install -y postgresql-devel

# 设置环境变量
export PATH="/usr/pgsql-15/bin:$PATH"  # 或 /usr/bin
```

### 2. Python.h 找不到
**症状**：`Python.h: No such file or directory`
**解决**：
```bash
dnf install -y python3-devel
```

### 3. 编译选项缺失
**症状**：链接错误，缺少 `-lssl`、`-lcrypto`
**解决**：
```bash
# 安装 OpenSSL 开发包
dnf install -y openssl-devel

# 设置环境变量
export LDFLAGS="-L/usr/lib64"
export CFLAGS="-I/usr/include/openssl"
```

### 4. 架构不匹配
**症状**：`ImportError: ... wrong ELF class`
**解决**：确保使用正确的 Python 和编译器
```bash
# 检查架构
uname -m  # 应为 loongarch64

# 使用对应的 Python
which python3
python3 -c "import struct; print(struct.calcsize('P') * 8)"  # 应为 64
```

## 验证方法

```bash
# 1. 检查产物文件
ls -lh /work/output/*.whl
ls -lh /work/output/*.so 2>/dev/null

# 2. 测试安装
pip3 install --target=/tmp/test-install /work/output/*.whl
python3 -c "import package_name; print(package_name.__version__)"

# 3. 清理
rm -rf /tmp/test-install
```

## 常见项目速查

| 项目 | 依赖 | 构建命令 |
|---|---|---|
| psycopg2 | postgresql-devel | `PG_CONFIG=/usr/bin/pg_config python3 setup.py bdist_wheel` |
| cryptography | openssl-devel | `OPENSSL_DIR=/usr python3 setup.py bdist_wheel` |
| pymysql | 无 C 扩展 | `python3 -m build --wheel` |
| numpy | openblas-devel | `NPY_BLAS_ORDER=openblas python3 setup.py bdist_wheel` |
| pillow | libjpeg-devel, zlib-devel | `python3 -m build --wheel` |
