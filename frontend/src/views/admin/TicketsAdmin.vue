<template>
  <div class="page">
    <div class="page-title">报错工单管理</div>
    <div class="page-sub">接收云端/客户端上报的编译失败工单 · 状态流转（待处理/处理中/已解决/关闭）</div>

    <div class="card">
      <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center;flex-wrap:wrap">
        <el-select v-model="filterStatus" placeholder="全部状态" style="width:130px">
          <el-option label="全部状态" value="" />
          <el-option label="待处理" value="pending" />
          <el-option label="处理中" value="processing" />
          <el-option label="已解决" value="resolved" />
          <el-option label="已关闭" value="closed" />
        </el-select>
        <el-input v-model="searchText" placeholder="搜索工单号 / 软件名…" style="width:200px" />
        <el-button @click="loadTickets">🔍 查询</el-button>
        <span class="muted">共 {{ tickets.length }} 条</span>
      </div>

      <el-table :data="tickets" stripe style="width: 100%">
        <el-table-column prop="ticket_id" label="工单号" width="180" />
        <el-table-column label="来源" width="80">
          <template #default="{ row }">
            <el-tag :type="row.source === 'cloud' ? 'primary' : 'warning'" size="small">
              {{ row.source === 'cloud' ? '云端' : '客户端' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="task_software" label="软件" />
        <el-table-column prop="task_system" label="系统" width="140" />
        <el-table-column prop="task_id" label="关联任务" width="100" />
        <el-table-column prop="created_at" label="上报时间" width="160" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="ticketStatusType(row.status)" size="small">{{ ticketStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button 
              size="small" 
              :type="row.status === 'pending' ? 'primary' : 'default'"
              @click="openChat(row)"
            >
              {{ row.status === 'pending' ? '处理' : '查看' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '@/utils/request'

const router = useRouter()
const tickets = ref<any[]>([])
const filterStatus = ref('')
const searchText = ref('')

function ticketStatusType(status: string) {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    pending: 'warning',
    processing: 'warning',
    resolved: 'success',
    closed: 'info',
  }
  return map[status] || 'info'
}

function ticketStatusText(status: string) {
  const map: Record<string, string> = {
    pending: '待处理',
    processing: '处理中',
    resolved: '已解决',
    closed: '已关闭',
  }
  return map[status] || status
}

async function loadTickets() {
  const params: any = {}
  if (filterStatus.value) params.status = filterStatus.value
  if (searchText.value) params.search = searchText.value
  
  const res = await api.get('/admin/tickets', { params })
  tickets.value = res.data || []
}

function openChat(ticket: any) {
  router.push({ name: 'chat-admin', params: { ticketId: ticket.ticket_id } })
}

onMounted(loadTickets)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }
</style>
