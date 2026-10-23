<template>
  <div class="page">
    <div class="page-title">编译任务管理</div>
    <div class="page-sub">历史任务查询 · 状态筛选 · 日志回看 · 产物二次下载</div>

    <div class="card">
      <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center;flex-wrap:wrap">
        <el-select v-model="filterStatus" placeholder="全部状态" style="width:140px">
          <el-option label="全部状态" value="" />
          <el-option label="已完成" value="success" />
          <el-option label="编译中" value="compiling" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-input v-model="searchText" placeholder="搜索软件名 / 任务ID…" style="width:200px" />
        <el-button @click="loadTasks">🔍 查询</el-button>
        <el-button type="primary" @click="$router.push('/workbench')">+ 新建编译</el-button>
      </div>

      <el-table :data="tasks" stripe style="width: 100%">
        <el-table-column prop="task_id" label="ID" width="120" />
        <el-table-column prop="software_name" label="软件" min-width="120" />
        <el-table-column prop="system_name" label="系统" min-width="140" show-overflow-tooltip />
        <el-table-column label="模式" width="80">
          <template #default="{ row }">
            <el-tag :type="row.compile_mode === 'container' ? 'primary' : 'warning'" size="small">
              {{ row.compile_mode === 'container' ? '云端' : '本地' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration_seconds" label="耗时" width="80">
          <template #default="{ row }">
            {{ row.duration_seconds ? row.duration_seconds + '秒' : '-' }}
          </template>
        </el-table-column>
        <el-table-column label="产物" min-width="140">
          <template #default="{ row }">
            <span v-if="!row.artifact_path" class="muted">—</span>
            <span v-else :title="row.artifact_path" style="cursor:help">
              {{ artifactSummary(row.artifact_path, row.bundle_size) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="160">
          <template #default="{ row }">
            {{ formatDate(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="280">
          <template #default="{ row }">
            <el-button size="small" @click="viewLog(row)">日志</el-button>
            <el-button v-if="row.status === 'success'" size="small" @click="downloadArtifact(row)">下载</el-button>
            <el-button
              v-if="row.status === 'failed'"
              size="small"
              :type="existingTicketByTask.get(row.id) ? 'info' : 'danger'"
              :disabled="existingTicketByTask.has(row.id)"
              @click="reportTicket(row)">{{ existingTicketByTask.get(row.id) ? `已反馈 #${existingTicketByTask.get(row.id)}` : '一键上报' }}</el-button>
            <el-button
              v-if="row.status === 'success' || row.status === 'failed'"
              size="small"
              type="success"
              :loading="generatingDraftIds.has(row.id)"
              :disabled="generatingDraftIds.has(row.id)"
              @click="onGenerateDraft(row)">✨ 沉淀经验</el-button>
            <el-button v-if="row.status === 'submitted' || row.status === 'compiling'" size="small" type="warning" @click="cancelTask(row)">取消</el-button>
            <el-button v-if="row.status === 'success' || row.status === 'failed' || row.status === 'cancelled'" size="small" type="primary" @click="retryTask(row)">重试</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="pagination.page"
        v-model:page-size="pagination.pageSize"
        :total="pagination.total"
        layout="total, sizes, prev, pager, next"
        style="margin-top: 16px; justify-content: flex-end;"
        @change="loadTasks"
      />
    </div>

    <!-- 日志弹窗 -->
    <el-dialog v-model="logDialogVisible" :title="`📟 编译日志 #${currentTask?.task_id}`" width="700px">
      <div class="term">
        <div v-for="(line, idx) in currentLog" :key="idx" :class="['log-line', line.type]">{{ line.text }}</div>
      </div>
      <template #footer>
        <el-button @click="logDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 报错工单提交（选填描述，附日志可选） -->
    <el-dialog v-model="ticketDraftVisible" title="📨 反馈问题" width="720px" :close-on-click-modal="false">
      <el-form label-width="100px">
        <el-form-item label="关联任务">
          <el-tag>#{{ ticketDraftForm.task_id }} · {{ ticketDraftForm.software_name }}</el-tag>
          <span class="muted" style="margin-left:8px;font-size:12px">管理员复现时需要这些信息</span>
        </el-form-item>
        <el-form-item label="标题" required>
          <el-input v-model="ticketDraftForm.title" maxlength="60" show-word-limit />
        </el-form-item>
        <el-form-item label="问题描述">
          <el-input v-model="ticketDraftForm.description" type="textarea" :rows="8" resize="vertical"
                    placeholder="可留空 —— 留空时自动附 build.log 末 50 行" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="ticketDraftIncludeLog">
            描述为空时自动附 build.log 末 50 行（默认勾选）
          </el-checkbox>
        </el-form-item>
        <el-form-item>
          <div class="muted" style="font-size:12px;line-height:1.5">
            💡 提示：管理员会在容器内启动编译 agent，能实际改源码、跑命令验证、生成经验
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ticketDraftVisible = false">取消</el-button>
        <el-button type="primary" :loading="ticketDrafting" @click="submitTicketDraft">提交工单</el-button>
      </template>
    </el-dialog>

    <!-- 经验草稿预览/编辑/提交 对话框（与 Workbench 共用逻辑） -->
    <el-dialog v-model="draftVisible" title="✨ AI 生成的经验草稿" width="780px" :close-on-click-modal="false">
      <el-alert
        type="success"
        :closable="false"
        show-icon
        title="由 AI 基于本任务日志总结；请检查后修改再提交，管理员审核通过后会入库到 Skill 库"
        style="margin-bottom: 12px"
      />
      <div class="field" style="margin-bottom: 10px">
        <label>Skill 名称（slug）</label>
        <el-input v-model="draftForm.skill_name" placeholder="如 ollama-openeuler-2403" />
      </div>
      <div class="field" style="margin-bottom: 10px">
        <label>类型</label>
        <el-radio-group v-model="draftForm.skill_type">
          <el-radio value="generic">通用（工具链 / 环境 / 错误经验）</el-radio>
          <el-radio value="specific">专用（单软件编译）</el-radio>
        </el-radio-group>
      </div>
      <div class="field">
        <label>SKILL.md 内容</label>
        <el-input v-model="draftForm.skill_content" type="textarea" :rows="18" resize="vertical" />
      </div>
      <template #footer>
        <el-button @click="draftVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onSubmitDraft">提交审核</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import api from '@/utils/request'
import { fmtDateTime, parseServerDate } from '@/utils/datetime'
import { ElMessage, ElMessageBox } from 'element-plus'

const tasks = ref<any[]>([])
const filterStatus = ref('')
const searchText = ref('')
const logDialogVisible = ref(false)
const currentTask = ref<any>(null)
const currentLog = ref<any[]>([])

const pagination = ref({ page: 1, pageSize: 20, total: 0 })

async function loadTasks() {
  const params: any = {
    page: pagination.value.page,
    page_size: pagination.value.pageSize,
  }
  if (filterStatus.value) params.status = filterStatus.value
  if (searchText.value) params.search = searchText.value
  
  const res = await api.get('/compile/tasks', { params })
  // /compile/tasks 直接返 {total, tasks}（没包 ApiResponse）—— 与单 task 接口不同
  tasks.value = res.tasks || []
  pagination.value.total = res.total || 0
}

function statusType(status: string) {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    success: 'success',
    failed: 'danger',
    compiling: 'warning',
    submitted: 'info',
  }
  return map[status] || 'info'
}

function statusText(status: string) {
  const map: Record<string, string> = {
    success: '成功',
    failed: '失败',
    compiling: '编译中',
    submitted: '排队中',
  }
  return map[status] || status
}

function formatDate(dateStr: string) {
  // 完整日期时间 YYYY-MM-DD HH:MM（跨天任务也看得出来）
  // 10-23 时区修复：后端 naive UTC，parseServerDate 补 Z 按 UTC 解析再转本地
  const d = parseServerDate(dateStr)
  if (!d) return dateStr
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// artifact_path 是 JSON 字符串：文件 {name,size}；目录 {name:'x/', type:'dir', files}（不记磁盘和）
// 摘要：首条目名 + 其余计数 + 真实下载体积（bundle_size=artifacts.tar.gz 字节，无包回退清单和标≈）
function artifactSummary(artifactPath: string, bundleSize?: number): string {
  try {
    const arr = JSON.parse(artifactPath)
    if (!Array.isArray(arr) || arr.length === 0) return '—'
    const first = arr[0]
    const name = first.name || '?'
    const totalBytes = bundleSize ?? arr.reduce((s: number, a: any) => s + (a.size || 0), 0)
    const sizeStr = formatSize(totalBytes) + (bundleSize ? '' : '≈')
    if (arr.length === 1) return `${name} (${sizeStr})`
    return `${name} +${arr.length - 1}个 (${sizeStr})`
  } catch {
    return '?'
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

async function viewLog(task: any) {
  currentTask.value = task
  const res = await api.get(`/compile/tasks/${task.task_id}/log`)
  // axios 拦截器已 unwrap response.data，res 直接是 {task_id, log}
  const log = res.log || res.data?.log || ''
  // 解析 [HH:MM:SS] [LEVEL] 前缀分级（与 Workbench mapLevel 对齐；旧版按 [OK] 关键词全对不上后端的 SUCCESS/DEBUG）
  currentLog.value = log.split('\n').filter((l: string) => l.trim()).map((line: string) => {
    const m = line.match(/^\[[^\]]*\d{2}:\d{2}:\d{2}[^\]]*\]\s+\[(\w+)\]\s+(.*)$/)
    if (m) return { text: line, type: mapLevel(m[1]) }
    return { text: line, type: 'info' }
  })
  logDialogVisible.value = true
}

function mapLevel(level: string): string {
  switch (level) {
    case 'ERROR': return 'err'
    case 'WARN': return 'warn'
    case 'SUCCESS': return 'ok'
    case 'DEBUG': return 'dim'
    default: return 'info'
  }
}

function downloadArtifact(task: any) {
  const taskId = task.task_id
  if (!taskId) {
    ElMessage.warning('无 task_id')
    return
  }
  // 与 Workbench.vue 同样的 fetch+blob 下载流程
  const token = localStorage.getItem('token') || ''
  fetch(`/api/compile/tasks/${taskId}/artifacts/download`, {
    headers: token ? { 'Authorization': `Bearer ${token}` } : {},
  })
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.blob()
    })
    .then(blob => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${taskId}-artifacts.tar.gz`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      ElMessage.success('下载已开始')
    })
    .catch(err => {
      ElMessage.error('下载失败：' + (err?.message || err))
    })
}

const ticketDraftVisible = ref(false)
const ticketDrafting = ref(false)
const ticketDraftIncludeLog = ref(true)
const ticketDraftForm = reactive({
  task_id: 0 as number,
  software_name: '',
  title: '',
  description: '',
})
const existingTicketByTask = ref<Map<number, number>>(new Map())  // task.id → ticket_id

// 查 task 是否已有 pending/processing 工单（决定按钮文案）
async function loadExistingTickets() {
  try {
    const r: any = await api.get('/tickets/my')
    const map = new Map<number, number>()
    for (const t of (r.data || [])) {
      if ((t.status === 'pending' || t.status === 'processing') && t.task_id) {
        map.set(t.task_id, t.ticket_id)
      }
    }
    existingTicketByTask.value = map
  } catch {}
}

async function reportTicket(task: any) {
  // 已有工单 → 跳过去看
  const existing = existingTicketByTask.value.get(task.id)
  if (existing) {
    ElMessage.info(`任务 #${task.task_id} 已有工单 #${existing}，跳转到工单中心`)
    window.location.href = `/tickets#${existing}`
    return
  }
  // 直接打开提交对话框（不去 AI 草稿端点）
  ticketDrafting.value = false
  ticketDraftForm.task_id = task.id
  ticketDraftForm.software_name = task.software_name
  ticketDraftForm.title = `[失败] ${task.software_name}`
  ticketDraftForm.description = ''  // 选填（留空会自动附 build.log 末 50 行）
  ticketDraftIncludeLog.value = true
  ticketDraftVisible.value = true
}

async function submitTicketDraft() {
  if (!ticketDraftForm.title.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  try {
    const r: any = await api.post('/tickets/report', {
      task_id: ticketDraftForm.task_id,
      source: 'client',
      title: ticketDraftForm.title.trim(),
      description: ticketDraftForm.description.trim() || undefined,  // 选填
      include_log_tail: ticketDraftIncludeLog.value,
    })
    if (r?.code === 200) {
      ElMessage.success(`工单 #${r.data.ticket_id} 已提交，等待管理员处理`)
      ticketDraftVisible.value = false
      loadExistingTickets()
    } else {
      ElMessage.error(r?.message || '提交失败')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '提交失败')
  }
}

async function cancelTask(task: any) {
  try {
    await ElMessageBox.confirm(
      `确认取消任务 #${task.task_id}？\n容器会被停止，worker 下个检查点感知后退出。`,
      '取消编译任务',
      { confirmButtonText: '确认取消', cancelButtonText: '不取消', type: 'warning' }
    )
  } catch {
    return  // 用户取消
  }
  try {
    const r: any = await api.post(`/compile/tasks/${task.task_id}/cancel`)
    if (r?.code === 200) {
      ElMessage.success('已取消')
      loadTasks()  // 刷新列表
    } else {
      ElMessage.error(r?.message || '取消失败')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '取消失败')
  }
}

async function retryTask(task: any) {
  try {
    await ElMessageBox.confirm(
      `确认重试任务 #${task.task_id}？\n将创建新任务（参数同原任务），worker 会重新跑一遍。`,
      '重试编译任务',
      { confirmButtonText: '确认重试', cancelButtonText: '不重试', type: 'info' }
    )
  } catch {
    return
  }
  try {
    const r: any = await api.post(`/compile/tasks/${task.task_id}/retry`)
    if (r?.code === 200) {
      ElMessage.success(`已创建新任务：${r.data.new_task_id}`)
      loadTasks()  // 刷新列表
    } else {
      ElMessage.error(r?.message || '重试失败')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '重试失败')
  }
}

// ============== AI 经验草稿（手动触发，与 Workbench 同逻辑）==============
const draftVisible = ref(false)
const submitting = ref(false)
// 多任务行并行生成可能同时进行，按 id 区分 loading 态
const generatingDraftIds = ref<Set<number>>(new Set())
// 持久提示（duration: 0）：让用户知道正在调用 LLM 生成草稿（可能较慢）
const pendingMsg = ref<any>(null)
function showPendingMsg(taskId: number) {
    hidePendingMsg()
    pendingMsg.value = ElMessage({
        message: `正在为任务 #${taskId} 生成 AI 经验草稿（约 1~4 分钟，请稍候）…`,
        type: 'info',
        duration: 0,
        showClose: false,
    })
}
function hidePendingMsg() {
    if (pendingMsg.value) {
        try { pendingMsg.value.close() } catch {}
        pendingMsg.value = null
    }
}
// 当前正在操作的任务（含 system_id，提交时用）
const draftTask = ref<any>(null)
const draftForm = reactive({
  skill_name: '',
  skill_type: 'specific' as 'generic' | 'specific',
  skill_content: '',
})

// 业务 task_id → DB 主键 id
async function fetchTaskDbId(taskId: string): Promise<number> {
  const r: any = await api.get(`/compile/tasks/${taskId}`)
  const t = r?.data ?? r
  if (t && typeof t.id === 'number') return t.id
  throw new Error('无法解析 task 主键 id')
}

async function onGenerateDraft(task: any) {
  if (generatingDraftIds.value.has(task.id)) return  // 防重复点
  draftTask.value = task
  generatingDraftIds.value.add(task.id)
  // 触发响应式（Set 不会自动追踪，需替换）
  generatingDraftIds.value = new Set(generatingDraftIds.value)
  showPendingMsg(task.id)
  try {
    const dbId = await fetchTaskDbId(task.task_id)
    // 草稿生成 LLM 要 1-4 分钟，默认 30s 超时不够 → 5 分钟
    const r: any = await api.post('/experience/generate-draft', { task_id: dbId }, { timeout: 300000 })
    const data = r?.data ?? r
    draftForm.skill_name = data?.skill_name || ''
    draftForm.skill_type = (data?.skill_type === 'generic' || data?.skill_type === 'specific') ? data.skill_type : 'specific'
    draftForm.skill_content = data?.draft_content || ''
    draftVisible.value = true
  } catch (e: any) {
    const msg = e?.response?.data?.detail || e?.response?.data?.message || e?.message || String(e)
    ElMessage.error('草稿生成失败：' + msg)
  } finally {
    generatingDraftIds.value.delete(task.id)
    generatingDraftIds.value = new Set(generatingDraftIds.value)
    hidePendingMsg()
  }
}

async function onSubmitDraft() {
  const task = draftTask.value
  if (!task) return
  if (!draftForm.skill_name.trim()) { ElMessage.warning('请填写 Skill 名称'); return }
  if (!draftForm.skill_content.trim() || draftForm.skill_content.length < 10) {
    ElMessage.warning('SKILL.md 内容不能为空（至少 10 字）'); return
  }
  submitting.value = true
  try {
    const dbId = await fetchTaskDbId(task.task_id)
    await api.post('/experience/submit', {
      task_id: dbId,
      skill_name: draftForm.skill_name.trim(),
      skill_type: draftForm.skill_type,
      skill_content: draftForm.skill_content,
      system_id: task.system_id,
    })
    ElMessage.success('已提交经验沉淀请求，等待管理员审核')
    draftVisible.value = false
  } catch (e: any) {
    const msg = e?.response?.data?.detail || e?.response?.data?.message || e?.message || String(e)
    ElMessage.error('提交失败：' + msg)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadTasks()
  loadExistingTickets()
})
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }
.term { background: #0f172a; color: #cbd5e1; border-radius: 8px; padding: 14px; font-family: monospace; font-size: 12.5px; max-height: 400px; overflow: auto; line-height: 1.7 }
.log-line.ok { color: #4ade80 }
.log-line.err { color: #f87171 }
.log-line.warn { color: #fbbf24 }
.log-line.dim { color: #64748b }
.log-line.info { color: #cbd5e1 }
</style>
