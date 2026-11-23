<template>
  <div class="page">
    <div class="page-title">数据统计看板</div>
    <div class="page-sub">编译 · 工单 · 经验闭环 全链路真实数据 <span v-if="generatedAt" class="gen-time">（更新于 {{ fmtTime(generatedAt) }}）</span></div>

    <!-- 加载失败：整页占位，绝不假数据 -->
    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="mb16"
      title="统计数据加载失败" :description="loadError" />
    <div v-loading="loading">
      <!-- 核心指标（全部来自 /admin/stats/overview 真实聚合） -->
      <div class="grid g4">
        <div class="stat">
          <div class="stat-head">
            <span class="lbl">编译任务</span>
            <span class="llm-tabs mini">
              <button v-for="t in TASK_TABS" :key="t.k" :class="{ on: taskTab === t.k }" @click="taskTab = t.k">{{ t.label }}</button>
            </span>
          </div>
          <div class="num">{{ kpiTask.total }}</div>
          <div class="delta up">成功 {{ kpiTask.success }}<span v-if="kpiTask.total"> ({{ Math.round(kpiTask.success / kpiTask.total * 100) }}%)</span></div>
        </div>
        <div class="stat clickable" @click="goTickets">
          <div class="num">{{ kpi.ticket_solve_rate }}%</div>
          <div class="lbl">工单解决率（共 {{ kpi.ticket_total }}）</div>
          <div class="delta muted">平均解决时长 {{ kpi.ticket_avg_solve_hours != null ? kpi.ticket_avg_solve_hours + ' 小时' : '—' }}</div>
        </div>
        <div class="stat">
          <div class="num">{{ kpi.active_users }}</div>
          <div class="lbl">活跃用户（7 天）</div>
        </div>
      </div>

      <!-- LLM 用量账（11-23 全档）：总量 KPI + 近7天走势 + 按软件 TOP5 + 最近明细 -->
      <div class="card mb16">
        <h3>🧮 LLM 用量
          <span class="llm-tabs">
            <button v-for="t in LLM_TABS" :key="t.k" :class="{ on: llmTab === t.k }" @click="llmTab = t.k">{{ t.label }}</button>
          </span>
          <span v-if="llmShown.usage_missing_rows" class="muted small"> · {{ llmShown.usage_missing_rows }} 行网关未回账</span>
        </h3>
        <div class="llm-kpi-row">
          <div><b>{{ llmShown.calls }}</b><span>调用次数</span></div>
          <div><b>{{ fmtTok(llmShown.input) }}</b><span>输入 tokens</span></div>
          <div><b>{{ fmtTok(llmShown.output) }}</b><span>输出 tokens<span class="muted small">（思考 {{ fmtTok(llmShown.reasoning) }}）</span></span></div>
          <div><b>{{ llmShown.cache_pct }}%</b><span>输入缓存命中率<span class="muted small">（省 {{ fmtTok(llmShown.cache_read) }}）</span></span></div>
          <div><b>{{ fmtDur(llmShown.seconds) }}</b><span>LLM 累计时长</span></div>
        </div>
        <div v-if="llmTrend.length" class="llm-trend">
          <div v-for="t in llmTrend" :key="t.date" class="llm-day" :title="`${t.date}: ${t.calls} 次 · 输入 ${fmtTok(t.input)} · 输出 ${fmtTok(t.output)}`">
            <div class="llm-day-bar" :style="{ height: dayBarH(t) + 'px' }"></div>
            <span>{{ t.date.slice(3) }}</span>
          </div>
        </div>
        <div v-else class="empty-hint">暂无用量数据 —— 部署后新跑的任务自动入账</div>
        <div v-if="llmTop.length || llmRecent.length" class="grid g2" style="margin-top:12px">
          <div>
            <h4 class="sub-h">按软件 TOP{{ llmTop.length }}（输入量排序）</h4>
            <el-table v-if="llmTop.length" :data="llmTop" size="small" stripe>
              <el-table-column prop="software" label="软件" show-overflow-tooltip />
              <el-table-column label="调用" width="60"><template #default="{ row }">{{ row.calls }}</template></el-table-column>
              <el-table-column label="输入" width="80"><template #default="{ row }">{{ fmtTok(row.input) }}</template></el-table-column>
              <el-table-column label="输出" width="72"><template #default="{ row }">{{ fmtTok(row.output) }}</template></el-table-column>
              <el-table-column label="LLM时长" width="84"><template #default="{ row }">{{ fmtDur(row.seconds) }}</template></el-table-column>
            </el-table>
            <div v-else class="empty-hint muted">积累中</div>
          </div>
          <div>
            <h4 class="sub-h">最近明细（≤10 单）</h4>
            <el-table v-if="llmRecent.length" :data="llmRecent" size="small" stripe>
              <el-table-column prop="software" label="软件" show-overflow-tooltip />
              <el-table-column label="状态" width="76"><template #default="{ row }">{{ row.status }}</template></el-table-column>
              <el-table-column label="调用" width="56"><template #default="{ row }">{{ row.calls }}</template></el-table-column>
              <el-table-column label="输入" width="80"><template #default="{ row }">{{ fmtTok(row.input) }}<span v-if="row.missing" title="网关未回账"> ⚠</span></template></el-table-column>
              <el-table-column label="输出" width="72"><template #default="{ row }">{{ fmtTok(row.output) }}</template></el-table-column>
              <el-table-column label="时长" width="76"><template #default="{ row }">{{ fmtDur(row.seconds) }}</template></el-table-column>
            </el-table>
            <div v-else class="empty-hint muted">积累中</div>
          </div>
        </div>
      </div>

      <div class="grid g2">
        <!-- 趋势：真实成功/总数；0 样本的天显示灰点不误导 -->
        <div class="card">
          <h3>📊 近 7 天编译成功率</h3>
          <div class="chart-container">
            <div class="bar-chart">
              <div v-for="item in trend" :key="item.date" class="bar-wrap">
                <div v-if="item.total > 0" class="bar-item" :style="{ height: barHeight(item) + '%', background: barColor(item) }"
                  :title="`${item.date}: 成功 ${item.success}/${item.total}（失败 ${item.failed}）`">
                  <span class="bar-label">{{ Math.round(rateOf(item)) }}%</span>
                </div>
                <div v-else class="bar-empty" :title="`${item.date}: 无任务`"></div>
                <span class="bar-date">{{ item.date.slice(3) }}</span>
              </div>
            </div>
          </div>
          <div class="chart-foot muted">柱高=当日成功率；悬停看 成功/总数</div>
        </div>

        <!-- 失败原因 TOP：样本 <10 明示"样本积累中"，不编造排行 -->
        <div class="card">
          <h3>🔥 失败原因 TOP{{ topErrors.length }} <span v-if="failedSamples < 10" class="muted small">（样本 {{ failedSamples }} 条，积累中）</span></h3>
          <el-table v-if="topErrors.length" :data="topErrors" stripe size="small">
            <el-table-column type="index" label="#" width="40" />
            <el-table-column prop="error_type" label="错误类型" show-overflow-tooltip />
            <el-table-column prop="count" label="次数" width="70" />
            <el-table-column label="占比" width="80">
              <template #default="{ row }">{{ row.percentage }}%</template>
            </el-table-column>
          </el-table>
          <div v-else class="empty-hint">{{ failedSamples ? '暂无归类结果' : '暂无失败样本 🎉' }}</div>
        </div>
      </div>

      <div class="grid g2">
        <!-- 工单流水线 -->
        <div class="card">
          <h3>🎫 工单流水线</h3>
          <div class="pipeline">
            <div v-for="(seg, i) in pipeline" :key="seg.status" class="pipe-stage">
              <div class="pipe-num" :style="{ color: PIPE_COLOR[seg.status] }">{{ seg.count }}</div>
              <div class="pipe-lbl">{{ PIPE_LABEL[seg.status] }}</div>
              <div v-if="i < pipeline.length - 1" class="pipe-arrow">→</div>
            </div>
          </div>
        </div>

        <!-- 经验闭环漏斗 -->
        <div class="card">
          <h3>🔄 经验闭环漏斗 <span class="muted small">（工单 → 经验 → 审批 → Skill）</span></h3>
          <div v-for="f in funnel" :key="f.stage" class="funnel-row">
            <span class="funnel-lbl">{{ f.stage }}</span>
            <div class="funnel-track"><div class="funnel-bar" :style="{ width: funnelWidth(f.count) }"></div></div>
            <span class="funnel-num">{{ f.count }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '@/utils/request'

const router = useRouter()
const loading = ref(false)
const loadError = ref('')
const generatedAt = ref('')

const kpi = ref<Record<string, any>>({
  task_windows: { all: { total: 0, success: 0 }, week: { total: 0, success: 0 }, month: { total: 0, success: 0 } },
  ticket_solve_rate: 0, ticket_total: 0, ticket_avg_solve_hours: null,
  active_users: 0,
})
const trend = ref<any[]>([])
const topErrors = ref<any[]>([])
const failedSamples = ref(0)
const pipeline = ref<any[]>([])
const funnel = ref<any[]>([])

// LLM 用量账（11-23）
const ZERO_USAGE = { calls: 0, input: 0, output: 0, reasoning: 0, cache_read: 0, cache_pct: 0, seconds: 0, usage_missing_rows: 0 }
const llmUsageAll = ref<Record<string, any>>({ ...ZERO_USAGE })
const llmUsageWeek = ref<Record<string, any>>({ ...ZERO_USAGE })
const llmUsageMonth = ref<Record<string, any>>({ ...ZERO_USAGE })
const llmTab = ref<'all' | 'week' | 'month'>('all')
// 任务卡三窗口（与 LLM 用量卡同构；口径=窗口内创建，成功=其中 success）
const taskTab = ref<'all' | 'week' | 'month'>('all')
const TASK_TABS = [{ k: 'all', label: '全部' }, { k: 'week', label: '本周' }, { k: 'month', label: '本月' }] as const
const kpiTask = computed(() => (kpi.value.task_windows || {})[taskTab.value] || { total: 0, success: 0 })
const LLM_TABS = [{ k: 'all', label: '全部' }, { k: 'week', label: '本周' }, { k: 'month', label: '本月' }] as const
const llmShown = computed(() =>
  llmTab.value === 'week' ? llmUsageWeek.value : llmTab.value === 'month' ? llmUsageMonth.value : llmUsageAll.value)
const llmTrend = ref<any[]>([])
const llmTop = ref<any[]>([])
const llmRecent = ref<any[]>([])
function fmtTok(n: number) {
  n = n || 0
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}
function fmtDur(s: number) {
  s = s || 0
  if (s >= 3600) return (s / 3600).toFixed(1) + ' 小时'
  if (s >= 60) return Math.round(s / 60) + ' 分钟'
  return s + ' 秒'
}
function dayBarH(t: any) {
  const max = Math.max(...llmTrend.value.map(x => x.input), 1)
  return Math.max(Math.round((t.input / max) * 46), 3)
}

const PIPE_LABEL: Record<string, string> = { pending: '待接单', processing: '处理中', resolved: '已解决', closed: '已关闭' }
const PIPE_COLOR: Record<string, string> = { pending: 'var(--warning)', processing: 'var(--primary)', resolved: 'var(--success)', closed: 'var(--text3)' }

function rateOf(t: any) { return t.total > 0 ? (t.success / t.total) * 100 : 0 }
function barHeight(t: any) { return Math.max(rateOf(t), 8) }
function barColor(t: any) {
  const r = rateOf(t)
  return r >= 90 ? 'var(--success)' : r >= 70 ? 'var(--primary)' : 'var(--warning)'
}
function funnelWidth(c: number) {
  const max = Math.max(...funnel.value.map(f => f.count), 1)
  return Math.max((c / max) * 100, 2) + '%'
}
function fmtTime(iso: string) {
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'))
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function goTickets() { router.push('/admin/tickets') }

async function loadStats() {
  loading.value = true
  loadError.value = ''
  try {
    const res: any = await api.get('/admin/stats/overview')
    const d = res.data || {}
    kpi.value = { ...kpi.value, ...(d.kpi || {}) }
    trend.value = d.recent_success_rate_trend || []
    topErrors.value = d.top_errors || []
    failedSamples.value = d.failed_samples ?? 0
    pipeline.value = d.ticket_pipeline || []
    funnel.value = d.experience_funnel || []
    llmUsageAll.value = { ...llmUsageAll.value, ...(d.llm_usage || {}) }
    llmUsageWeek.value = { ...llmUsageWeek.value, ...(d.llm_usage_week || {}) }
    llmUsageMonth.value = { ...llmUsageMonth.value, ...(d.llm_usage_month || {}) }
    llmTrend.value = d.llm_trend || []
    llmTop.value = d.llm_top_software || []
    llmRecent.value = d.llm_recent || []
    generatedAt.value = d.generated_at || ''
  } catch (e: any) {
    // 失败只留占位，不渲染任何假数据
    loadError.value = e?.response?.data?.detail || e?.message || '网络错误'
  } finally {
    loading.value = false
  }
}

onMounted(loadStats)
</script>

<style scoped>
.stat-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:2px }
.stat-head .lbl { margin:0 }
.llm-tabs { display:inline-flex; gap:4px; margin-left:10px; vertical-align:middle }
.llm-tabs button { border:1px solid var(--border); background:var(--card); color:var(--text2); font-size:11.5px; padding:2px 10px; border-radius:99px; cursor:pointer }
.llm-tabs button.on { background:var(--primary); border-color:var(--primary); color:#fff }
.llm-tabs.mini { display:flex; gap:3px; margin:6px 0 0 }
.llm-tabs.mini button { font-size:10.5px; padding:1px 8px }
.llm-kpi-row { display:flex; flex-wrap:wrap; gap:26px; margin:6px 0 14px }
.llm-kpi-row b { display:block; font-size:21px; font-weight:700 }
.llm-kpi-row span { display:block; font-size:12px; color:var(--text3); margin-top:2px }
.llm-trend { display:flex; align-items:flex-end; gap:14px; height:64px; padding:6px 4px 0 }
.llm-day { display:flex; flex-direction:column; align-items:center; gap:4px; font-size:11px; color:var(--text3) }
.llm-day-bar { width:26px; background:var(--primary); border-radius:4px 4px 0 0; min-height:3px; opacity:.85 }
.sub-h { font-size:13px; font-weight:600; margin:0 0 8px }
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.gen-time { font-size: 11.5px }
.mb16 { margin-bottom: 16px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g4 { grid-template-columns: repeat(3, 1fr) }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g4 { grid-template-columns: repeat(2, 1fr) } .g2 { grid-template-columns: 1fr } }
.stat { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 16px }
.stat.clickable { cursor: pointer; transition: box-shadow .2s, border-color .2s }
.stat.clickable:hover { border-color: var(--primary); box-shadow: 0 2px 8px rgba(64,158,255,.15) }
.stat .num { font-size: 26px; font-weight: 700; margin: 2px 0 }
.stat .lbl { font-size: 12.5px; color: var(--text3) }
.stat .delta { font-size: 12px }
.up { color: var(--success) }
.down { color: var(--danger) }
.muted { color: var(--text3) }
.small { font-size: 12px; font-weight: 400 }
.chart-container { height: 170px; display: flex; align-items: flex-end; padding-top: 16px }
.bar-chart { display: flex; flex: 1; align-items: flex-end; gap: 6px; height: 100% }
.bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100% }
.bar-item { width: 100%; border-radius: 4px 4px 0 0; position: relative; cursor: pointer; min-height: 8px; transition: height 0.3s }
.bar-empty { width: 100%; height: 6px; background: var(--border); border-radius: 3px }
.bar-label { position: absolute; top: -16px; left: 50%; transform: translateX(-50%); font-size: 10px; color: var(--text3); white-space: nowrap }
.bar-date { margin-top: 6px; font-size: 10.5px; color: var(--text3) }
.chart-foot { margin-top: 10px; font-size: 11.5px }
.empty-hint { padding: 28px 0; text-align: center; color: var(--text3); font-size: 13px }
.pipeline { display: flex; align-items: center; gap: 4px; padding: 10px 0 }
.pipe-stage { flex: 1; display: flex; align-items: center; position: relative }
.pipe-num { font-size: 24px; font-weight: 700 }
.pipe-lbl { font-size: 12px; color: var(--text3); margin-left: 6px }
.pipe-arrow { position: absolute; right: -10px; color: var(--border); font-size: 14px }
.funnel-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px }
.funnel-lbl { width: 68px; font-size: 12.5px; color: var(--text2); flex-shrink: 0 }
.funnel-track { flex: 1; height: 14px; background: var(--bg, #f5f7fa); border-radius: 7px; overflow: hidden }
.funnel-bar { height: 100%; background: linear-gradient(90deg, var(--primary), var(--success)); border-radius: 7px; transition: width .4s }
.funnel-num { width: 34px; text-align: right; font-size: 13px; font-weight: 600 }
</style>
