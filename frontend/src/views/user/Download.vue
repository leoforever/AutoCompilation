<template>
  <div class="page">
    <div class="page-title">客户端下载入口</div>
    <div class="page-sub">龙芯 LoongArch 专属编译客户端 · 本地离线编译 · 数据零上传</div>

    <div class="notice warn" style="margin-bottom:16px">
      🔒 本地客户端编译全程离线，源码/日志/产物不上传云端，适合涉密代码场景。
    </div>

    <div class="grid g3">
      <!-- 桌面客户端 -->
      <div class="card" style="text-align:center">
        <div style="font-size:42px;margin-bottom:10px">🖥️</div>
        <h3>桌面客户端</h3>
        <p class="muted" style="margin:10px 0">图形界面 · 拖拽上传 · 实时进度可视化</p>
        <div style="margin:10px 0">
          <el-tag size="small" style="margin:3px">LoongArch 64</el-tag>
          <el-tag size="small" type="success" style="margin:3px">openEuler 24.03</el-tag>
          <el-tag size="small" type="warning" style="margin:3px">UOS V20</el-tag>
        </div>
        <div style="margin-top:16px">
          <el-button type="primary">下载 .rpm (18.4 MB)</el-button>
          <el-button style="margin-left:8px">查看版本说明</el-button>
        </div>
      </div>

      <!-- CLI 客户端 -->
      <div class="card" style="text-align:center">
        <div style="font-size:42px;margin-bottom:10px">⌨️</div>
        <h3>CLI 客户端</h3>
        <p class="muted" style="margin:10px 0">命令行工具 · 脚本集成 · CI/CD 友好</p>
        <div style="margin:10px 0">
          <el-tag size="small" style="margin:3px">LoongArch 64</el-tag>
          <el-tag size="small" type="success" style="margin:3px">Python 3.9+</el-tag>
          <el-tag size="small" type="info" style="margin:3px">无 GUI 依赖</el-tag>
        </div>
        <div style="margin-top:16px">
          <el-button type="primary">下载 tar.gz (4.2 MB)</el-button>
          <el-button style="margin-left:8px">快速上手</el-button>
        </div>
      </div>

      <!-- 使用教程 -->
      <div class="card">
        <h3>📖 使用教程</h3>
        <p class="muted" style="margin:10px 0">文档 · 示例 · 常见问题</p>
        <div style="text-align:left;font-size:13px;line-height:2.2">
          <el-link type="primary" underline="never">📄 快速开始（5 分钟）</el-link><br>
          <el-link type="primary" underline="never">📄 本地编译流程详解</el-link><br>
          <el-link type="primary" underline="never">📄 Skill 同步机制说明</el-link><br>
          <el-link type="primary" underline="never">📄 报错上报与脱敏指南</el-link><br>
          <el-link type="primary" underline="never">📄 FAQ / 排错手册</el-link>
        </div>
      </div>
    </div>

    <!-- 同步状态 -->
    <div class="card">
      <h3>🔄 同步状态</h3>
      <div class="notice ok">
        ✅ 上次同步：2026-09-21 09:45 · 拉取 128 条 Skill（通用 42 · 专用 86）· 耗时 3.2s
      </div>
      <div style="margin-top:12px">
        <el-button size="small">立即同步</el-button>
        <el-button size="small" style="margin-left:8px">查看同步日志</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g3 { grid-template-columns: repeat(3, 1fr) }
@media (max-width: 900px) { .g3 { grid-template-columns: 1fr } }
.muted { color: var(--text3); font-size: 12px }
.notice { background: var(--warning-light); border: 1px solid #fde3b5; color: #92400e; border-radius: 8px; padding: 10px 14px; font-size: 12.5px }
.notice.ok { background: var(--success-light); border-color: #b5e8d3; color: #0f7a4d }
</style>
