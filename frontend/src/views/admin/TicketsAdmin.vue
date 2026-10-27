<template>
  <div class="page">
    <div class="page-title">📨 报错工单管理</div>
    <div class="page-sub">用户提单 → 接单后容器内启动 DeepAgent → 多轮对话解决 → 沉淀为 Skill</div>

    <div class="card">
      <div class="toolbar">
        <el-radio-group v-model="filterStatus" @change="loadTickets">
          <el-radio-button value="">全部 ({{ counts.all }})</el-radio-button>
          <el-radio-button value="pending">待处理 ({{ counts.pending }})</el-radio-button>
          <el-radio-button value="processing">处理中 ({{ counts.processing }})</el-radio-button>
          <el-radio-button value="resolved">已解决 ({{ counts.resolved }})</el-radio-button>
          <el-radio-button value="closed">已关闭 ({{ counts.closed }})</el-radio-button>
        </el-radio-group>
        <el-input v-model="searchText" placeholder="搜索工单号 / 标题 / 软件名 / 提交人" style="width:300px" clearable @keyup.enter="loadTickets" />
        <el-button type="primary" @click="loadTickets">🔍 查询</el-button>
        <span class="muted">显示 {{ tickets.length }} 条</span>
      </div>

      <el-table :data="tickets" stripe style="width: 100%" v-loading="loading">
        <el-table-column prop="ticket_id" label="工单号" width="100">
          <template #default="{ row }">
            <el-link type="primary" underline="never" @click="openDetail(row)">#{{ row.ticket_id }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="username" label="提交人" width="100">
          <template #default="{ row }">
            {{ row.username }}
            <el-tag v-if="row.username === '已注销'" type="info" size="small" style="margin-left:4px">已注销</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="解决说明" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.resolution" class="resolution-cell">{{ row.resolution }}</span>
            <span v-else-if="row.status === 'resolved' || row.status === 'closed'" class="muted">（未填）</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="上报时间" width="160">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row)">查看</el-button>
            <el-button v-if="row.status === 'pending'" size="small" type="primary" @click="quickStatus(row, 'processing')">接单</el-button>
            <el-button v-else-if="row.status === 'processing'" size="small" type="success" @click="openResolveDialog(row)">标记解决</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 详情抽屉：上半任务详情 / 中日志 / 下半 Agent 工作台 -->
    <el-drawer v-model="detailVisible" :title="`工单 #${current?.ticket_id || ''} — ${current?.title || ''}`" size="80%" :close-on-click-modal="false" @close="stopPhasePolling()">
      <template #header>
        <div style="display:flex;align-items:center;gap:12px;width:100%">
          <el-button size="small" @click="closeDetail" :icon="undefined">
            ← 返回列表
          </el-button>
          <span style="font-size:16px;font-weight:600">工单 #{{ current?.ticket_id || '' }} — {{ current?.title || '' }}</span>
        </div>
      </template>
      <div v-if="current" class="detail">
        <!-- 2026-10-19：明显的"返回工单列表"按钮（不依赖 el-drawer #header slot） -->
        <div class="back-bar">
          <el-button type="default" size="default" @click="closeDetail">
            ← 返回工单列表
          </el-button>
        </div>
        <!-- 顶部状态条：一眼看清工单状态 + 接单状态 -->
        <div class="topbar">
          <el-tag :type="statusType(current.status)" size="large" effect="dark">
            {{ statusText(current.status) }}
          </el-tag>
          <span class="muted" style="margin-left:12px">
            提交人: <b>{{ current.username }}</b> · 上报时间: {{ formatDate(current.created_at) }}
          </span>
          <!-- 2026-10-19：当前阶段（实时变化，不进历史气泡） -->
          <span v-if="currentPhaseLabel" class="phase-pill" :class="currentPhaseClass">
            {{ currentPhaseLabel }}
          </span>
          <el-tag v-if="current.status === 'pending'" type="warning" effect="plain" style="margin-left:12px">
            ⚠️ 未接单 — 点击下方"接单"启动容器内 Agent
          </el-tag>
          <el-tag v-else-if="current.status === 'processing' && current.agent_active" type="success" effect="plain" style="margin-left:12px">
            🟢 Agent 工作中（容器 {{ current.container_id_short }}）
          </el-tag>
          <el-tag v-else-if="current.status === 'processing'" type="warning" effect="plain" style="margin-left:12px">
            🟡 Agent 启动中…（容器建中 + 拉源码 + DeepAgent 初始化）
          </el-tag>
        </div>

        <!-- 1. 任务详情（admin 复现需要） -->
        <h4>📋 任务详情（复现用）</h4>
        <el-descriptions v-if="current.task" :column="2" border size="small">
          <el-descriptions-item label="任务ID">{{ current.task.task_str_id }}（db_id={{ current.task.db_id }}）</el-descriptions-item>
          <el-descriptions-item label="软件">{{ current.task.software_name }}</el-descriptions-item>
          <el-descriptions-item label="系统">{{ current.task.system_name || `id=${current.task.system_id}` }}</el-descriptions-item>
          <el-descriptions-item label="提交时间">{{ formatDate(current.task.created_at) }} · {{ current.task.duration_seconds }}s</el-descriptions-item>
          <el-descriptions-item label="源类型">
            <el-tag :type="current.task.source_type === 'git' ? 'primary' : 'warning'" size="small">
              {{ current.task.source_type }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="current.task.source_type === 'git'" label="git 地址" :span="2">
            <el-link v-if="current.task.git_url" :href="current.task.git_url" target="_blank" type="primary">{{ current.task.git_url }}</el-link>
            <span v-else>-</span>
            <span v-if="current.task.git_branch" class="muted" style="margin-left:8px">branch: {{ current.task.git_branch }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="current.task.source_type === 'upload'" label="源码包" :span="2">
            <code style="font-size:12px">{{ current.task.source_path }}</code>
            <span v-if="current.task.source_size_mb" class="muted" style="margin-left:8px">({{ current.task.source_size_mb }} MB)</span>
          </el-descriptions-item>
        </el-descriptions>

        <!-- 2. 工单描述 -->
        <h4>📝 问题描述</h4>
        <div class="desc">{{ current.description || '（无描述）' }}</div>

        <div v-if="current.resolution" class="resolution-box">
          <strong>✅ 解决说明：</strong>{{ current.resolution }}
        </div>

        <!-- 3.（已移除）失败日志全景区·2026-10-27：工单域不再取全文——自动流转零消费者，
             人工深挖走任务页；工单日志=上报时自动附进 description 的末 50 行，自包含 -->

        <!-- 4. Agent 工作台（醒目卡片；pending 也显示占位，admin 一眼看到） -->
        <div class="agent-board">
          <h4 style="margin-top:0">🤖 Agent 工作台</h4>
          <el-alert
            v-if="!current.agent_active && current.status === 'pending'"
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom:10px"
          >
            <strong>未接单</strong> —— 接单后会在容器内启动 DeepAgent（建容器 + 拉源码 + 启动 agent），需 5-30 秒
            <div style="margin-top:6px">
              <el-button type="primary" size="small" @click="quickStatus(current, 'processing')">🚀 立即接单</el-button>
            </div>
          </el-alert>
          <el-alert
            v-else-if="!current.agent_active && current.status === 'processing' && !current.agent_error"
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom:10px"
          >
            <!-- T8-U 2026-10-22：显示后端 phase 细粒度当前阶段（git clone 大仓可能数分钟，
                 笼统"启动中"无法判断卡在哪）。currentPhaseLabel 由 phase 轮询实时更新；
                 轮询尚未出首个事件时回落通用文案 -->
            <strong v-if="current.agent_restoring">Agent 现场恢复中…</strong>
            <strong v-else-if="currentPhaseLabel">{{ currentPhaseLabel }}</strong>
            <strong v-else>Agent 启动中（正在创建容器…）</strong>
            <span style="margin-left:6px">已耗时 {{ agentStartElapsed }}s{{ currentPhaseLabel ? '' : '，大仓源码拉取可能需数分钟' }}</span>
          </el-alert>
          <el-alert
            v-else-if="!current.agent_active && current.status === 'processing' && current.agent_error"
            type="error"
            :closable="false"
            show-icon
            style="margin-bottom:10px"
          >
            <strong>Agent 启动失败</strong>
            <pre style="margin:8px 0;font-size:12px;white-space:pre-wrap;background:rgba(0,0,0,0.05);padding:8px;border-radius:4px">{{ current.agent_error }}</pre>
            <div style="margin-top:6px;display:flex;gap:8px">
              <el-button type="primary" size="small" @click="retryAgent()">🔄 重新启动</el-button>
              <span class="muted" style="font-size:12px;line-height:1.5">或点击下方"标记已解决"手动处理</span>
            </div>
          </el-alert>
          <el-alert
            v-else-if="current.agent_active"
            type="success"
            :closable="false"
            show-icon
            style="margin-bottom:10px"
          >
            <strong>容器 {{ current.container_id_short }} 活跃中</strong> —— Agent 真实在容器内跑命令/改源码
            <el-button size="small" type="danger" plain style="margin-left:8px" @click="cleanupContainer()">清理容器</el-button>
          </el-alert>

          <!-- T8: 双线分离——用户消息线(TicketMessage) / Agent 工作台线(B3 指令)。
               病根修复：此前抽屉唯一输入框发 /agent/message，管理员没有"回复用户"入口 -->
          <el-tabs v-model="chatTab" style="margin-bottom:6px">
            <el-tab-pane name="user">
              <template #label>用户消息</template>
              <div class="chat" style="max-height:340px;overflow:auto">
                <div v-if="!userMessages.length" class="chat-empty">（暂无用户消息）</div>
                <div v-for="m in userMessages" :key="'u' + m.id" :class="['msg', m.role]">
                  <div class="msg-who">
                    <el-tag :type="m.role === 'admin' ? 'success' : 'info'" size="small">{{ m.role === 'admin' ? '管理员（我）' : '用户' }}</el-tag>
                    · {{ m.username }} · {{ formatDate(m.created_at) }}
                  </div>
                  <div class="msg-content">{{ m.content }}</div>
                </div>
              </div>
              <div class="reply" v-if="current.status !== 'closed'">
                <el-input v-model="userReply" type="textarea" :rows="3"
                          :placeholder="current.status === 'pending' ? '回复用户（发送后工单自动进入处理中）（Ctrl+Enter）' : '回复用户（Ctrl+Enter 发送）'"
                          @keydown.ctrl.enter="sendUserReply" />
                <div style="margin-top:8px;display:flex;gap:8px;justify-content:flex-end">
                  <el-button type="primary" :loading="sendingReply" :disabled="!userReply.trim()" @click="sendUserReply">
                    回复用户
                  </el-button>
                </div>
              </div>
              <div v-else class="muted" style="font-size:13px;padding:8px 0">工单已关闭，双方禁言</div>
            </el-tab-pane>

            <el-tab-pane label="Agent 工作台" name="agent" :disabled="!current.agent_session_exists && !current.agent_active">
              <!-- 对话流（T9-U 回合分组：文本=章节头，工具=折叠明细） -->
              <div class="chat" ref="chatScrollRef">
                <div v-if="chatTurns.length === 0" class="chat-empty">
                  （暂无消息 —— 接单 + Agent 启动后与 agent 多轮对话解决）
                </div>
                <template v-for="(t, ti) in chatTurns" :key="'turn-' + ti">
                  <div v-for="m in t.lead" :key="m.id" :class="['msg', m.role]">
                    <div class="msg-who">
                      <el-tag :type="roleTag(m.role)" size="small">{{ roleLabel(m.role) }}</el-tag>
                      · {{ m.username || (m.role === 'agent' ? 'Agent' : m.role) }} · {{ formatDate(m.created_at) }}
                    </div>
                    <div class="msg-content">{{ m.content }}</div>
                  </div>
                  <div v-if="t.tools.length" class="turn-tools">
                    <div class="turn-tools-head" @click="toggleTurn(ti)">
                      {{ isOpenTurn(ti) ? '▾' : '▸' }} 🔧 {{ t.tools.length }} 个工具调用{{ isOpenTurn(ti) ? '（点击收起）' : '（已收起，点击展开）' }}
                    </div>
                    <template v-if="isOpenTurn(ti)">
                      <div v-for="m in t.tools" :key="m.id" :class="['msg', m.role]">
                        <div class="msg-who">
                          <el-tag :type="roleTag(m.role)" size="small">{{ roleLabel(m.role) }}</el-tag>
                          · {{ m.username || 'Agent' }} · {{ formatDate(m.created_at) }}
                        </div>
                        <div class="msg-content">{{ m.content }}</div>
                      </div>
                    </template>
                  </div>
                </template>
                <div v-if="agentThinking" class="msg agent pending">
                  <div class="msg-who">Agent 思考中…</div>
                  <div class="msg-content">{{ agentThinking }}</div>
                </div>
                <div v-if="agentPartial && !agentThinking && current.status === 'processing'" class="msg agent pending">
                  <div class="msg-who">⏳ 本回合仍在进行中</div>
                  <div class="msg-content">以下进度为快照（约 20 秒一存，非实时），跑完自动转为完整记录{{ agentPartial.admin_content ? '；本回合指令：「' + agentPartial.admin_content.slice(0, 60) + '」' : '' }}</div>
                </div>
              </div>

              <!-- 输入框（仅 processing+agent_active 时可输入） -->
              <div class="reply" v-if="current.status === 'processing' && current.agent_active">
                <el-input v-model="newMessage" type="textarea" :rows="3"
                          placeholder="告诉 agent 下一步该做什么（Ctrl+Enter 发送）"
                          @keydown.ctrl.enter="sendAgentMessage" />
                <div style="margin-top:8px;display:flex;gap:8px;justify-content:flex-end">
                  <el-button type="primary" :loading="sendingAgent" :disabled="!newMessage.trim()" @click="sendAgentMessage">
                    {{ stoppingAgent ? '停止中…' : '发送给 Agent' }}
                  </el-button>
                  <el-button v-if="sendingAgent" type="danger" plain :loading="stoppingAgent" @click="stopAgentRound">
                    ■ 停止
                  </el-button>
                </div>
              </div>
            </el-tab-pane>
          </el-tabs>
        </div>

        <!-- 5. 解决/生成 Skill 按钮组 -->
        <div class="actions">
          <el-button
            v-if="current.status === 'pending'"
            type="primary"
            @click="quickStatus(current, 'processing')"
          >接单（启动 Agent）</el-button>
          <el-button
            v-if="current.status === 'processing'"
            type="success"
            @click="openResolveDialog(current)"
          >标记已解决</el-button>
          <el-button
            v-if="current.status === 'resolved'"
            size="small"
            @click="openSkillPreview(current)"
          >📚 生成经验（预览后进审批）</el-button>
          <el-button @click="quickStatus(current, 'closed')">关闭工单</el-button>
          <el-button @click="closeDetail">← 返回工单列表</el-button>
        </div>
      </div>
    </el-drawer>

    <!-- 解决弹窗（必填 resolution + 可选自动生成 skill） -->
    <el-dialog v-model="resolveDialogVisible" title="标记已解决" width="500px">
      <el-form label-width="100px">
        <el-form-item label="解决说明" required>
          <el-input v-model="resolveForm.resolution" type="textarea" :rows="5"
                    placeholder="简述根因和修复方案（admin 必填）" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="resolveForm.autoSkill">
            同时生成经验草稿（提交后进「审批」页，预览确认再入库）
          </el-checkbox>
        </el-form-item>
        <el-form-item>
          <div class="muted" style="font-size:12px;line-height:1.5">
            💡 勾选后：自动从工单+解决说明拼 SKILL.md 草稿并提审，与用户提交经验同一条审批流
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resolveDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="resolving" @click="confirmResolve">确认解决</el-button>
      </template>
    </el-dialog>

    <!-- 经验草稿预览弹窗（编辑 + 确认后提交审批） -->
    <el-dialog v-model="skillPreviewVisible" title="📚 经验草稿预览（确认前可编辑）" width="760px" :close-on-click-modal="false" @close="stopDraftPolling()">
      <div v-if="skillDraft">
        <div style="margin-bottom:8px">
          <el-tag v-if="skillDraft.generated_by === 'llm'" type="success" size="small" effect="plain">🤖 AI 总结（同用户经验提示词）</el-tag>
          <el-tag v-else type="warning" size="small" effect="plain">📋 模板拼装（AI 不可用：{{ skillDraft.llm_error || '未知原因' }}）</el-tag>
        </div>
        <el-form label-width="80px" style="margin-bottom:8px">
          <el-form-item label="Skill 名称">
            <el-input v-model="skillDraft.skill_name" />
          </el-form-item>
        </el-form>
        <el-form-item label-width="0">
          <el-input v-model="skillDraft.skill_content" type="textarea" :rows="18" class="draft-editor" />
        </el-form-item>
        <div class="muted" style="font-size:12px;margin-top:6px">
          提交后进「审批」列表（与用户经验同流程）：通过=写 Skill 表+落盘 SKILL.md 生效；驳回=留痕不入库
        </div>
      </div>
      <div v-else class="chat-wait" style="text-align:center;padding:60px 0">
        <div style="font-size:28px" class="spin">🛠️</div>
        <div style="margin-top:12px;font-weight:600">AI 正在生成攻略草稿…</div>
        <div class="muted" style="margin-top:6px">已等待 {{ draftElapsed }} 秒（思考模型生成约需 1-3 分钟，可关窗稍后再点）</div>
      </div>
      <template #footer>
        <el-button @click="skillPreviewVisible = false">取消</el-button>
        <el-button type="primary" :loading="submittingDraft" :disabled="!skillDraft" @click="submitSkillDraft">提交审批</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/utils/request'
import { fmtDateTime } from '@/utils/datetime'

const tickets = ref<any[]>([])
const filterStatus = ref<string>('')
const searchText = ref('')
const loading = ref(false)
const detailVisible = ref(false)
const current = ref<any>(null)
const newMessage = ref('')
// T8: 双线 tab 状态
const chatTab = ref<'user' | 'agent'>('user')
const userReply = ref('')
const agentPartial = ref<any>(null)  // T9-U 进行中回合快照标记
const sendingReply = ref(false)
// tab1 数据源：对话线消息（排除工作台指令 kind=agent_cmd）；历史旧行 kind=chat 自然全含
const userMessages = computed(() => (current.value?.messages || []).filter((m: any) => (m.kind || 'chat') !== 'agent_cmd'))
// T8: tab2 兜底视图（B3 历史拉不到时）同样只留工作台线，防 chat/用户行再串入
const agentFallbackMessages = computed(() => (current.value?.messages || []).filter((m: any) => m.kind === 'agent_cmd'))
async function sendUserReply() {
  if (!userReply.value.trim() || !current.value) return
  const content = userReply.value.trim()
  sendingReply.value = true
  try {
    await api.post(`/admin/tickets/${current.value.ticket_id}/messages`, null, { params: { content } })
    current.value.messages.push({ id: Date.now(), role: 'admin', content, username: '我', kind: 'chat', created_at: new Date().toISOString() })
    userReply.value = ''
    ElMessage.success('已回复用户')
    // pending 首回自动转 processing 由后端做，这里同步状态显示+列表计数
    if (current.value.status === 'pending') current.value.status = 'processing'
    loadCounts()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '回复失败')
  } finally {
    sendingReply.value = false
  }
}
const agentThinking = ref('')
const sendingAgent = ref(false)
// T9-U 停止当前轮：abort 本地 SSE 读流 + "停止中…"态（收敛前禁误发新指令）
const agentAbort: { ref: AbortController | null } = { ref: null }
const stoppingAgent = ref(false)
const counts = reactive({ all: 0, pending: 0, processing: 0, resolved: 0, closed: 0 })

// 解决弹窗
const resolveDialogVisible = ref(false)
const resolveTarget = ref<any>(null)
const resolving = ref(false)
const resolveForm = reactive({ resolution: '', autoSkill: true })

function statusType(status: string) { return { pending: 'warning', processing: 'primary', resolved: 'success', closed: 'info' }[status] || 'info' }
function statusText(status: string) { return { pending: '待处理', processing: '处理中', resolved: '已解决', closed: '已关闭' }[status] || status }
function roleTag(role: string) { return { user: '', admin: 'success', agent: 'warning', tool: 'info', system: '' }[role] || '' }
function roleLabel(role: string) { return { user: '用户', admin: '管理员', agent: 'Agent', tool: '工具', system: '系统' }[role] || role }
function formatDate(s: string) { return fmtDateTime(s) }  // 10-23: naive UTC 补 Z 解析（时区三踩收口）

// 2026-10-19：关闭工单详情抽屉（不删任何数据，只隐藏）
// 重要：保留 localChat + _phaseLastSeq（Map 持久化跨 stop/start）
// 再进入时（同一 ticket）：
//   - openDetail 会从 DB 拉 B3 history + 重置 localChat
//   - startPhasePolling 用 Map[ticketId] 续上 since_seq，不重头显示已完成阶段
// agent/容器/in-memory session 完全不动（后端无感知）
function closeDetail() {
  detailVisible.value = false
  // localChat.value = []   // 不清空（保留显示，避免重入闪烁；openDetail 会覆盖）
  // _phaseLastSeqMap 不动  // 保留，startPhasePolling 用 Map[ticketId] 续
}

// 2026-10-19：聊天区滚到底
const chatScrollRef = ref<HTMLElement | null>(null)
function scrollChatToBottom() {
  // 等 DOM 更新完再滚
  setTimeout(() => {
    const el = chatScrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  }, 0)
}

async function loadCounts() {
  const r: any = await api.get('/admin/tickets')
  const all = r.data || []
  counts.all = all.length
  for (const s of ['pending', 'processing', 'resolved', 'closed']) {
    ;(counts as Record<string, number>)[s] = all.filter((t: any) => t.status === s).length
  }
}

async function loadTickets() {
  loading.value = true
  try {
    const params: any = {}
    if (filterStatus.value) params.status = filterStatus.value
    if (searchText.value) params.search = searchText.value
    const r: any = await api.get('/admin/tickets', { params })
    tickets.value = r.data || []
  } finally {
    loading.value = false
  }
  loadCounts()
}

async function openDetail(row: any) {
  const r: any = await api.get(`/admin/tickets/${row.ticket_id}`)
  current.value = r.data
  // A 方案：进入 processing 工单即对账——内存 session 丢了（后端重启过）后端会自动恢复，
  // 这里轮询到 ready 为止，用户不再需要手点"重新启动"
  if (r.data?.status === 'processing') {
    try {
      const st: any = await api.get(`/admin/tickets/${row.ticket_id}/agent/status`)
      const p = st.data?.phase
      if (p === 'restoring' || p === 'starting') {
        current.value.agent_restoring = true
        pollAgentStart(row.ticket_id)
      }
    } catch { /* status 探测失败不阻塞打开 */ }
  }
  newMessage.value = ''
  userReply.value = ''
  // T9-U 10-23 用户拍板：工单抽屉一律默认落 Agent 工作台（tab1 用户消息一键可达）
  chatTab.value = 'agent'
  agentThinking.value = ''
  // B3 2026-10-19：admin 抽屉打开时拉 agent 历史 + 拼到 localChat
  // （解决"退出再进 agent 消息丢"问题）
  // T8 线分离（2026-10-22 泄漏修复：tab2 混进了 tab1 的"回复用户"行）——
  // admin 行只取工作台指令线 kind=agent_cmd；chat 回复线只在 tab1 显示
  const adminHistory = (r.data?.messages || []).filter((m: any) => m.role === 'admin' && m.kind === 'agent_cmd')
  const agentHistoryResp: any = await api.get(`/admin/tickets/${row.ticket_id}/agent/messages-history`).catch(() => ({ data: { messages: [] } }))
  const agentMsgs = (agentHistoryResp.data?.messages || [])
  // T9-U 轮中快照：partial 存在=这轮还在跑（DB 里是 ≥20s 节流的进度快照，非最终账）
  agentPartial.value = agentHistoryResp.data?.partial || null
  // T9-U 2026-10-23 交错时间线：B3 数组本身就是 langgraph state 的自然时序
  // （human/ai/tool 交错），此前按"来源分块堆"（admin 全顶+agent 全底）失真。
  // 现在顺序遍历 B3：human→管理员气泡（与 TicketMessage 按内容配对拿权威 created_at），
  // ai→Agent 气泡，tool_calls→工具气泡（输出按 tcId 合并回调用气泡），ts 缺的旧行继承不上也不乱序。
  const combined: any[] = []
  const b3HumanKeys = new Set(agentMsgs.filter((m: any) => m.type === 'human').map((m: any) => (m.content || '').slice(0, 80)))
  for (const m of agentMsgs) {
    if (m.type === 'human') {
      const adm = adminHistory.find((a: any) => (a.content || '').slice(0, 80) === (m.content || '').slice(0, 80))
      combined.push({
        id: `hist-admin-${adm?.id ?? combined.length}`,
        role: 'admin',
        content: m.content,
        username: adm?.username || '我',
        created_at: adm?.created_at || m.ts || null,  // TicketMessage 时间优先，退回 B3 ts
      })
    } else if (m.type === 'ai') {
      if (m.content) {
        combined.push({ id: `hist-agent-${m.tool_call_id || combined.length}`, role: 'agent', content: m.content, username: 'Agent', created_at: m.ts || null })
      }
      if (m.tool_calls) {
        for (const tc of m.tool_calls) {
          const _tc_content = tc.content || `[${tc.name || '?'}] ${tc.args && Object.keys(tc.args).length ? JSON.stringify(tc.args) : '(无参数)'}`
          combined.push({ id: `hist-tool-${m.tool_call_id || combined.length}-${tc.name}`, tcId: tc.id, role: 'tool', content: _tc_content, username: 'Agent', created_at: m.ts || null })
        }
      }
    } else if (m.type === 'tool') {
      const hit = m.tool_call_id ? combined.find(x => x.tcId === m.tool_call_id) : null
      if (hit) {
        hit.content = `${hit.content}\n↳ ${(m.content || '').slice(0, 1200)}`
      } else {
        combined.push({ id: `hist-toolresult-${m.tool_call_id || combined.length}`, role: 'tool', content: `↳ [${m.name || 'result'}] ${(m.content || '').slice(0, 1200)}`, username: 'Agent', created_at: m.ts || null })
      }
    }
  }
  // B3 里没有的指令行（历史裁剪/旧 session）按 created_at 插入正确位置，不整块沉底
  const strays = adminHistory.filter((a: any) => !b3HumanKeys.has((a.content || '').slice(0, 80)))
  if (strays.length) {
    for (const m of strays) {
      const idx = combined.findIndex(x => x.created_at && new Date(x.created_at) > new Date(m.created_at))
      const item = { id: `hist-admin-${m.id}`, role: 'admin', content: m.content, username: m.username || '我', created_at: m.created_at }
      idx === -1 ? combined.push(item) : combined.splice(idx, 0, item)
    }
  }
  localChat.value = combined
  collapsedTurns.value = new Set()  // 重开抽屉恢复全展开
  console.log('[B3] openDetail loaded history: admin=', adminHistory.length, 'agent/tool msgs=', agentMsgs.length, 'combined=', combined.length)
  detailVisible.value = true
  // 2026-10-19：滚到聊天最底
  scrollChatToBottom()
  // 2026-10-19：重置 phase-pill（每次进入时只显示当前最新阶段，不重头）
  currentPhaseLabel.value = ''
  currentPhaseClass.value = 'phase-progress'
  // 2026-10-19 方案 2：起 phase 轮询（拉工单调试实时阶段提示）
  // 2026-10-20 修复：仅 processing 工单轮询——pending/resolved/closed 无启动过程，
  // 后端 done=False 永等导致 1.5s/次无限刷屏
  if (r.data.status === 'processing' && agentPartial.value) {
    startPartialPolling(row.ticket_id)  // T9-U：这轮在跑，定期重拉 B3 让进度可见
  }
  if (r.data.status === 'processing') {
    startPhasePolling(row.ticket_id)
  }
  // 如果已接单但 agent 未启动，轮询等它起来
  if (r.data.status === 'processing' && !r.data.agent_active) {
    pollAgentStart(row.ticket_id)
  }
}

// 2026-10-19 方案 2：phase 轮询 + 推 system 消息
// 2026-10-19 进一步：按 ticketId 分组持久化 _phaseLastSeq（重入不重头）
let _phasePollTimer: any = null
let _phaseLastSeqMap: Map<number, number> = new Map()  // ticketId → 已读最大 seq
// 2026-10-19：当前阶段（顶部状态条，**不**进 localChat 历史气泡）
const currentPhaseLabel = ref<string>('')  // 空 = 不显示
// T8-U: 启动/恢复已耗时（秒），进入 processing-未active 轮询时重置
const agentStartElapsed = ref(0)
let _startTimer: ReturnType<typeof setInterval> | null = null
function startElapsedTimer() {
  stopElapsedTimer()
  agentStartElapsed.value = 0
  _startTimer = setInterval(() => { agentStartElapsed.value += 1 }, 1000)
}
function stopElapsedTimer() { if (_startTimer) { clearInterval(_startTimer); _startTimer = null } }

const currentPhaseClass = ref<string>('phase-progress')  // phase-progress/running/done/failed
const PHASE_LABEL: Record<string, { emoji: string; label: string }> = {
  reuse_container: { emoji: '♻️', label: '复用容器' },
  create_container: { emoji: '🐳', label: '启动容器' },
  container_ready: { emoji: '✅', label: '容器就绪' },
  reuse_source: { emoji: '♻️', label: '复用源码' },
  fetch_source: { emoji: '📥', label: '拉取源码' },
  source_ready: { emoji: '✅', label: '源码就绪' },
  init_backend: { emoji: '🔧', label: '初始化后端' },
  init_llm: { emoji: '🤖', label: '初始化 LLM' },
  build_prompt: { emoji: '📝', label: '构造 system_prompt' },
  create_deep_agent: { emoji: '🧠', label: '启动 DeepAgent' },
  deep_agent_ready: { emoji: '🎉', label: 'Agent 就绪' },
}
function startPhasePolling(ticketId: number) {
  stopPhasePolling()
  // 2026-10-19：按 ticketId 分组持久化 _phaseLastSeq（避免重入时重复显示已完成阶段）
  // 跨 stop/start 保留进度，跨 ticket 隔离
  const seenSeq = _phaseLastSeqMap.get(ticketId) || 0
  _phasePollTimer = setInterval(async () => {
    try {
      const r: any = await api.get(`/admin/tickets/${ticketId}/agent/phase`, { params: { since_seq: seenSeq } })
      const data = r.data || {}
      // 2026-10-19：phase 状态**只更新顶部 phase-pill**（不进 localChat 历史气泡）
      // 用户每次进入时只看到当前最新阶段（避免 11 条系统消息刷屏）
      for (const ev of (data.events || [])) {
        // 按 seq 持久化（按 ticketId 分组）
        const cur = _phaseLastSeqMap.get(ticketId) || 0
        _phaseLastSeqMap.set(ticketId, Math.max(cur, ev.seq || 0))
        // 只更新 currentPhaseLabel（顶部状态条）
        const info = PHASE_LABEL[ev.phase] || { emoji: '⚙️', label: ev.phase }
        currentPhaseLabel.value = `${info.emoji} ${info.label}${ev.detail ? '：' + ev.detail : ''}`
        // 阶段类型（用于 CSS 颜色）
        currentPhaseClass.value = ev.phase === 'deep_agent_ready' ? 'phase-done'
          : ev.phase === 'create_deep_agent' || ev.phase === 'fetch_source' ? 'phase-running'
          : 'phase-progress'
      }
      if (data.done) {
        console.log('[phase] done, stop polling')
        // 2026-10-19：done 时 phase-pill 变绿"已就绪"
        currentPhaseClass.value = 'phase-done'
        stopPhasePolling()
      }
      if (data.error) {
        // 2026-10-19：error 也只更新顶部 phase-pill（红底）
        currentPhaseLabel.value = `❌ 启动失败: ${data.error}`
        currentPhaseClass.value = 'phase-failed'
        stopPhasePolling()
      }
    } catch (e) {
      console.error('[phase] poll err', e)
    }
  }, 1500)  // 1.5s 一次（避免太频繁）
}
function stopPhasePolling() {
  if (_phasePollTimer) {
    clearInterval(_phasePollTimer)
    _phasePollTimer = null
  }
  stopPartialPolling()  // T9-U: partial 刷新随抽屉生命周期
  stopElapsedTimer()  // T8-U: 关抽屉/切工单时启动计时一并停（pollAgentStart 后台循环自会随三出口收敛）
}

async function pollAgentStart(ticketId: number) {
  startElapsedTimer()  // T8-U: 启动/恢复计时（alert 里显示已耗时）
  // restoring（重启后自动恢复现场：docker start + agent 重建）可能要 30-90s，窗口放宽
  // 2026-10-22：窗口 45×2s=90s → 300×2s=10min。教训：pytorch 级大仓 clone 数分钟，
  // 旧窗口内 clone 还在正常推进就被判"未启动请接单"（误诊）；就绪/失败出口仍即时跳出
  for (let i = 0; i < 300; i++) {
    await new Promise(r => setTimeout(r, 2000))
    const r: any = await api.get(`/admin/tickets/${ticketId}/agent/status`)
    const d = r.data || {}
    if (d.active) {
      const det: any = await api.get(`/admin/tickets/${ticketId}`)
      current.value = det.data
      current.value.agent_error = null
      current.value.agent_restoring = false
      ElMessage.success('Agent session 已就绪')
      stopElapsedTimer()
      return
    }
    if (d.phase === 'failed' && d.error) {
      // 启动失败 — 显示错误 + 停止轮询
      current.value.agent_error = d.error
      ElMessage.error(`Agent 启动失败：${d.error}`)
      stopElapsedTimer()
      return
    }
    if (d.phase === 'session_lost') {
      // 兼容老后端：仍提示手动重新启动
      current.value.agent_error = d.error
      ElMessage.warning(`Session 已丢失，请点「重新启动」按钮`)
      stopElapsedTimer()
      return
    }
    // starting / restoring → 顶部给个状态提示继续等
    if (d.phase === 'restoring' && i === 0) {
      current.value = current.value || {}
      current.value.agent_restoring = true
    }
  }
  // 超时但没明确错误 — 读 /agent/phase 定位卡在哪个阶段（中文），按阶段给可判断的原因
  current.value.agent_restoring = false
  try {
    const r2: any = await api.get(`/admin/tickets/${ticketId}/agent/phase`, { params: { since_seq: 0 } })
    const data = r2.data || {}
    const events = data.events || []
    if (events.length === 0) {
      // 完全没事件 — 后端确实没开始建 session（没点到接单/启动请求丢了）
      current.value.agent_error = '后端未收到启动请求（没有任何阶段记录）。请点「接单」；若已接单请点「重新启动」'
      ElMessage.warning('Agent 未启动：请接单 / 重新启动')
    } else {
      const lastEv = events[events.length - 1]
      const cn = (PHASE_LABEL[lastEv.phase] || { label: lastEv.phase }).label
      const hint: Record<string, string> = {
        create_container: 'docker 创建容器异常慢——可能镜像首次拉取或 docker daemon 卡住',
        fetch_source: 'git clone 大仓进行中——pytorch 这类仓库需数分钟，稍后重开抽屉即可看到进展',
        create_deep_agent: 'DeepAgent 初始化慢——LLM 连接/技能加载阶段，可稍等或重新启动',
      }
      current.value.agent_error = `已进行到「${cn}」阶段，等待 ${agentStartElapsed.value}s 未就绪。`
        + (data.error ? `后端报错：${data.error}` : (hint[lastEv.phase] || '该阶段耗时异常，可点「重新启动」'))
        + '（阶段未回退=大概率仍在推进，不一定是失败）'
      ElMessage.error(`Agent 启动卡在「${cn}」阶段`)
    }
  } catch (e) {
    current.value.agent_error = `启动等待 ${agentStartElapsed.value}s 超时，且阶段记录读取失败——检查后端日志`
  }
}

async function retryAgent() {
  if (!current.value) return
  current.value.agent_error = null
  try {
    const r: any = await api.post(`/admin/tickets/${current.value.ticket_id}/agent/retry`)
    ElMessage.success(r?.message || '重新启动已触发')
    // 重新轮询
    pollAgentStart(current.value.ticket_id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重新启动失败')
  }
}

async function quickStatus(row: any, newStatus: string) {
  const label = { processing: '接单', resolved: '标记已解决', closed: '关闭' }[newStatus]
  // 关工单时需要二次确认（其他操作直接走）
  if (newStatus === 'closed') {
    try {
      await ElMessageBox.confirm(`确认关闭工单 #${row.ticket_id}？关闭后不可恢复`, '提示', { type: 'warning', confirmButtonText: '关闭', cancelButtonText: '取消' })
    } catch { return }
  } else {
    try {
      await ElMessageBox.confirm(`确认${label}工单 #${row.ticket_id}？`, '提示', { type: 'info', confirmButtonText: '确认', cancelButtonText: '取消' })
    } catch { return }
  }
  try {
    const r: any = await api.post(`/admin/tickets/${row.ticket_id}/status`, null, { params: { new_status: newStatus } })
    ElMessage.success(r?.message || '操作成功')
    // 接单 / 进入 processing：保持抽屉打开，刷新 current 数据 + 启动 agent 轮询
    if (newStatus === 'processing' && detailVisible.value) {
      const det: any = await api.get(`/admin/tickets/${row.ticket_id}`)
      current.value = det.data
      pollAgentStart(row.ticket_id)
    } else if (newStatus === 'closed') {
      // 关闭：关抽屉
      detailVisible.value = false
    } else {
      // 其他：刷新 current 后保持抽屉打开
      const det: any = await api.get(`/admin/tickets/${row.ticket_id}`)
      current.value = det.data
    }
    loadTickets()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

function openResolveDialog(row: any) {
  resolveTarget.value = row
  resolveForm.resolution = ''
  resolveForm.autoSkill = true
  resolveDialogVisible.value = true
}

async function confirmResolve() {
  if (!resolveForm.resolution.trim()) {
    ElMessage.warning('请填写解决说明')
    return
  }
  resolving.value = true
  try {
    const r: any = await api.post(
      `/admin/tickets/${resolveTarget.value.ticket_id}/resolve`,
      null,
      // 勾选自动生成经验时后端同步调 LLM（60s+），放宽超时
      { params: { resolution: resolveForm.resolution.trim(), auto_generate_skill: resolveForm.autoSkill }, timeout: 240000 }
    )
    ElMessage.success(r?.message || '已解决')
    resolveDialogVisible.value = false
    const d = r.data || {}
    if (d.new_task_id) ElMessage.info(`新 task: ${d.new_task_id}（admin 可下载产物）`)
    if (d.draft_job_started) {
      // 后台已在生成草稿 → 直接开预览弹窗轮询（不卡 HTTP）
      openSkillPreview(resolveTarget.value)
    }
    detailVisible.value = false
    loadTickets()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '解决失败')
  } finally {
    resolving.value = false
  }
}

// 前端本地 chat 数组（agent 对话不持久化，admin 在这个工单会话期间保留）
const localChat = ref<any[]>([])

// 2026-10-19：监听 localChat 长度变化：消息增加自动滚到底
// 必须在 localChat 声明之后注册 watch（TDZ 错误：Cannot access before initialization）
function _stopSafetyReset() {
  // 停止信号是异步收敛（长命令击杀秒级；纯 LLM 调用等本次返回）。
  // 90s 未等到 stopped/done 事件（如旧后端/事件丢失）→ 复位按钮态，输入框解禁，
  // 此时若真在跑，后端 409 防御仍在，误发会被拒——安全。
  window.setTimeout(() => {
    if (stoppingAgent.value) {
      stoppingAgent.value = false
      ElMessage.warning('停止信号已发出，仍在等待本轮收敛（可在时间线观察是否已停）')
    }
  }, 90000)
}

async function stopAgentRound() {
  // T9-U 停止当前轮：后端置旗标+杀活跃 exec（进程树）→ 轮在下一 chunk/命令返回边界停
  if (!current.value || stoppingAgent.value) return
  stoppingAgent.value = true
  try {
    await api.post(`/admin/tickets/${current.value.ticket_id}/agent/stop`)
    _stopSafetyReset()
  } catch (e: any) {
    stoppingAgent.value = false
    ElMessage.error('停止请求失败: ' + (e?.message || ''))
    return
  }
  // "停止中…"保持到 worker 在命令/chunk 边界收敛（SSE 会收到 stopped；长命令击杀后
  // 秒级返回；纯 LLM 调用中则等本次返回——UI 明示异步语义，不假装瞬时）
}

watch(() => localChat.value.length, () => {
  scrollChatToBottom()
})

let _partialTimer: number | null = null
function startPartialPolling(ticketId: number) {
  stopPartialPolling()
  _partialTimer = window.setInterval(async () => {
    try {
      const r: any = await api.get(`/admin/tickets/${ticketId}/agent/messages-history`)
      const msgs = r.data?.messages || []
      const part = r.data?.partial || null
      // 轮询期间用户可能在手动编辑 localChat？——只在内容有增量时整体替换
      const cur = localChat.value
      if (msgs.length && (part || cur.length === 0)) {
        localChat.value = rebuildFromB3(msgs, part)
      }
      agentPartial.value = part
      if (!part) stopPartialPolling()  // 轮末转正，停止刷新
    } catch { /* 网络抖动下轮再来 */ }
  }, 15000)
}
function stopPartialPolling() {
  if (_partialTimer) { clearInterval(_partialTimer); _partialTimer = null }
}
// 从 B3 消息重建 localChat（与 openDetail 拼装同规则的精简版，供轮询刷新用）
function rebuildFromB3(agentMsgs: any[], _part: any): any[] {
  const combined: any[] = []
  for (const m of agentMsgs) {
    if (m.type === 'human') {
      combined.push({ id: `p-admin-${combined.length}`, role: 'admin', content: m.content, username: '我', created_at: m.ts || null })
    } else if (m.type === 'ai') {
      if (m.content) combined.push({ id: `p-agent-${combined.length}`, role: 'agent', content: m.content, username: 'Agent', created_at: m.ts || null })
      for (const tc of (m.tool_calls || [])) {
        combined.push({ id: `p-tool-${combined.length}-${tc.name}`, tcId: tc.id, role: 'tool', content: `[${tc.name || '?'}]`, username: 'Agent', created_at: m.ts || null })
      }
    } else if (m.type === 'tool') {
      const hit = m.tool_call_id ? combined.find(x => x.tcId === m.tool_call_id) : null
      if (hit) hit.content = `${hit.content}\n↳ ${(m.content || '').slice(0, 1200)}`
      else combined.push({ id: `p-tout-${combined.length}`, role: 'tool', content: `↳ [${m.name || 'result'}] ${(m.content || '').slice(0, 1200)}`, username: 'Agent', created_at: m.ts || null })
    }
  }
  return combined
}

// T9-U 2026-10-23 回合分组渲染：admin/agent 文本 = 章节头（lead），其间的 tool 调用收进
// 可折叠明细——治"计划长文孤零零压在一堆工具上面"的观感（时序本身是对的，ReAct 就是
// agent 只在回合首尾说话；缺的是把"这段工具属于哪句话"表达出来）
const chatTurns = computed(() => {
  const list = localChat.value.length ? localChat.value : (agentFallbackMessages.value || [])
  const turns: { lead: any[]; tools: any[] }[] = []
  let cur: { lead: any[]; tools: any[] } | null = null
  for (const m of list) {
    if (m.role === 'admin' || m.role === 'agent') {
      cur = { lead: [m], tools: [] }
      turns.push(cur)
    } else {
      if (!cur) { cur = { lead: [], tools: [] }; turns.push(cur) }
      cur.tools.push(m)
    }
  }
  return turns
})
// T9-U: 默认全展开（读流水不需要逐组点击）；collapsedTurns 记"手动收起"的组，点击切换
const collapsedTurns = ref<Set<number>>(new Set())
function isOpenTurn(i: number) { return !collapsedTurns.value.has(i) }
function toggleTurn(i: number) {
  const st = new Set(collapsedTurns.value)
  st.has(i) ? st.delete(i) : st.add(i)
  collapsedTurns.value = st
}

async function sendAgentMessage() {
  if (!newMessage.value.trim() || !current.value) return
  const content = newMessage.value.trim()
  const ticketId = current.value.ticket_id
  newMessage.value = ''
  console.log('[FE→BE] sendAgentMessage START', { ticket_id: ticketId, content, localChat_len_before: localChat.value.length })
  // 1) 立即显示 admin 消息
  const adminMsg = {
    id: Date.now(),
    role: 'admin',
    content,
    username: '我',
    created_at: new Date().toISOString(),
    kind: 'agent_cmd',  // T8: 线别标记，不串进 tab1（11 月 vue-tsc2：kind 随字面量声明，不再事后挂属性）
  }
  localChat.value.push(adminMsg)
  console.log('[FE] pushed adminMsg', { id: adminMsg.id, localChat_len: localChat.value.length })
  agentThinking.value = '（Agent 思考中…）'
  sendingAgent.value = true
  const controller = new AbortController()
  agentAbort.ref = controller

  // 2) 2026-10-19：改用 SSE 流式（POST /agent/stream）—— 边想边打字
  // 参考 stage5 编译任务的实时进度推送（§46 + §30 BROADCASTER），但工单走 SSE 不走 WS
  // 每次 SSE event 立即 push localChat + 滚到底
  const baseURL = (api.defaults?.baseURL || '').replace(/\/$/, '') || window.location.origin
  const streamURL = `${baseURL}/admin/tickets/${ticketId}/agent/stream?content=${encodeURIComponent(content)}`

  // 标记 agent 消息气泡（流式累积，最后 done 时更新）
  let agentMsgId: number | null = null
  const toolMsgIds: number[] = []  // 已 push 的 tool 气泡 id
  let agentContent = ''

  try {
    // SSE 端点需要 auth，axios 默认带 header，但 fetch 不带 — 从 localStorage 取 token
    const token = localStorage.getItem('token') || ''
    const resp = await fetch(streamURL, {
      method: 'POST',
      signal: controller.signal,  // T9-U 停止：点"停止"后掐本地读流
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      // body 用 JSON 形式（FastAPI params 也支持 query，这里用 query 更简单）
    })
    if (resp.status === 409) {
      // T9-U 缺口 B 防御命中：上一轮停止未收敛
      const detail = await resp.text().catch(() => '')
      ElMessage.warning(detail.includes('上一轮') ? '上一轮还在停止收敛中，请稍候再发' : '上一轮仍在进行，请先停止或等待')
      return
    }
    if (!resp.ok || !resp.body) {
      throw new Error(`stream HTTP ${resp.status}: ${await resp.text().catch(() => '')}`)
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    agentThinking.value = '（Agent 收到，开始流式…）'
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // SSE event 由 \n\n 分隔
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const event = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        // 解析 "data: {...}" 行
        const dataLine = event.split('\n').find(l => l.startsWith('data:'))
        if (!dataLine) continue
        const json = dataLine.slice(5).trim()
        if (!json) continue
        let payload: any
        try { payload = JSON.parse(json) } catch { continue }
        console.log('[SSE]', payload)
        if (payload.type === 'admin_msg_id') {
          // admin 消息已存库（不影响前端 localChat 推）
        } else if (payload.type === 'delta') {
          // AI 输出 token 增量
          agentContent += payload.content
          if (agentMsgId === null) {
            // 第一次 delta：push 新气泡
            agentMsgId = Date.now() + Math.random()
            localChat.value.push({
              id: agentMsgId,
              role: 'agent',
              content: agentContent,
              username: 'Agent',
              created_at: new Date().toISOString(),
            })
          } else {
            // 后续 delta：update 已存在气泡的 content
            const msg = localChat.value.find(m => m.id === agentMsgId)
            if (msg) msg.content = agentContent
          }
        } else if (payload.type === 'tool') {
          // 工具调用 — 2026-10-19：后端已按 tool_call id 聚合成完整 1 次调用（含友好 content）
          // 之前前端"重复合并"hack 删除：后端修好后不再有碎片，hack 里的 break 还会误杀读循环
          const _friendly = payload.content || `[${payload.tool || '?'}] ${payload.input && Object.keys(payload.input).length ? JSON.stringify(payload.input) : '(无参数)'}`
          const toolId = Date.now() + Math.random()
          toolMsgIds.push(toolId)
          localChat.value.push({
            id: toolId,
            tcId: payload.id,  // 2026-10-19：langchain tool_call id，供 tool_result 关联穿插
            role: 'tool',
            content: _friendly,
            username: 'Agent',
            created_at: new Date().toISOString(),
          })
        } else if (payload.type === 'tool_result') {
          // 2026-10-19：工具输出——能按 tcId 关联到调用气泡的就"调用+输出"合一条（穿插不堆底部）；
          // 关联不上就独立 result 气泡
          const hit = payload.tool_call_id ? localChat.value.find(m => m.tcId === payload.tool_call_id) : null
          if (hit) {
            hit.content = `${hit.content}\n↳ ${payload.content}`
          } else {
            localChat.value.push({
              id: Date.now() + Math.random(),
              role: 'tool',
              content: `↳ [${payload.name || 'result'}] ${payload.content}`,
              username: 'Agent',
              created_at: new Date().toISOString(),
            })
          }
        } else if (payload.type === 'stopped') {
          // T9-U 后端停止收尾事件（转正已由其 finally 完成）——"停止中"至此收敛
          stoppingAgent.value = false
          controller.abort()          // 流尾还有 done 事件，本地无需再读
          sendingAgent.value = false
          agentThinking.value = ''
          localChat.value.push({
            id: Date.now() + Math.random(),
            role: 'system',
            content: `⏹ ${payload.message}`,
            username: 'System',
            created_at: new Date().toISOString(),
          })
        } else if (payload.type === 'error') {
          // 错
          localChat.value.push({
            id: Date.now() + Math.random(),
            role: 'system',
            content: `❌ ${payload.message}`,
            username: 'System',
            created_at: new Date().toISOString(),
          })
        } else if (payload.type === 'done') {
          // 收尾 — 跟同步路径兼容（agent_unavailable 等）
          if (payload.response && agentMsgId === null) {
            // 没收到过 delta 事件（罕见）：整段 push
            agentMsgId = Date.now() + Math.random()
            localChat.value.push({
              id: agentMsgId,
              role: 'agent',
              content: payload.response,
              username: 'Agent',
              created_at: new Date().toISOString(),
            })
          }
          if (payload.agent_unavailable) {
            ElMessage.warning('agent 工具调用暂时不可用')
          }
          stoppingAgent.value = false  // 任何 done 均代表轮已终止
        }
        // 滚到底
        scrollChatToBottom()
      }
    }
    agentThinking.value = ''
    console.log('[SSE done] agentContent len=', agentContent.length, 'toolMsgs=', toolMsgIds.length)
  } catch (e: any) {
    console.error('[FE] sendAgentMessage SSE CATCH', e?.message)
    agentThinking.value = ''
    if (e?.name !== 'AbortError') {  // T9-U：主动停止掐流不当报错
      ElMessage.error(e?.message || '发送失败（流式连接错误）')
    }
  } finally {
    sendingAgent.value = false
    stoppingAgent.value = false
    agentAbort.ref = null
    // 刷新工单元信息
    try {
      const det: any = await api.get(`/admin/tickets/${ticketId}`)
      current.value = det.data
    } catch {}
  }
}

async function cleanupContainer() {
  try {
    await ElMessageBox.confirm('确认清理容器？磁盘产物保留', '提示', { type: 'warning' })
  } catch { return }
  try {
    const r: any = await api.post(`/admin/tickets/${current.value.ticket_id}/cleanup-container`)
    ElMessage.success(r?.message || '已清理')
    current.value.agent_active = false
    current.value.container_id_short = null
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '清理失败')
  }
}

// 经验草稿预览 → 编辑 → 提交审批（走 experience 流程）
const skillPreviewVisible = ref(false)
const skillDraft = ref<any>(null)
const submittingDraft = ref(false)
const draftElapsed = ref(0)  // 等待秒数（异步生成进度感）

async function openSkillPreview(row: any) {
  skillDraft.value = null
  skillPreviewVisible.value = true
  draftElapsed.value = 0
  try {
    // 异步任务：秒回，后台 LLM 生成（60-240s），轮询状态——避免 HTTP 超时
    await api.post(`/admin/tickets/${row.ticket_id}/generate-skill/preview-job`)
    pollDraftJob(row.ticket_id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '草稿生成启动失败')
    skillPreviewVisible.value = false
  }
}

let _draftPollTimer: any = null
function stopDraftPolling() {
  if (_draftPollTimer) { clearInterval(_draftPollTimer); _draftPollTimer = null }
}
async function pollDraftJob(ticketId: number) {
  if (_draftPollTimer) clearInterval(_draftPollTimer)
  _draftPollTimer = setInterval(async () => {
    try {
      const r: any = await api.get(`/admin/tickets/${ticketId}/generate-skill/preview-job/status`)
      const d = r.data || {}
      draftElapsed.value = Math.round(d.elapsed || 0)
      if (d.status === 'done' && d.result) {
        clearInterval(_draftPollTimer); _draftPollTimer = null
        skillDraft.value = d.result
      } else if (d.status === 'error') {
        clearInterval(_draftPollTimer); _draftPollTimer = null
        ElMessage.error('草稿生成失败：' + (d.error || '未知错误'))
        skillPreviewVisible.value = false
      }
    } catch {
      clearInterval(_draftPollTimer); _draftPollTimer = null
      ElMessage.error('草稿状态查询失败')
    }
  }, 3000)
}

async function submitSkillDraft() {
  if (!skillDraft.value || !current.value) return
  submittingDraft.value = true
  try {
    const r: any = await api.post(`/admin/tickets/${current.value.ticket_id}/generate-skill`, {
      skill_content: skillDraft.value.skill_content,
      skill_name: skillDraft.value.skill_name,
    })
    ElMessage.success(r?.message || '已提交审批')
    skillPreviewVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '提交失败')
  } finally {
    submittingDraft.value = false
  }
}

onMounted(loadTickets)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }
.resolution-cell { color: var(--el-color-success, #67c23a); font-size: 13px }
.spin { display: inline-block; animation: spin 1.6s linear infinite }
@keyframes spin { to { transform: rotate(360deg) } }
.toolbar { display: flex; gap: 12px; margin-bottom: 14px; align-items: center; flex-wrap: wrap }
.detail h4 { margin: 18px 0 8px; font-size: 14px; font-weight: 600 }
/* 2026-10-19：返回工单列表 bar（明显放顶部左侧） */
.back-bar { padding: 12px 16px; background: #f5f7fa; border-bottom: 1px solid #ebeef5; margin-bottom: 8px; display: flex; align-items: center; gap: 12px }
.back-bar .el-button { font-size: 14px; font-weight: 500 }
/* 2026-10-19：放大原 #header 的返回按钮（被 el-drawer 折叠太隐蔽） */
.el-drawer__header .el-button--small { font-size: 13px }
.topbar { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; margin-bottom: 4px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px }
/* 2026-10-19：当前阶段状态条（顶部右侧，动态变色）*/
.phase-pill { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; margin-left: 12px; font-weight: 500; transition: all 0.3s }
.phase-progress { background: #f4f4f5; color: #606266; border: 1px solid #dcdfe6 }
.phase-running { background: #fff7e6; color: #e6a23c; border: 1px solid #faecd8; animation: phase-pulse 1.5s ease-in-out infinite }
.phase-done { background: #f0f9eb; color: #67c23a; border: 1px solid #e1f3d8 }
.phase-failed { background: #fef0f0; color: #f56c6c; border: 1px solid #fde2e2 }
@keyframes phase-pulse { 0%, 100% { opacity: 1 } 50% { opacity: 0.6 } }
.agent-board { background: linear-gradient(135deg, #fff 0%, #f8faff 100%); border: 2px solid #409eff; border-radius: 10px; padding: 16px; margin-top: 16px; box-shadow: 0 2px 12px rgba(64,158,255,0.08) }
.agent-board h4 { color: #409eff }
.chat-empty { padding: 20px; text-align: center; color: var(--text3); font-size: 12px; font-style: italic }
.desc { background: var(--bg); padding: 12px; border-radius: 6px; white-space: pre-wrap; line-height: 1.6; max-height: 200px; overflow: auto; font-size: 13px }
.resolution-box { background: #f0f9eb; border-left: 3px solid #67c23a; padding: 10px 12px; margin-top: 10px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; border-radius: 4px }
.log { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 6px; max-height: 280px; overflow: auto; font-size: 12px; font-family: monospace; white-space: pre-wrap }
.chat { display: flex; flex-direction: column; gap: 10px; max-height: 60vh; overflow: auto; padding: 8px; background: var(--bg); border-radius: 6px }
.msg { padding: 8px 12px; border-radius: 6px; max-width: 90% }
.msg.user { background: #e6f0ff; align-self: flex-start }
.msg.admin { background: #f0f9eb; align-self: flex-end }
.msg.agent { background: #fdf6ec; align-self: flex-start; border-left: 3px solid #e6a23c }
.msg.tool { background: #f4f4f5; align-self: stretch; max-width: 100%; font-size: 12px; color: var(--text3) }
.msg.system { background: #ecf5ff; align-self: center; max-width: 100%; font-size: 12px; color: var(--primary, #409eff); text-align: center; font-style: italic }
.msg.pending { opacity: 0.7; font-style: italic }
.turn-tools-head {
  font-size: 12px; color: #8a8f98; cursor: pointer; user-select: none;
  padding: 3px 8px; margin: 4px 0; border-radius: 4px;
  background: rgba(128, 135, 148, 0.10);
}
.turn-tools-head:hover { background: rgba(128, 135, 148, 0.20); }
.msg-who { font-size: 11px; color: var(--text3); margin-bottom: 4px; display: flex; gap: 6px; align-items: center }
.msg-content { white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.6 }
.reply { margin-top: 10px }
.actions { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); display: flex; gap: 10px; justify-content: flex-end }
</style>
