---
name: deepseek-harness-loongarch-openeuler-2403
type: layer2-build
allowed-tools: [execute, read_file]
description: |
  deepseek-harness（dsh）0.2.2-alpha.2 在龙芯 openEuler-24.03 LTS 的完整构建、产物部署与 dsh web 启动流程。编译 deepseek-harness / dsh / pnpm install --frozen-lockfile / build:artifacts / release:pack / koffi CMake / xlsx CDN 慢 / 缺 loong64 binding（rolldown、lightningcss、oxc-resolver、require-builtin）/ 拿到 dsh 产物 tar.gz 怎么装怎么跑 / dsh web 启动报 host preparation failed 时读这条。
---

# deepseek-harness (dsh) — loongarch64 构建与部署一体攻略

- 软件：deepseek-harness（`dsh`），pnpm monorepo + TypeScript + Node ESM
- 版本：`@deepseek-ai/dsh-root` 0.2.2-alpha.2，commit `ce7073d5`
- 系统：openEuler 24.03 (LTS-SP4)，内核 6.6.52-1.7.lns23.loongarch64，loongarch64
- 本文两条主线：**§A 拿到产物包怎么部署运行**（日常路径，先读）；**§B 从源码怎么编**（重编/改版时才用）

---

## §A 部署与运行（产物 tar.gz → dsh web）

### 前置

- Node ≥ 24.9（`engines.node` 要求；实测 v24.22.2 loong64 与 v26.7.0 均可）。
  无现成则：`https://registry.npmmirror.com/-/binary/node-unofficial-builds/v24.22.2/node-v24.22.2-linux-loong64.tar.xz` 解到 /usr/local
- npm 可用；产物包（如 `T-20261030101819-0EBFBA-artifacts.tar.gz`，解出 `artifacts/output/{npm,npm-vendor,npm-landlock,native,...}`）

### 一键脚本（推荐）

`dsh-web.sh`（全文见文末附录）：默认直接启动、`--reinstall` 从原始 tar.gz 全重装、`--status` 看状态、`PORT=3082` 换端口。等价手工五步：

```sh
# 1) 解包
tar -xzf <产物>.tar.gz -C <ART_DIR>            # A=<ART_DIR>/artifacts/output
# 2) 建工程（package.json 最小即可）
mkdir -p <WORKDIR> && cd <WORKDIR> && echo '{"name":"dsh-test","version":"0.0.0","private":true}' > package.json
# 3) 全量装本地 tarball（约 656 包，5 分钟，1.2GB；os 含 darwin 的包 npm 会自动跳过，
#    脚本用 python 预生成清单剔除非 linux 包更稳）
npm install --no-audit --no-fund --registry=https://registry.npmmirror.com $(ls <A>/npm*/*.tgz)
#    ⚠ 只 dsh 家族本体必须本地 tarball；registry 上有同名同版本的上游 x86 build，别按包名联网装
# 4) 补 node-pty loong64 prebuild（产物已知限制 4，必做，否则运行期炸）
mkdir -p node_modules/node-pty/prebuilds/linux-loong64
cp <A>/native/node-pty-prebuilds/linux-loong64/pty.node node_modules/node-pty/prebuilds/linux-loong64/
# 5) 补 require-builtin loong64 binding（web 启动硬依赖，npmmirror 没有，专源有）
curl -fLsk -o rb.tgz https://registry.loongnix.cn:5873/node-addon-require-builtin-linux-loong64-gnu/-/node-addon-require-builtin-linux-loong64-gnu-0.1.9.tgz
npm install --no-audit --no-fund ./rb.tgz
```

### 启动与验证

```sh
cd <WORKDIR>
node node_modules/@deepseek-ai/dsh/lib/bin.js --version     # → 0.2.2-alpha.2
setsid nohup node node_modules/@deepseek-ai/dsh/lib/bin.js web --port 3081 --no-open >> dsh-web.log 2>&1 &
# 就绪判据：端口在听 + 日志出 token URL（浏览器开该 URL 会自动 303 发 cookie 进界面；
# curl 直接带 token 会被 401 拦——认证走浏览器 cookie 流，属正常）
ss -tln | grep 3081; grep -o 'http://127.0.0.1:[0-9]*/?token=\S*' dsh-web.log | tail -1
```

- 上游文档的 `npx @deepseek-ai/dsh web` / `pnpm dsh web` 是 x86/ARM 与源码仓开发者姿势，**loong64 均不可用**（npx 现装的公共 registry 没有 loong64 原生件）；本攻略的 `bin.js web` 即安装形态等价物。
- 验收必须**真实启动一次**：只 `--version`/`--dump-default-config` 通过≠可用（boot 卡 require-builtin 的教训见 §C-9）。

---

## §B 从源码构建（17 步，重编/上游改版时用）

1. 环境探测：`uname -a; cat /etc/os-release; for t in node npm npx pnpm python3 gcc g++ c++ make git curl dnf cmake ninja; do command -v $t||echo MISSING; done; nproc; free -m; df -h /work`
   实测基线：make/git/curl/dnf/python3/gcc 预装；node 全家+g++/cmake/ninja 需装。
2. 装 loong64 Node：npmmirror unofficial-builds `node-v24.22.2-linux-loong64.tar.xz` → /usr/local，软链 node/npm/npx 到 /usr/bin。
3. `dnf install -y gcc-c++ musl-gcc musl-libc-static cmake ninja-build`（cmake/ninja 供 koffi 源构建回退，缺了第 16 步必炸）。
4. `npm config set registry https://registry.npmmirror.com`（构建期公共依赖走公共源即可；**loong64 独门件走专源单点 curl**，见步 12——这是与全局源策略的例外，专源对纯 JS 偶慢/滞后）；`npm i -g pnpm@11.28.5`；`pnpm config set --global store-dir /work/.pnpm-store`。
5. xlsx CDN 依赖（`https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz`）：整包/串行分片都拿不动（<1KB/s），用 **64KB Range 分片 + 24 路并发 + 每片重试**（脚本 `tools/dlpar.sh` 在产物里），得 2409319B 且 sha512 与 lockfile integrity 一致。
6. 播种 pnpm store：`/work/build/seed` 里 `pnpm add xlsx@file:/work/build/xlsx-0.20.3.tgz`；⚠ `pnpm init` 会按当前 CLI 写 packageManager/devEngines 字段（曾触发下载 pnpm 12.16.1），手工删这两字段、用仓库钉定 11.28.5。frozen-lockfile 仍会按 CDN URL 找——用 `tools/rewrite-index.py` 往 store `index.db` 的 `package_index` 插 `<integrity>\t<depPath>` 两种 key（`xlsx@<URL>` 与裸 `<URL>`），此后零 CDN 请求（depPath 格式出自 pnpm 源码 `storeIndexKey()`）。
7. `cd /work/src && CI=true pnpm install --frozen-lockfile`（约 80s；node-pty 走 node-gyp 本机编译，headers 自 unofficial-builds 正常；失败再试 `--nodedir=/usr/local` 兜底）。
8. node-pty loong64 prebuild：`build/Release/pty.node` → `prebuilds/linux-loong64/pty.node`。
9. 新增 linux-loong64 原生平台包：复制 linux-arm64 包改 name/cpu/directory/platform；`native/system/scripts/repo.mjs` 的 E_MACHINE 加 `loong64: 258`；entry optionalDependencies、lockfile importer/link 更新；**`scripts/check-workspace-constraints.ts` 的 publicNativePackages 白名单必须加**（否则 release:pack 报未登记——攻略旧版没提）。
10. 二次 `pnpm install --frozen-lockfile` 确认 Already up to date（约 30s）。
11. `pnpm --dir native/system run build:native`（约 10s）→ landlock-run（静态 musl）+ glibc/musl 两个 system.node。
12. 从专源补 3 个 loong64 binding（上游 optionalDependencies 无 loong64，pnpm 静默跳过→运行期才炸）：
    - `@rolldown%2fbinding-linux-loong64-gnu` **1.1.1 与 1.2.7 都要**
    - `lightningcss-linux-loong64-gnu-1.33.0`
    - `@oxc-resolver%2fbinding-linux-loong64-gnu-11.24.2`（上游钉 11.20.0，源只有 11.24.2；NAPI_RS_ENFORCE_VERSION_CHECK 未设可顶）
    URL 形如 `https://registry.loongnix.cn:5873/<pkg>%2f名/-/文件名.tgz`（**仅 HTTPS**）；`.node` 放进对应包目录 + 按包名目录各放一份（绕版本一致性检查）。
13. `pnpm run build:artifacts --profile official`（3 轮过：第 1/2 轮各缺一个 binding，补后第 3 轮 8 passed/0 failed，288.83s）。
14. `pnpm run release:pack --family vendor --out dist/npm-vendor`（9 包）；`--family dsh --out dist/npm`（373 包，串行 prepack 约 24min；`--concurrency` 未验证慎用）。
15. `pnpm --dir native/system run release:pack <dest> --current-platform-only`（2 包：entry + linux-loong64）。
16. `pnpm run release:verify-packed-install --family dsh --from dist/npm --from dist/npm-vendor`（第 1 轮炸 koffi 缺 cmake→装后第 2 轮过：`677 default-product packages` / `installed @deepseek-ai/dsh reports 0.2.2-alpha.2`）。
17. 归档 `/work/output/`：npm npm-vendor npm-landlock native patches tools verify.sh BUILD_NOTES.md；长命令一律 nohup 后台+轮询 rc 文件，前台只跑短命令（防墙钟误杀）。

---

## §C 踩坑速查

| # | 症状 | 根因 | 处置 |
|---|---|---|---|
| 1 | xlsx GET ETIMEDOUT / curl 只回 120KB | CDN→容器带宽极低但 Range 可达 | 64KB×37 分片 24 并发（tools/dlpar.sh） |
| 2 | seed 时下载 pnpm 12.16.1 | pnpm init 按当前 CLI 写 packageManager | 删 packageManager/devEngines 字段 |
| 3 | file: 播种后仍访问 CDN | store key=integrity\tdepPath 不匹配 | rewrite-index.py 双 key 写入 |
| 4 | Cannot find module '@rolldown/binding-linux-loong64-gnu' | optionalDeps 无 loong64，静默跳过 | 专源补 .node（两版本），进 dist/shared + 包名目录 |
| 5 | lightningcss/../.node not found → config-loader 崩 | 同上 | 专源补 1.33.0；预防性补 oxc-resolver |
| 6 | no declared binaries for linux-loong64 / Unsupported platform 警告刷屏 | 上游平台矩阵无 loong64 | 步 9 全家桶（含白名单！） |
| 7 | koffi: CMake does not seem to be available | 无 loong64 预编译回退源构建 | dnf cmake ninja-build |
| 8 | 656 包装"成功"、boot 才炸 | npm 对平台件按 os/cpu 元数据静默跳过 | 装后 grep Unsupported platform + require 冒烟（nodejs-toolchain 清点流程） |
| 9 | web 启动 host preparation failed: No usable native binding found for node-addon-require-builtin-linux-loong64-gnu (auto) | dsh boot 需钻 V8 内部布局，上游无 loong64 实现 | **首选**：§A 步 5 装专源 rb-loong64 tgz；**次选**：源码仓 `dsh-node-addon-require-builtin` 以 nodeabi 后端自编（binding.gyp 改 narb_backend=nodeabi/narb_product=require-builtin，补 node-addon-api+nodedir headers，产物铺 `node-addon-require-builtin/build/nodeabi/node-v<MODULES>-linux-loong64-gnu/require_builtin.node`，MODULES=137 为 Node24；binding 与 node ABI 必须对）；**下策**（仅试玩，改安全语义勿生产）：--expose-internals + JS shim 替换 lib/index.js |
| 10 | 装 rb 后仍 no usable binding | binding 内 ABI/backend 与当前 node 不匹配 | 核对 process.versions.modules 与 binding 目录名（napi-v9 通用、node-vNNN 锁版本） |
| 11 | landlock-run --probe rc=125 | 内核未启用 Landlock LSM | 契约内降级，非故障；真 confinement 需 lsm=landlock 宿主 |
| 12 | curl 带 token 访问 web 恒 401 | 认证=浏览器 303 换 cookie 流 | 用浏览器开日志里的 token URL |

## 已知限制

- Desktop 仅 bundle gate 过，无 Electron loong64 预编译未打包；Python SDK 单文件 exe 未构建（@yao-pkg/pkg 无 loong64 target）
- 第三方 loong64 binding 属手工补件：`pnpm install --frozen-lockfile` 会清掉，重构建需按 native/loong64-bindings/ 再铺（或正式写 overrides+私有 .npmrc）
- 公共 registry 已有同名同版本上游包，验证/部署一律本地 tarball

## 附录：dsh-web.sh 一键脚本

（放 WORKDIR 同级，`chmod +x` 后用；环境变量 PORT/WORKDIR/ART_DIR/TARBALL/NODE26 可覆盖默认值）

```bash
#!/usr/bin/env bash
# dsh-web.sh — deepseek-harness (dsh) 0.2.2-alpha.2 loong64 一键安装+启动
# 用法:
#   ./dsh-web.sh              # 环境已装好→直接启动 web :3081；已运行→跳过
#   ./dsh-web.sh --reinstall  # 从原始压缩包完整重装（删旧环境），再启动
#   ./dsh-web.sh --status     # 只看状态
#   PORT=3082 ./dsh-web.sh    # 换端口
set -euo pipefail

PORT="${PORT:-3081}"
WORKDIR="${WORKDIR:-$HOME/dsh-test}"
ART_DIR="${ART_DIR:-$HOME/dsh-0.2.2-artifacts}"
TARBALL="${TARBALL:-$HOME/deepagent-demo/T-20261030101819-0EBFBA-artifacts.tar.gz}"
NODE26="${NODE26:-$HOME/node-versions/v26.7.0/bin}"
LOGFILE="$WORKDIR/dsh-web-$PORT.log"
RB_URL="https://registry.loongnix.cn:5873/node-addon-require-builtin-linux-loong64-gnu/-/node-addon-require-builtin-linux-loong64-gnu-0.1.9.tgz"

export PATH="$NODE26:$PATH"
# sharp/libvips（LoongArch 本机特例，见 TOOLS.md）
export LD_LIBRARY_PATH="$HOME/local-vips/lib/loongarch64-linux-gnu:$HOME/local-vips/usr/lib:${LD_LIBRARY_PATH:-}"

die() { echo "❌ $*" >&2; exit 1; }

status() {
  local pid
  pid=$(pgrep -f "dsh/lib/bin.js web --port $PORT" | head -1 || true)
  if [ -n "$pid" ]; then
    echo "✅ dsh web 运行中: PID $pid → http://127.0.0.1:$PORT"
    grep -o 'http://127.0.0.1:[0-9]*/?token=[^ ]*' "$LOGFILE" 2>/dev/null | tail -1
  else
    echo "⚪ dsh web (port $PORT) 未运行"
  fi
}

start() {
  [ -x "$NODE26/node" ] || die "缺 Node >=24.9 ($NODE26)。先装 node-versions/v26.7.0"
  [ -f "$WORKDIR/node_modules/@deepseek-ai/dsh/lib/bin.js" ] || die "环境未安装: $WORKDIR（先跑 --reinstall）"

  if pgrep -f "dsh/lib/bin.js web --port $PORT" >/dev/null; then
    echo "ℹ️ 已有实例在 port $PORT，跳过启动"; status; return 0
  fi
  echo "▶ 启动 dsh web (port $PORT) ..."
  cd "$WORKDIR"
  setsid nohup node node_modules/@deepseek-ai/dsh/lib/bin.js web --port "$PORT" --no-open >> "$LOGFILE" 2>&1 < /dev/null &
  for i in $(seq 1 45); do
    sleep 2
    if ss -tln 2>/dev/null | grep -q ":$PORT "; then
      echo "✅ 监听就绪: http://127.0.0.1:$PORT"
      for t in 1 2 3 4 5; do
        url=$(grep -o 'http://127.0.0.1:[0-9]*/?token=[^ ]*' "$LOGFILE" 2>/dev/null | tail -1)
        [ -n "$url" ] && { echo "$url"; return 0; }
        sleep 1
      done
      echo "⚠️ 日志暂无 token，看 $LOGFILE"
    fi
    pgrep -f "dsh/lib/bin.js web --port $PORT" >/dev/null || break
  done
  tail -20 "$LOGFILE" >&2 || true
  die "启动失败，见 $LOGFILE"
}

install_from_scratch() {
  [ -f "$TARBALL" ] || die "找不到原始压缩包: $TARBALL"
  echo "▶ [1/5] 解包 → $ART_DIR"
  rm -rf "$ART_DIR" && mkdir -p "$ART_DIR"
  tar -xzf "$TARBALL" -C "$ART_DIR"
  A="$ART_DIR/artifacts/output"; [ -d "$A" ] || die "解包结构不对: 缺 $A"

  echo "▶ [2/5] 建工程 + 生成包列表（剔除 darwin/experimental）"
  rm -rf "$WORKDIR" && mkdir -p "$WORKDIR" && cd "$WORKDIR"
  echo '{"name":"dsh-test","version":"0.0.0","private":true}' > package.json
  python3 - "$A" <<'EOF'
import tarfile,json,glob,sys
A=sys.argv[1];ok=[]
for f in sorted(glob.glob(f'{A}/npm*/*.tgz')):
    p=json.load(tarfile.open(f).extractfile('package/package.json'))
    oss=p.get('os') or []
    if oss and 'linux' not in oss: continue
    ok.append(f)
open('tgz-list.txt','w').write('\n'.join(ok));print('will install',len(ok),'tarballs')
EOF

  echo "▶ [3/5] npm install（约 5 分钟，1.2GB，勿打断）"
  npm install --no-audit --no-fund --registry=https://registry.npmmirror.com $(cat tgz-list.txt | tr '\n' ' ')

  echo "▶ [4/5] 补 node-pty loong64 prebuild（产物已知限制，必做）"
  mkdir -p node_modules/node-pty/prebuilds/linux-loong64
  cp "$A/native/node-pty-prebuilds/linux-loong64/pty.node" node_modules/node-pty/prebuilds/linux-loong64/

  echo "▶ [5/5] 补 require-builtin loong64 binding（web 启动硬依赖，npmmirror 没有）"
  curl -fLsk -o rb-loong64.tgz "$RB_URL"
  npm install --no-audit --no-fund ./rb-loong64.tgz

  node node_modules/@deepseek-ai/dsh/lib/bin.js --version && echo "✅ 安装完成"
}

case "${1:-}" in
  --reinstall) install_from_scratch; : > "$LOGFILE" 2>/dev/null || true; start ;;
  --status)    status ;;
  ""|--start)  start ;;
  *)           die "未知参数: $1（可用: --reinstall | --status | --start）" ;;
esac
```