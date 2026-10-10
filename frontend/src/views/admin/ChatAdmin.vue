<template>
  <div class="page">
    <div class="page-title">对话式人工干预</div>
    <div class="page-sub">针对单个工单，与用户在线对话 · 定位编译问题 · 给出解决方案 · 远程指导</div>

    <div class="grid g2">
      <!-- 对话面板 -->
      <div class="card">
        <h3>💬 工单 #{{ ticketId }}</h3>
        
        <!-- AI 辅助分析 -->
        <div class="ai-card" style="margin-bottom:14px">
          <b>🤖 AI 辅助分析：</b><br>
          根因：pcre 库版本不匹配（系统默认 10.42，软件要求 ≥ 10.40）<br>
          建议修复命令：<code>dnf install pcre2-devel</code>
        </div>

        <div class="chat-box" ref="chatBox">
          <div v-for="(msg, idx) in messages" :key="idx" :class="['msg', msg.role]">
            <div class="who">
              {{ msg.role === 'admin' ? '我（管理员）' : '用户' }} {{ formatDate(msg.created_at) }}
            </div>
            <div class="content" v-html="msg.content"></div>
          </div>
        </div>

        <div style="margin-top:12px;display:flex;gap:8px">
          <el-input v-model="newMessage" placeholder="输入回复..." style="flex:1" @keyup.enter="sendMessage" />
          <el-button type="primary" size="small" @click="sendMessage">发送</el-button>
        </div>

        <div style="margin-top:10px;display:flex;gap:10px">
          <el-button type="success" size="small" @click="resolveTicket">✅ 标记已解决</el-button>
          <el-button size="small" @click="convertToSkill">📝 整理为 Skill</el-button>
        </div>
      </div>

      <!-- 工单详情 -->
      <div class="card">
        <h3>📋 工单详情</h3>
        <div class="ticket-detail">
          <div><b>软件：</b>nginx v1.25</div>
          <div><b>系统：</b>openEuler 24.03 (LoongArch)</div>
          <div><b>关联任务：</b>#1022</div>
          <div><b>上报时间：</b>2026-09-21 10:12</div>
          <div><b>设备架构：</b>loongarch64</div>
          <div>
            <b>日志（脱敏）：</b><br>
            <code>configure: error: the HTTP rewrite module requires the PCRE library</code>
          </div>
          <div><b>状态：</b><el-tag type="warning" size="small">处理中</el-tag></div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import api from '@/utils/request'
import { ElMessage } from 'element-plus'

const route = useRoute()
const ticketId = ref(route.params.ticketId as string)
const messages = ref<any[]>([])
const newMessage = ref('')
const chatBox = ref<HTMLElement>()

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

async function loadMessages() {
  const res = await api.get(`/tickets/${ticketId.value}`)
  messages.value = res.data?.messages || []
  await nextTick()
  scrollToBottom()
}

async function sendMessage() {
  if (!newMessage.value.trim()) return
  
  const content = newMessage.value
  newMessage.value = ''
  
  messages.value.push({ role: 'admin', content, created_at: new Date().toISOString() })
  scrollToBottom()
  
  await api.post(`/tickets/${ticketId.value}/messages`, { content })
}

function scrollToBottom() {
  if (chatBox.value) {
    chatBox.value.scrollTop = chatBox.value.scrollHeight
  }
}

function resolveTicket() {
  ElMessage.success('✅ 工单已标记为已解决')
}

function convertToSkill() {
  ElMessage.info('📝 已将本次对话内容整理为标准 OpenClaw Skill，进入审核队列')
}

onMounted(loadMessages)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g2 { grid-template-columns: 1fr } }
.ai-card { background: linear-gradient(135deg, #eff6ff, #f0fdf4); border: 1px solid #bfdbfe; border-radius: 10px; padding: 14px; font-size: 13px }
.ai-card b { color: #1d4ed8 }
.chat-box { max-height: 340px; overflow-y: auto; padding: 6px; display: flex; flex-direction: column; gap: 10px }
.msg { max-width: 80%; padding: 10px 13px; border-radius: 12px; font-size: 13px }
.msg.admin { align-self: flex-end; background: var(--primary); color: #fff; border-bottom-right-radius: 4px }
.msg.user { align-self: flex-start; background: #f1f5f9; color: var(--text); border-bottom-left-radius: 4px }
.who { font-size: 11px; opacity: 0.7; margin-bottom: 3px }
.content { line-height: 1.6 }
.content code { background: rgba(0,0,0,0.1); padding: 2px 6px; border-radius: 4px; font-size: 12px }
.ticket-detail { font-size: 13px; line-height: 2 }
.ticket-detail code { background: #f0f1f3; padding: 1px 6px; border-radius: 4px; font-size: 12px; font-family: monospace; color: #c7254e }
</style>
