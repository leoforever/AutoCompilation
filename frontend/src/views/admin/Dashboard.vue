<template>
  <div class="page">
    <div class="page-title">数据统计看板</div>
    <div class="page-sub">编译成功率 · 报错 TOP 问题 · 经验贡献 · 用户活跃度</div>

    <!-- 核心指标 -->
    <div class="grid g4">
      <div class="stat">
        <div class="num" style="color:var(--success)">{{ stats.success_rate }}%</div>
        <div class="lbl">本月编译成功率</div>
        <div class="delta up">↑ 3.2% vs 上月</div>
      </div>
      <div class="stat">
        <div class="num">{{ stats.total_tasks }}</div>
        <div class="lbl">总编译任务</div>
        <div class="delta up">↑ 18% 本周新增</div>
      </div>
      <div class="stat">
        <div class="num">{{ stats.total_experiences }}</div>
        <div class="lbl">经验入库</div>
        <div class="delta up">↑ 12 条今日审核通过</div>
      </div>
      <div class="stat">
        <div class="num">{{ stats.active_users }}</div>
        <div class="lbl">活跃用户</div>
        <div class="delta down">↓ 2 人离线</div>
      </div>
    </div>

    <div class="grid g2">
      <!-- 成功率趋势图 -->
      <div class="card">
        <h3>📊 近 30 天编译成功率趋势</h3>
        <div class="chart-container" ref="chartRef">
          <div class="bar-chart">
            <div 
              v-for="(item, idx) in trendData" 
              :key="idx"
              class="bar-item"
              :style="{ height: item.rate + '%', background: barColor(item.rate) }"
              :title="`${item.date}: ${item.rate}%`"
            >
              <span class="bar-label">{{ item.rate }}%</span>
            </div>
          </div>
          <div class="chart-labels">
            <span v-for="label in chartLabels" :key="label">{{ label }}</span>
          </div>
        </div>
      </div>

      <!-- 报错 TOP5 -->
      <div class="card">
        <h3>🔥 报错 TOP5 问题</h3>
        <el-table :data="topErrors" stripe size="small">
          <el-table-column type="index" label="#" width="40" />
          <el-table-column prop="error_type" label="错误类型" />
          <el-table-column prop="count" label="发生次数" width="80" />
          <el-table-column prop="percentage" label="占比" width="60">
            <template #default="{ row }">{{ row.percentage }}%</template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <div class="grid g2">
      <!-- 集群节点状态 -->
      <div class="card">
        <h3>🖥️ 编译集群节点状态</h3>
        <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:8px">
          <el-tag v-for="node in nodes" :key="node.id" :type="nodeStatusType(node.status)" effect="light">
            ● {{ node.name }} {{ node.statusText }}
          </el-tag>
        </div>
      </div>

      <!-- Skill 同步概览 -->
      <div class="card">
        <h3>🔄 Skill 同步概览</h3>
        <div class="notice ok">
          ✅ 上次推送：2026-09-21 09:45 · 向 47 个客户端推送更新（128 条）
        </div>
        <div style="margin-top:10px;font-size:13px;color:var(--text2)">
          <div>通用 Skill：<span style="font-weight:600">{{ syncSummary.generic_total }}</span> 条 · <span style="color:var(--success)">已同步 {{ syncSummary.generic_synced }} 条</span></div>
          <div>专用 Skill：<span style="font-weight:600">{{ syncSummary.specific_total }}</span> 条 · <span style="color:var(--success)">已同步 {{ syncSummary.specific_synced }} 条</span></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/utils/request'

const stats = ref({
  success_rate: 0,
  total_tasks: 0,
  total_experiences: 0,
  active_users: 0,
})

const trendData = ref<{ date: string; rate: number; total: number }[]>([])
const topErrors = ref<{ error_type: string; count: number; percentage: number }[]>([])
const nodes = ref<{ id: string; name: string; status: string; statusText: string }[]>([])
const syncSummary = ref({ generic_total: 0, generic_synced: 0, specific_total: 0, specific_synced: 0 })

const chartLabels = ['9/1', '9/7', '9/14', '9/21']

function barColor(rate: number) {
  if (rate >= 90) return 'var(--success)'
  if (rate >= 85) return 'var(--primary)'
  return 'var(--warning)'
}

function nodeStatusType(status: string) {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    idle: 'success',
    busy: 'warning',
    offline: 'danger',
  }
  return map[status] || 'info'
}

async function loadStats() {
  const res = await api.get('/admin/stats/overview')
  stats.value = res.data || {}
  trendData.value = res.data?.recent_success_rate_trend || []
  topErrors.value = res.data?.top_errors || [
    { error_type: '依赖缺失：pcre-devel 版本不匹配', count: 47, percentage: 18.2 },
    { error_type: 'OOM 内存溢出（loongarch 镜像默认 2G）', count: 38, percentage: 14.7 },
    { error_type: '网络超时：源码下载失败', count: 29, percentage: 11.2 },
    { error_type: '交叉编译器 loongarch64-linux-gnu 未安装', count: 21, percentage: 8.1 },
    { error_type: 'CMake 配置阶段报错', count: 17, percentage: 6.6 },
  ]
  nodes.value = res.data?.cluster_nodes || [
    { id: '1', name: 'la-node-01', status: 'idle', statusText: '空闲' },
    { id: '2', name: 'la-node-02', status: 'idle', statusText: '空闲' },
    { id: '3', name: 'la-node-03', status: 'busy', statusText: '占用中 (1/8)' },
    { id: '4', name: 'la-node-04', status: 'offline', statusText: '离线' },
  ]
  syncSummary.value = res.data?.skill_sync_summary || { generic_total: 42, generic_synced: 41, specific_total: 86, specific_synced: 83 }
}

onMounted(loadStats)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g4 { grid-template-columns: repeat(4, 1fr) }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g4 { grid-template-columns: repeat(2, 1fr) } .g2 { grid-template-columns: 1fr } }
.stat { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 16px }
.stat .num { font-size: 26px; font-weight: 700; margin: 2px 0 }
.stat .lbl { font-size: 12.5px; color: var(--text3) }
.stat .delta { font-size: 12px }
.up { color: var(--success) }
.down { color: var(--danger) }
.chart-container { height: 180px; display: flex; align-items: flex-end; gap: 6px; padding-top: 20px }
.bar-chart { display: flex; flex: 1; align-items: flex-end; gap: 4px }
.bar-item { flex: 1; border-radius: 4px 4px 0 0; position: relative; cursor: pointer; min-height: 4px; transition: height 0.3s }
.bar-label { position: absolute; bottom: -18px; left: 50%; transform: translateX(-50%); font-size: 10px; color: var(--text3); white-space: nowrap }
.chart-labels { display: flex; justify-content: space-between; margin-top: 8px; color: var(--text3); font-size: 11px }
.notice { background: var(--success-light); border: 1px solid #b5e8d3; color: #0f7a4d; border-radius: 8px; padding: 10px 14px; font-size: 12.5px }
</style>
