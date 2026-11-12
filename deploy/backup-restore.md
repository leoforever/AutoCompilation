# 平台备份与恢复（deploy/platform-backup.sh）

一台一条链：**旧机 backup → 传包 → 新机拉最新代码 → restore**。
设计语义 = "代码永远是 git 最新，机器状态（凭据/库/数据/证书）回到备份时点"。

## 备份哪些东西

| 内容 | 来源路径 | 恢复方式 |
|---|---|---|
| 凭据配置（DB 口令、AI 三件套、JWT 密钥） | backend/.env.compile | 覆盖（旧文件转存 .pre-restore） |
| 数据库整库 | pg_dump 全量 SQL | drop + create + 灌回 + 行数验证 |
| 产物树（交付物/日志/BUILD_NOTES） | artifacts/ | rsync --delete 回到备份时点 |
| 货架正本 + meta + INDEX | cache/ | 同上 |
| 轮换前库备份 | backend/cache/ | 同上 |
| HTTPS 证书 | /etc/nginx/ssl | 解包覆盖 |
| 主 nginx.conf（含平台 include 行） | /etc/nginx/ | 覆盖（旧文件转存 .pre-restore） |
| skills/ 与 backend/config.yaml 快照 | 项目内 | **默认不恢复**（保持 git 最新）；RESTORE_GITTRACKED=1 才覆盖 |
| 元信息（备份时 git 版本/未提交清单） | meta.txt | 仅供恢复后对账 |

不备份：node_modules、dist（重建）、日志 logs/（非运行状态）。
入库的模板与平台 nginx conf 走 git，不进包。

## 如何备份（源机执行）

```bash
cd /root/.openclaw/workspace/compile-platform      # .209 路径为 /root/AutoCompilation
bash deploy/platform-backup.sh backup              # 全量（含产物树，慢），默认输出 /root
EXCLUDE_ARTIFACTS=1 bash deploy/platform-backup.sh backup           # 快档（不含产物）
bash deploy/platform-backup.sh backup /data/backups                 # 指定输出目录（根盘紧张时）
bash deploy/platform-backup.sh backup /mnt/nas/week11.tar.gz        # 指定完整文件名
```

输出盘不足会**预检报错、不写半包**（预检按"中转+成品双倍 + 1GB 富余"估）。

产物是 `/root/platform-backup-<主机>-<时间>.tar.gz`，自动 600 权限——**包内含明文凭据，
不要放共享目录，传输后删中转副本**。

## 如何恢复（目标机执行）

```bash
cd /root/AutoCompilation && git pull               # 1) 代码先最新
scp 源机:/root/platform-backup-*.tar.gz /root/     # 2) 拿包
bash deploy/platform-backup.sh restore /root/包.tar.gz --dry-run   # 3) 先看九步计划（不改任何东西）
bash deploy/platform-backup.sh restore /root/包.tar.gz             # 4) 真恢复
```

真恢复顺序：停服务 → env 归位 → 经 postgres 超级通道把库口令对齐 env →
清库重灌 + 验证 → 数据目录 rsync → 证书/主 nginx.conf → setup.sh install（幂等自愈
pg_hba / 拷 nginx 模板 / 缺 JWT 自动生成）→ restart。

口令对齐这一步专治"env 与库脱钩"（git pull 后 env 重生成、库里还是旧口令）
——rotate-db-password 在这种场景会卡住，restore 用超级用户通道直接对齐。

## 口径与边界（如实）

- restore 真实灌库路径首次使用请先 --dry-run；drop database 会清目标机现库，
  中途失败有 meta.txt + .pre-restore 两处转存可人工回退。
- 备份失败最常见原因是 pg_dump 连不上（env 与库脱钩）：脚本会告警继续备文件部分，
  但包内无 db.sql——先手工对齐口令或在有健康库的机器上做基准备份。
- 验证现状：backup 已实弹（201 今日快照）；restore 演练到 dry-run 九步全对，
  真灌库在目标机首用时完成首战验证。
