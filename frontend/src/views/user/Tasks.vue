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
        <el-table-column prop="software_name" label="软件" />
        <el-table-column prop="system_name" label="系统" width="140" />
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
        <el-table-column label="产物" width="200">
          <template #default="{ row }">
            <el-link v-if="row.artifact_path" type="primary" underline="never">
              {{ row.artifact_path }}
            </el-link>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="160">
          <template #default="{ row }">
            {{ formatDate(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240">
          <template #default="{ row }">
            <el-button size="small" @click="viewLog(row)">日志</el-button>
            <el-button v-if="row.status === 'success'" size="small" @click="downloadArtifact(row)">下载</el-button>
            <el-button v-if="row.status === 'failed'" size="small" type="danger" @click="reportTicket(row)">一键上报</el-button>
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/utils/request'
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
  tasks.value = res.data?.tasks || []
  pagination.value.total = res.data?.total || 0
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
  return new Date(dateStr).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

async function viewLog(task: any) {
  currentTask.value = task
  const res = await api.get(`/compile/tasks/${task.task_id}/log`)
  const log = res.data?.log || ''
  currentLog.value = log.split('\n').filter(Boolean).map(line => ({
    text: line,
    type: line.includes('[OK]') ? 'ok' : line.includes('[ERROR]') ? 'err' : line.includes('[WARN]') ? 'warn' : 'dim'
  }))
  logDialogVisible.value = true
}

function downloadArtifact(task: any) {
  ElMessage.info(`模拟下载：${task.artifact_path}`)
}

function reportTicket(task: any) {
  ElMessage.info(`已为任务 #${task.task_id} 生成工单`)
  window.location.href = '/tickets'
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

onMounted(loadTasks)
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
</style>
