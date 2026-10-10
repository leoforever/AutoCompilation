"""
Sample SKILL.md files for testing
"""
SKILL_DATA = {
    "gen-pip-loongarch": {
        "name": "gen-pip-loongarch",
        "type": "generic",
        "version": "v2.1",
        "skill_content": """# gen-pip-loongarch

## 适用系统
- openEuler 24.03 LTS (loongarch64)
- 统信 UOS V20 专业版
- 龙蜥 Anolis OS 8

## 问题描述
在 LoongArch 架构上安装 Python 包时，部分包需要从源码编译，依赖特定的交叉编译工具链。

## 依赖列表
| 依赖 | 版本要求 | 安装方式 |
|------|----------|----------|
| python3-devel | >= 3.9 | dnf install python3-devel |
| gcc | >= 12 | dnf install gcc |
| g++ | >= 12 | dnf install gcc-c++ |
| pip | >= 21.0 | dnf install python3-pip |

## 编译步骤
```bash
# 1. 安装基础依赖
dnf install -y python3-devel gcc gcc-c++ pip

# 2. 设置 pip 镜像源（可选）
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 安装 Python 包
pip install <package-name> --no-binary :all:
```

## 注意事项
- LoongArch 架构需要启用 FPU 硬件浮点支持
- 部分包可能不支持 loongarch64，需要查找社区维护的分支
- 建议使用虚拟环境避免系统包污染

## 错误排查
- 问题：pip install 失败，提示找不到编译器
  → 解决：安装 gcc gcc-c++ 开发包
- 问题：编译过程中内存不足
  → 解决：增加 swap 空间或限制并发编译数 (-j1)
""",
        "script_content": "#!/bin/bash\nset -e\n\necho \"[INFO] 安装 LoongArch Python 编译依赖...\"\ndnf install -y python3-devel gcc gcc-c++ pip\n\necho \"[INFO] 依赖安装完成\"\n"
    },
    "ollama-oe2403-build": {
        "name": "ollama-oe2403-build",
        "type": "specific",
        "version": "v0.9.7",
        "skill_content": """# ollama-oe2403-build

## 适用系统
- openEuler 24.03 LTS (loongarch64)

## 问题描述
编译 ollama v0.9.7 在 openEuler 24.03 上的 LoongArch 版本。

## 依赖列表
| 依赖 | 版本要求 | 安装方式 |
|------|----------|----------|
| go | >= 1.22.0 | dnf install golang-go |
| cmake | >= 3.20 | dnf install cmake 或源码编译 |
| pcre2-devel | >= 10.40 | dnf install pcre2-devel |
| libcurl-devel | >= 7.88 | dnf install libcurl-devel |
| openssl-devel | >= 3.0 | dnf install openssl-devel |

## 编译步骤
```bash
# 1. 克隆源码
git clone --depth 1 --branch v0.9.7 https://github.com/ollama/ollama.git
cd ollama

# 2. 配置交叉编译
export CROSS_COMPILE=loongarch64-unknown-linux-gnu-
export CGO_ENABLED=1
export CC=loongarch64-linux-gnu-gcc

# 3. 构建
make -j$(nproc)
make install PREFIX=/usr/local

# 4. 打包
make rpm  # 或 make deb，根据目标系统选择
```

## 注意事项
- LoongArch 架构需要启用 FPU 硬件浮点支持
- pcre2 版本必须 >= 10.40，低版本会导致 configure 失败
- 建议内存 >= 4GB，避免 OOM

## 错误排查
- 问题：configure 报错 "the HTTP rewrite module requires the PCRE library"
  → 解决：安装 pcre2-devel 而非 pcre-devel，命令：dnf install pcre2-devel
- 问题：编译过程中 OOM
  → 解决：增加容器内存限制或使用 -j1 单线程编译
""",
        "script_content": "#!/bin/bash\nset -e\n\necho \"[INFO] 开始编译 ollama v0.9.7...\"\necho \"[INFO] 目标系统：openEuler 24.03 LTS (loongarch64)\"\n\n# 安装依赖\ndnf install -y golang-go cmake pcre2-devel libcurl-devel openssl-devel\n\n# 克隆源码\ngit clone --depth 1 --branch v0.9.7 https://github.com/ollama/ollama.git\ncd ollama\n\n# 配置编译环境\nexport CROSS_COMPILE=loongarch64-unknown-linux-gnu-\nexport CGO_ENABLED=1\nexport CC=loongarch64-linux-gnu-gcc\n\n# 编译\nmake -j$(nproc)\nmake install PREFIX=/usr/local\n\n# 打包\nmake rpm\n\necho \"[SUCCESS] ollama 编译完成\"\n"
    }
}

def get_sample_skills():
    """返回示例 Skill 数据"""
    return list(SKILL_DATA.values())
