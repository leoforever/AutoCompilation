<template>
  <div class="page">
    <div class="card">
      <h3>📋 操作日志审计</h3>
      <div style="display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap">
        <el-select v-model="auditFilter.user_id" placeholder="用户" clearable filterable size="small" style="width:150px" @change="loadAuditLogs(1)">
          <el-option v-for="u in userOptions" :key="u.user_id" :label="u.username" :value="u.user_id" />
        </el-select>
        <el-select v-model="auditFilter.action" placeholder="操作类型" clearable size="small" style="width:170px" @change="loadAuditLogs(1)">
          <el-option v-for="a in ACTION_OPTIONS" :key="a.value" :label="a.label" :value="a.value" />
        </el-select>
        <el-select v-model="auditFilter.target_type" placeholder="对象类型" clearable size="small" style="width:130px" @change="loadAuditLogs(1)">
          <el-option v-for="t in ['compile_task','ticket','experience','skill','user','system','auth']" :key="t" :label="t" :value="t" />
        </el-select>
        <el-input v-model="auditFilter.q" placeholder="搜对象号/细节" clearable size="small" style="width:200px" @keyup.enter="loadAuditLogs(1)" />
        <el-button size="small" @click="loadAuditLogs(1)">查询</el-button>
      </div>
      <el-table :data="auditLogs" stripe style="width: 100%" size="small" v-loading="loading">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ fmtAuditTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="username" label="用户" width="110" />
        <el-table-column prop="action" label="操作" width="170">
          <template #default="{ row }">
            <el-tag size="small" :type="auditActionTag(row.action)" effect="plain">{{ auditActionLabel(row.action) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="对象" min-width="180">
          <template #default="{ row }">{{ row.target_key || row.target_type || '-' }}</template>
        </el-table-column>
        <el-table-column prop="details" label="细节" min-width="220" show-overflow-tooltip />
        <el-table-column prop="ip_address" label="IP" width="130" />
      </el-table>
      <el-pagination
        v-model:current-page="auditPage.page"
        :page-size="auditPage.pageSize"
        :total="auditPage.total"
        layout="total, prev, pager, next"
        style="margin-top: 10px; justify-content: flex-end"
        @current-change="(p:number)=>loadAuditLogs(p)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/utils/request'
import { fmtDateTime } from '@/utils/datetime'

const auditLogs = ref<any[]>([])
const loading = ref(false)
const auditFilter = ref<{ user_id: number | ''; action: string; target_type: string; q: string }>({ user_id: '', action: '', target_type: '', q: '' })
// 用户筛选选项：admin 用户列表（含登录失败等无主行不在此维度——user_id=NULL 行
// 只有"不限用户"时可见，选中具体用户即看其本人操作）
const userOptions = ref<any[]>([])
async function loadUserOptions() {
  try {
    const r: any = await api.get('/admin/users', { params: { include_deleted: false } })
    userOptions.value = r?.data || []   // 拦截器 unwrap 后 {code,data:[...]}，Users.vue 同款消费
  } catch { /* 选项拉取失败不阻塞审计页主体 */ }
}
const auditPage = ref({ page: 1, pageSize: 20, total: 0 })

async function loadAuditLogs(page = 1) {
  loading.value = true
  auditPage.value.page = page
  const params: any = { page, page_size: auditPage.value.pageSize }
  if (auditFilter.value.user_id !== '') params.user_id = auditFilter.value.user_id
  if (auditFilter.value.action) params.action = auditFilter.value.action
  if (auditFilter.value.target_type) params.target_type = auditFilter.value.target_type
  if (auditFilter.value.q) params.q = auditFilter.value.q.trim()
  try {
    const r: any = await api.get('/admin/audit-logs', { params })
    auditLogs.value = r?.data?.items || []
    auditPage.value.total = r?.data?.total || 0
  } catch (e) { console.warn('[audit] 加载失败', e) }
  finally { loading.value = false }
}

function fmtAuditTime(s0: string) {
  return s0 ? fmtDateTime(s0, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '-'
}
const ACTION_LABELS: Record<string, string> = {
  login_failed: '登录失败', login_success: '登录', user_register: '注册', password_changed: '改密',
  task_submit: '提交编译', task_cancel: '取消任务', task_retry: '重试任务',
  artifact_downloaded: '下载产物', log_downloaded: '下载日志',
  ticket_report: '上报工单', ticket_message_user: '工单发言', ticket_closed: '关单',
  experience_submit: '提交经验', experience_approve: '经验通过', experience_reject: '经验驳回',
  draft_generated: '生成草稿', admin_password_reset: '管理员重置密码',
  qa_ask: '问答提问', qa_save_draft: '问答存草稿',
  user_created: '建用户', user_updated: '改用户', user_deleted: '注销用户', user_restored: '恢复用户',
  skill_created: '建Skill', skill_updated: '改Skill', skill_deleted: '删Skill', skill_reconcile: 'Skill对账',
  system_created: '建系统', system_updated: '改系统', system_deleted: '删系统',
}
function auditActionLabel(a: string) { return ACTION_LABELS[a] || a }
function auditActionTag(a: string) {
  if (a.includes('failed') || a.includes('deleted') || a.includes('reject') || a.includes('cancel')) return 'danger'
  if (a.includes('approve') || a.includes('success') || a.includes('created') || a.includes('submit')) return 'success'
  if (a.includes('updated') || a.includes('retry') || a.includes('reset') || a.includes('downloaded')) return 'warning'
  return 'info'
}
const ACTION_OPTIONS = Object.entries(ACTION_LABELS).map(([value, label]) => ({ value, label }))

onMounted(() => { loadAuditLogs(1); loadUserOptions() })
</script>

<style scoped>
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
</style>
