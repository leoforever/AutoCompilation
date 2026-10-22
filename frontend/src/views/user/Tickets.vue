<template>
  <div class="page">
    <div class="page-title">问题上报中心</div>
    <div class="page-sub">失败任务一键上报 · 查看工单进度 · 管理员对话回复</div>

    <div class="grid g2">
      <!-- 我的上报记录 -->
      <div class="card">
        <h3>📥 我的上报记录</h3>
        <el-table :data="tickets" stripe style="width: 100%">
          <el-table-column prop="ticket_id" label="工单号" width="180" />
          <el-table-column prop="task_id" label="关联任务" width="100" />
          <el-table-column prop="title" label="标题" />
          <el-table-column prop="created_at" label="上报时间" width="160">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="ticketStatusType(row.status)" size="small">{{ ticketStatusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <!-- T8: 管理员新回复红点（打开对话即清零） -->
              <el-badge :value="row.admin_unread" :hidden="!row.admin_unread" :offset="[4, -2]">
                <el-button size="small" @click="openChat(row)">查看对话</el-button>
              </el-badge>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 工单对话面板 -->
      <div class="card" v-if="currentTicket">
        <h3>💬 工单 #{{ currentTicket.ticket_id }}</h3>
        <div class="chat-box" ref="chatBox">
          <div v-for="(msg, idx) in messages" :key="idx" :class="['msg', msg.role]">
            <div class="who">{{ msg.role === 'user' ? '我' : '管理员' }} {{ formatDate(msg.created_at) }}</div>
            <div class="content">{{ msg.content }}</div>
          </div>
        </div>
        <div v-if="currentTicket.status !== 'closed'" style="margin-top:12px;display:flex;gap:8px">
          <el-input v-model="newMessage" placeholder="输入回复..." style="flex:1" @keyup.enter="sendMessage" />
          <el-button type="primary" size="small" @click="sendMessage">发送</el-button>
        </div>
        <div v-else class="muted" style="margin-top:12px;font-size:13px">工单已关闭，对话结束</div>
      </div>
      <div class="card" v-else style="display:flex;align-items:center;justify-content:center;min-height:300px">
        <span class="muted">选择左侧工单查看对话</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/utils/request'

const tickets = ref<any[]>([])
const currentTicket = ref<any>(null)
const messages = ref<any[]>([])
const newMessage = ref('')
const chatBox = ref<HTMLElement>()

async function loadTickets() {
  const res = await api.get('/tickets/my')
  tickets.value = res.data || []
}

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

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

async function openChat(ticket: any) {
  currentTicket.value = ticket
  const res: any = await api.get(`/tickets/${ticket.ticket_id}`)
  messages.value = res.data?.messages || []
  // T8: 打开面板即把管理员回复标已读，红点/角标同步清零
  if (ticket.admin_unread > 0) {
    try { await api.post(`/tickets/${ticket.ticket_id}/read`); ticket.admin_unread = 0 } catch { /* 非关键路径 */ }
  }
  await nextTick()
  scrollToBottom()
}

async function sendMessage() {
  if (!newMessage.value.trim() || !currentTicket.value) return

  const content = newMessage.value
  newMessage.value = ''

  // 本地乐观显示用户消息
  messages.value.push({ role: 'user', content, created_at: new Date().toISOString() })
  scrollToBottom()

  // 调真接口（/tickets/{id}/message，单数）
  try {
    const r: any = await api.post(
      `/tickets/${currentTicket.value.ticket_id}/message`,
      null,
      { params: { content } }
    )
    if (r?.code && r.code !== 200) {
      ElMessage.error(r?.message || '发送失败')
    }
    // 拉一次详情刷新消息列表（拿到 id/timestamp）
    const det: any = await api.get(`/tickets/${currentTicket.value.ticket_id}`)
    messages.value = det.data?.messages || []
    scrollToBottom()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '发送失败')
  }
}

function scrollToBottom() {
  if (chatBox.value) {
    chatBox.value.scrollTop = chatBox.value.scrollHeight
  }
}

onMounted(loadTickets)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g2 { grid-template-columns: 1fr } }
.chat-box { max-height: 340px; overflow-y: auto; padding: 6px; display: flex; flex-direction: column; gap: 10px }
.msg { max-width: 80%; padding: 10px 13px; border-radius: 12px; font-size: 13px }
.msg.user { align-self: flex-end; background: var(--primary); color: #fff; border-bottom-right-radius: 4px }
.msg.admin { align-self: flex-start; background: #f1f5f9; color: var(--text); border-bottom-left-radius: 4px }
.who { font-size: 11px; opacity: 0.7; margin-bottom: 3px }
.muted { color: var(--text3); font-size: 12px }
</style>
