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
          <div class="num" :style="{ color: kpi.month_success_rate >= 80 ? 'var(--success)' : 'var(--warning)' }">
            {{ kpi.month_has_data ? kpi.month_success_rate + '%' : '—' }}
          </div>
          <div class="lbl">本月编译成功率</div>
          <div v-if="kpi.month_has_data" class="delta" :class="kpi.month_delta >= 0 ? 'up' : 'down'">
            {{ kpi.month_delta >= 0 ? '↑' : '↓' }} {{ Math.abs(kpi.month_delta) }}% vs 上月
          </div>
          <div v-else class="delta muted">本月暂无任务</div>
        </div>
        <div class="stat">
          <div class="num">{{ kpi.total_tasks }}</div>
          <div class="lbl">编译任务总数</div>
          <div class="delta up">本周新增 {{ kpi.week_new_tasks }}</div>
        </div>
        <div class="stat clickable" @click="goTickets">
          <div class="num">{{ kpi.ticket_solve_rate }}%</div>
          <div class="lbl">工单解决率（共 {{ kpi.ticket_total }}）</div>
          <div class="delta muted">平均解决时长 {{ kpi.ticket_avg_solve_hours != null ? kpi.ticket_avg_solve_hours + ' 小时' : '—' }}</div>
        </div>
        <div class="stat" :class="{ clickable: kpi.pending_experiences > 0 }" @click="goReview">
          <div class="num" :style="{ color: kpi.pending_experiences > 0 ? 'var(--warning)' : 'var(--success)' }">
            {{ kpi.pending_experiences }}
          </div>
          <div class="lbl">待审经验</div>
          <div class="delta muted">{{ kpi.pending_experiences > 0 ? '点击直达审批页 →' : '暂无积压' }}</div>
        </div>
        <div class="stat">
          <div class="num">{{ kpi.agent_msgs_week }}</div>
          <div class="lbl">Agent 对话（本周）</div>
          <div class="delta muted">含流式 + 同步轮次</div>
        </div>
        <div class="stat">
          <div class="num">{{ kpi.active_users }}</div>
          <div class="lbl">活跃用户（7 天）</div>
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
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '@/utils/request'

const router = useRouter()
const loading = ref(false)
const loadError = ref('')
const generatedAt = ref('')

const kpi = ref<Record<string, any>>({
  month_success_rate: 0, month_delta: 0, month_has_data: false,
  total_tasks: 0, week_new_tasks: 0,
  ticket_solve_rate: 0, ticket_total: 0, ticket_avg_solve_hours: null,
  pending_experiences: 0, agent_msgs_week: 0, active_users: 0,
})
const trend = ref<any[]>([])
const topErrors = ref<any[]>([])
const failedSamples = ref(0)
const pipeline = ref<any[]>([])
const funnel = ref<any[]>([])

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
function goReview() { if (kpi.value.pending_experiences > 0) router.push('/admin/review') }
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
