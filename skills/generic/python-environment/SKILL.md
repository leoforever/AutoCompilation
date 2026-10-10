---
name: python-environment
type: layer1-infra
description: 龙芯平台 Python 包源。pip install、装带 C 扩展的 Python 包时读这条。
allowed-tools: [execute, read_file]
---

# Python 环境（龙芯 LoongArch）

## pip 源（按优先级）
- 主: https://lcr.loongnix.cn
- 备: https://mirrors.aliyun.com/pypi/simple
- 备: https://pypi.tuna.tsinghua.edu.cn/simple
