---
name: pytorch-2160a0-loongnix-server-232-loongarch64-build
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  PyTorch 2.16.0a0（任务名 pytorch2.12）CPU-only wheel 与 libtorch 构建实录：Loongnix Server 23.2 loongarch64（参考 openEuler-24.03 LTS 攻略）。编译 PyTorch、装构建依赖、处理 pypi 502/403、submodule/asmjit/license-files、PEP 639 校验时读这条。
---

## 简要场景

- 软件：PyTorch；任务名 `pytorch2.12`，仓库 `version.txt` 为 `2.16.0a0`，源码 HEAD 为 `f54c229`，shallow clone。
- 实测系统：Loongnix Server 23.2（`ID=loongnix-server`，`PLATFORM_ID=platform:lns23`），架构 `loongarch64`，内核 `6.6.0-117.0.0.122.loongarch64`。
- 工具链：gcc/g++ 12.3.0，Python 3.11.6，pip 23.3.1；系统 cmake 3.30.5，构建实际使用 pip 安装的 cmake 4.4.4；ninja 1.13.2；git 2.47.3。
- 资源：`nproc` 128，内存 509 GiB，`/work` 可用 84G，overlayfs。
- 最终结果：success。
- 产物：`torch-2.16.0a0+gitf54c229-cp311-cp311-linux_loongarch64.whl`（81 MB）；libtorch 安装目录 319 MB / 9476 文件；补丁 `loongarch64.patch`（2 文件，+10/−11）。

## 构建步骤

1. 环境探测：确认 `version.txt`、git HEAD、系统 ID、`uname -m`、gcc、Python、submodule 状态，以及 `CMakeLists.txt` / `cmake/Dependencies.cmake` 中 loongarch 相关分支。

2. 安装 dnf 依赖：

   ```bash
   dnf install -y git patch wget curl make ninja-build gcc gcc-c++ python3-devel python3-pip \
       zlib-devel openssl-devel libffi-devel bzip2-devel xz-devel
   ```

   首次报缓存 rpm 缺失时，先执行：

   ```bash
   dnf clean packages
   ```

   再重跑上述安装。

3. 安装 loongarch64 可用系统依赖：

   ```bash
   dnf install -y sleef sleef-devel cpuinfo cpuinfo-devel
   dnf install -y python3-numpy python3-pyyaml python3-sympy python3-networkx
   dnf install -y openblas-devel
   dnf install -y numactl-devel libunwind-devel glog-devel gflags-devel libuv-devel
   dnf install -y openssl
   ```

   实测预装版本：sleef 3.8.0-2、cpuinfo 24.09.26、numpy 1.26.4、PyYAML 6.0.2、sympy 1.11.1、networkx 2.8.8、OpenBLAS 0.3.28。

4. 安装 Python 构建依赖。默认 pip 源不可用时，显式使用 `https://pypi.org/simple`：

   ```bash
   python3 -m pip install --index-url https://pypi.org/simple \
       'setuptools>=77,<82' 'packaging>=24.2' 'sympy>=1.13.3' \
       pyyaml cmake ninja pybind11 typing-extensions filelock jinja2 \
       networkx fsspec optree scikit-build-core spin requests six \
       expecttest hypothesis psutil build
   ```

   实测结果：cmake 4.4.4、ninja 1.13.2、optree 0.20.0、setuptools 81.0.0、sympy 1.14.0、packaging 26.3、scikit-build-core 1.1.1、build 1.6.1。cmake、ninja、optree、MarkupSafe、hypothesis、psutil 从 sdist 现场编译出 loongarch64 wheel。

5. 初始化顶层 submodule：

   ```bash
   git submodule update --init --jobs 8
   ```

   实测顶层 36 个 submodule 一次成功，rc=0，未出现 `.git/config.lock` 竞争。

6. 按路径递归初始化关键 submodule：

   ```bash
   git submodule update --init --recursive --jobs 4 <path>
   ```

   路径包括：`onnx`、`protobuf`、`tensorpipe`、`gloo`、`cpuinfo`、`pybind11`、`googletest`、`benchmark`、`ideep`。

   实测其中 2 个报 `Unable to find current revision`，但内容已就位。

7. 初始化 Eigen：

   ```bash
   python3 tools/optional_submodules.py checkout_eigen
   git checkout -f .
   ```

   实测得到 Eigen 5.0.1。

8. 处理 `asmjit`。`git clone` 反复失败，改用 codeload tarball：

   ```bash
   https://codeload.github.com/asmjit/asmjit/tar.gz/<pinned-sha>
   ```

   下载后用：

   ```bash
   tar -x --strip-components=1
   ```

   解到 `third_party/fbgemm/external/asmjit`。

9. 打 loongarch 架构补丁：在 `CMakeLists.txt` 中增加 `CPU_LOONGARCH` 分支。

   实测不要把 `CPU_INTEL` 置 ON。`cmake_dependent_option(USE_MKLDNN ... "${CPU_INTEL}" ...)` 与 `USE_ITT` 都依赖 `CPU_INTEL`，置 ON 会拉进 oneDNN/ITT 的 x86 代码路径。本机只加 `CPU_LOONGARCH` 标记，让厂商 `CPU_*` 全 OFF，靠 `vec_base.h` 的通用仿真 Vectorized 模板走通用路径。

10. 先做纯 cmake configure 探测（CPU-only 选项集），实测 14 秒 rc=0，零 CMake Error。探测阶段额外带：

   ```bash
   USE_NUMA=1 USE_LAPACK=0
   ```

   最终构建未显式传这两个。

11. 构建 wheel：

   ```bash
   python3 -m build --wheel --no-isolation --outdir /work/output
   ```

   使用：

   ```bash
   MAX_JOBS=96
   USE_CUDA=0 USE_ROCM=0 USE_XPU=0 USE_MPI=0 USE_NCCL=0 USE_XNNPACK=0 USE_NNPACK=0
   USE_PYTORCH_QNNPACK=0 USE_FBGEMM=0 USE_KLEIDIAI=0 USE_VULKAN=0 USE_MPS=0 USE_MKLDNN=0
   USE_DISTRIBUTED=0 USE_SYSTEM_SLEEF=1 USE_SYSTEM_CPUINFO=1 USE_OPENMP=1 BLAS=OpenBLAS BUILD_TEST=0
   ```

   实测 ninja 编译 1581 targets + install + 打包约 5 分钟，零 `error:`，产出：

   ```text
   torch-2.16.0a0+gitf54c229-cp311-cp311-linux_loongarch64.whl
   ```

12. 安装 libtorch：

   ```bash
   cmake --install build --prefix /work/output/libtorch
   ```

   实测 overlayfs 上失败，改为先安装到 `/tmp`，再复制到目标目录。

13. 导出补丁：

   ```bash
   git diff > /work/output/loongarch64.patch
   ```

## 踩坑点

1. pip 默认源不可用，表现为“找不到版本”。

   原日志：

   ```text
   ERROR: Could not find a version that satisfies the requirement numpy<2 (from versions: none)
   ```

   实测原因：`lpypi.loongnix.cn` 返回 `502 Bad Gateway`，`mirrors.aliyun.com` 返回 `403`，而 `pypi.org` 返回 `200`。

   规避：显式使用：

   ```bash
   --index-url https://pypi.org/simple
   ```

   numpy 走 dnf 的 `python3-numpy`，版本 1.26.4 满足 `numpy<2`。

2. dnf/pip 混装导致 `importlib.metadata` 版本误判。

   原日志：

   ```text
   Unmet dependencies: packaging>=24.2, found: 23.0
   ```

   但：

   ```bash
   python3 -c "import packaging;print(packaging.__version__)"
   ```

   显示 26.3。

   实测原因：dnf 的 `python3-packaging-23.0` 与 pip 安装的 26.3 共用 site-packages，`packaging-23.0.dist-info` 残留导致 `importlib.metadata` 报旧版本。setuptools、sympy 也存在类似混装。

   规避：删除旧 `dist-info` / `egg-info` 目录，例如：

   ```bash
   rm -rf packaging-23.0.dist-info sympy-1.11.1-py3.11.egg-info
   ```

   setuptools 使用：

   ```bash
   pip install --ignore-installed
   ```

   并手工清理 `_distutils_hack` / `pkg_resources` 后重装；用 `importlib.metadata.version()` 复核。

3. `asmjit` 的 `git clone` 在 overlayfs 上反复失败。

   原日志：

   ```text
   could not open '.../.git/objects/pack/tmp_pack_XXXX' for reading: No such file or directory
   invalid index-pack output
   ```

   实测原因：该容器 overlayfs 上 git 的 tmp_pack 文件写入异常；同法克隆其他仓库正常，仅此路径反复触发。

   规避：绕开 git，用 codeload tarball：

   ```text
   https://codeload.github.com/asmjit/asmjit/tar.gz/<pinned-sha>
   ```

   注意 `github.com/.../archive/<sha>.tar.gz` 对裸 SHA 返回 404，必须使用 codeload 形式。

4. 即使 `USE_FBGEMM=OFF`，configure 仍因 asmjit 缺失失败。

   原日志：

   ```text
   third_party/fbgemm/external/asmjit appears incomplete.
   ```

   实测原因：`cmake/PreBuildSteps.cmake` 的存在性检查不受 `USE_FBGEMM` 开关控制；且 asmjit 在 `.gitmodules` 里未注册，`git submodule update --init <path>` 会报：

   ```text
   pathspec ... did not match any file(s) known to git
   ```

   规避：用 tarball 填充 `third_party/fbgemm/external/asmjit`，仅满足检查；fbgemm 实际不参与本次 CPU-only 编译。

5. PEP 639 license-files 强校验失败。

   原日志：

   ```text
   ConfigurationError: Every pattern in "project.license-files" must match at least one file:
   'third_party/aiter/3rdparty/composable_kernel/LICENSE' did not match any
   ```

   共 11 条。

   实测原因：scikit-build-core 按 PEP 639 校验 `pyproject.toml` 的 61 条 license glob，每条必须命中至少 1 个文件；本机为 CPU-only 构建，未初始化 `aiter` / `fbgemm-nested` / `flash-attention` / `mslk` 等 GPU 子模块。

   规避：用 `tomllib` 比对出全部缺失项，并从 `pyproject.toml` 删除这 11 条 license glob，不初始化无关 GPU 树；复核 missing=0。

6. 源码构建 SLEEF 在 loongarch64 上失败。

   实测规避：使用系统包：

   ```bash
   USE_SYSTEM_SLEEF=1 USE_SYSTEM_CPUINFO=1
   ```

   wheel 不再捆绑 `libsleef.so` 和 `libcpuinfo.so`，运行期依赖系统：

   ```text
   /lib64/libsleef.so.3
   /lib64/libcpuinfo.so.24.09.26
   ```

7. 递归 submodule 时报 `Unable to find current revision`。

   实测：按路径递归初始化时，2 个路径报该信息，但实际内容已就位。

8. 攻略中的 `.git/config.lock` 竞争未复现。

   实测：先非递归 `--jobs 8` 初始化顶层 submodule，再按路径逐个递归，全程未出现 config.lock 错误。

9. overlayfs 上 `cmake --install` 失败。

   实测规避：先安装到 `/tmp`，再复制到 `/work/output/libtorch`。

10. 攻略中的 `collect_env.get_env_summary` 不存在。

    实测改用：

    ```bash
    python -c "import torch; ..."
    ```

    并结合冒烟脚本与 C++ 程序验证。

## 验证方法（轻量冒烟即可）

本平台是编译平台：验收只证明"装得上、import 得动、算子调得通"。**不要**跑 `test_ops`/gradcheck/compile/JIT/pytest 全量——test_ops 有 3 万+ 参数化 item（gradcheck 是数值微分，单条几秒~几十秒），龙芯走标量路径更慢，全套要数小时，那是 PyTorch CI 的开发动作，不是编译验收。
- 检查 wheel 包文件内容
- 在干净 venv 环境安装 wheel
- 基础验证：可正常 import、基础算子可调用（不跑完整 test_ops /compile/ JIT 全套用例）
- 基础验证 libtorch C++：可链接、能正常启动运行（简单调用即可，不做复杂算子核验）
- 核对产物版本字符串可正常读取展示

  ```bash
  /tmp/vcheck/bin/python - <<'EOF'
  import torch
  a = torch.randn(64, 64, dtype=torch.float64)
  print("version:", torch.__version__)
  print("matmul:", (a @ a).sum().item() is not None)
  x = torch.ones(3, requires_grad=True); (x * 2).sum().backward()
  print("grad:", x.grad.tolist())
  print("sleef exp:", torch.exp(torch.tensor(2.0, dtype=torch.float64)).item())
  print("SMOKE_OK")
  EOF
  ```

  五项全出数（版本串/matmul/backward 梯度/exp/SMOKE_OK）即判成功。
- 确认产物版本串：

  ```text
  torch-2.16.0a0+gitf54c229
  ```

- 确认构建 Summary 实测：

  ```text
  USE_BLAS=1
  USE_LAPACK=1
  USE_GLOG=OFF
  USE_ITT=OFF
  USE_MIMALLOC=OFF
  USE_MKLDNN=OFF
  USE_NNPACK=OFF
  USE_XNNPACK=OFF
  USE_QNNPACK=OFF
  USE_FBGEMM=OFF
  USE_DISTRIBUTED=0
  ```

- 确认补丁规模：

  ```text
  2 文件，+10/−11
  ```
