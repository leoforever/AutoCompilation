<template>
  <div class="page">
    <div class="card">
      <h3>💬 问答台</h3>
      <p class="hint">带引用回答的排查助手：结论必须回指平台资料（Skill/任务日志/编译纪要），无出处的内容自动降级到"通用知识"分面。不执行任何操作——要实机处理请转工单。</p>

      <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px">
        <el-select v-model="taskDbId" clearable filterable remote :remote-method="searchTasks"
                   :loading="taskLoading" placeholder="挂载任务上下文（可选；可输入任务名/任务号搜索）" style="width:320px">
          <el-option v-for="t in taskOptions" :key="t.id" :label="`${t.software_name}（${t.task_id}，${statusLabel(t.status)}）`" :value="t.id" />
        </el-select>
        <el-button @click="clearChat">清空重问</el-button>
      </div>

      <el-input v-model="pasteText" type="textarea" :rows="4"
                placeholder="（可选）粘贴报错片段/日志节选——作为证据资料注入，回答会引用它并同样脱敏；上限 2 万字符，取尾部 8k"
                style="margin-bottom:8px" />
      <el-input v-model="question" type="textarea" :rows="3" maxlength="4000" show-word-limit
                placeholder="例如：龙芯编 python 包总报 ffi 链接错误怎么办？（Ctrl+Enter 发送）"
                @keydown.ctrl.enter="ask" />
      <div style="margin-top:8px; display:flex; gap:8px; align-items:center">
        <el-button type="primary" :loading="busy" @click="ask">提问</el-button>
        <span class="hint">今日剩余 {{ quotaLeft }} 次（每日限额，含失败重试）</span>
      </div>
    </div>

    <div class="card" v-for="(m, idx) in messages" :key="idx">
      <h4>问：{{ m.q }}</h4>
      <template v-if="m.pending"><el-icon class="is-loading"><Loading /></el-icon> 检索资料并生成中（约 10~60 秒）…</template>
      <template v-else-if="m.error"><el-text type="danger">{{ m.error }}</el-text></template>
      <template v-else>
        <div class="answer">{{ m.answer }}</div>
        <div v-if="m.citations?.length" class="cite-box">
          <b>【平台依据】</b>
          <div v-for="c in m.citations" :key="c.ref + c.quote" class="cite">
            <span class="cite-head" @click="c._open = !c._open">▸ {{ c.ref }} ⟵ “{{ c.quote.slice(0, 120) }}{{ c.quote.length > 120 ? '…' : '' }}”</span>
            <pre v-if="c._open && c.ctx" class="cite-ctx">{{ c.ctx }}</pre>
          </div>
        </div>
        <div v-if="m.invalid_citations" class="hint">（模型给出的引用有 {{ m.invalid_citations }} 条回原文比对失败，已剔除）</div>
        <div v-if="m.general_notes" class="gen-box">
          <b>【通用知识，无平台出处】</b>
          <div class="gen">{{ m.general_notes }}</div>
        </div>
        <div style="margin-top:10px; display:flex; gap:8px">
          <el-button size="small" @click="openDraft(m)" :disabled="!m.citations?.length">存为经验草稿</el-button>
          <el-button size="small" @click="toTicket(m)">转工单</el-button>
          <span class="hint" v-if="m.sources_meta">上下文任务：{{ m.sources_meta.task_id }}</span>
        </div>
      </template>
    </div>

    <el-dialog v-model="draftVisible" title="存为经验草稿（进入审批链）" width="640px">
      <el-form label-width="110px">
        <el-form-item label="经验名"><el-input v-model="draft.skill_name" maxlength="100" /></el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="draft.skill_type">
            <el-radio value="generic">通用</el-radio><el-radio value="specific">专用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="目标系统">
          <el-select v-model="draft.system_id" style="width:220px">
            <el-option v-for="sy in systems" :key="sy.system_id" :label="sy.name" :value="sy.system_id" />
          </el-select>
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="draft.content" type="textarea" :rows="10" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="draftVisible=false">取消</el-button>
        <el-button type="primary" :loading="draftBusy" @click="saveDraft">提交审批</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import api from '@/utils/request'

const router = useRouter()
interface QaMsg { q: string; pending: boolean; answer: string; citations: any[];
  general_notes: string; error: string; invalid_citations?: number; canary_leak?: boolean;
  sources_meta?: { task_id: string; id: number; system_id: number } }
const question = ref('')
const pasteText = ref('')
const taskDbId = ref(null)
const taskOptions = ref<any[]>([])
const taskLoading = ref(false)
const systems = ref<any[]>([])
const messages = ref<QaMsg[]>([])
const busy = ref(false)
const quotaLeft = ref('-')
const draftVisible = ref(false)
const draftBusy = ref(false)
const draft = reactive({ skill_name: '', skill_type: 'specific', system_id: 1, content: '', task_db_id: 0 })
let pollTimer: any = null

let _searchTimer: any = null
async function fetchTasks(kw: string) {
  const params: any = kw ? { search: kw, page_size: 8 } : { page_size: 12 }
  const t: any = await api.get('/compile/tasks', { params })
  const list = (t?.tasks || []).map((x: any) => ({ id: x.id, task_id: x.task_id, software_name: (x.software_name || '').trim(), status: x.status }))
  // 远程搜索换列表时当前选中项必须保留，否则 el-select 显示蒸发
  const cur = taskOptions.value.find((x: any) => x.id === taskDbId.value)
  if (cur && !list.some((x: any) => x.id === cur.id)) list.unshift(cur)
  taskOptions.value = list
}
function searchTasks(kw: string) {           // el-select remote-method：防抖 300ms
  clearTimeout(_searchTimer)
  _searchTimer = setTimeout(async () => {
    taskLoading.value = true
    try { await fetchTasks(kw.trim()) } finally { taskLoading.value = false }
  }, 300)
}

async function init() {
  await fetchTasks('')
  const s: any = await api.get('/compile/options/systems')
  systems.value = (s?.data && (s.data.data || s.data)) || s?.systems || []
  if (taskOptions.value.length) taskDbId.value = taskOptions.value[0].id
  const st: any = await api.get('/admin/qa/status')
  if (st?.data?.status === 'running') {            // 刷新前在途 job：接管继续等
    const m = reactive<QaMsg>({ q: '（刷新前的问题）', pending: true, answer: '', citations: [], general_notes: '', error: '' })
    messages.value.push(m); busy.value = true; waitResult(m)
  }
  quotaLoad()
}
async function quotaLoad() {
  // 配额=后端账本口径（当日 qa_ask 审计行数）——取 status 端点顺带返的 quota 字段最诚实，
  // 后端没返则显示 '-' 不装懂（审计列表前端过滤是重复造轮子且受分页影响，弃）
  try {
    const st: any = await api.get('/admin/qa/quota')
    quotaLeft.value = st?.data?.left ?? '-'
  } catch { quotaLeft.value = '-' }
}

function finish(m: QaMsg, snap: any) {
  m.pending = false
  if (snap.type === 'done' && snap.result) {
    Object.assign(m, snap.result, { answer: snap.result.answer })
  } else if (snap.type === 'error') {
    m.error = snap.message || '生成失败'
  } else {
    m.error = '生成超时或无结果，请稍后重试'
  }
  busy.value = false
}

async function waitResult(m: QaMsg) {
  // SSE 主腿 + 轮询兜底（draft stream 同款）
  try {
    const token = localStorage.getItem('token') || ''
    const resp = await fetch('/api/admin/qa/stream', { headers: { Authorization: `Bearer ${token}` } })
    const reader = (resp.body as ReadableStream).getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const evt = buf.match(/data: (\{[\s\S]*?\})\n\n/)
      if (evt) { finish(m, JSON.parse(evt[1])); return }
    }
  } catch { /* 断流走轮询 */ }
  poll(m)
}
function poll(m: QaMsg) {
  clearInterval(pollTimer)
  pollTimer = setInterval(async () => {
    const st: any = await api.get('/admin/qa/status')
    const d = st?.data
    if (d?.status === 'done') { finish(m, { type: 'done', result: d.result }); clearInterval(pollTimer) }
    else if (d?.status === 'error') { finish(m, { type: 'error', message: d.error }); clearInterval(pollTimer) }
  }, 3000)
}

async function ask() {
  const q = question.value.trim()
  if (!q || busy.value) return
  // P0 串答案修复：先探后端——上一问在途则明确告知，不起新气泡不发 ask
  const st: any = await api.get('/admin/qa/status')
  if (st?.data?.status === 'running') {
    ElMessage.warning('上一问仍在生成（约需 1 分钟），完成后可再问')
    return
  }
  busy.value = true
  const m = reactive<QaMsg>({ q, pending: true, answer: '', citations: [], general_notes: '', error: '' })
  messages.value.push(m)
  question.value = ''
  try {
    const r: any = await api.post('/admin/qa/ask', { question: q, task_db_id: taskDbId.value, paste_text: pasteText.value })
    void r
    waitResult(m)
  } catch (e: any) {
    m.pending = false
    m.error = e?.response?.data?.detail || '提问失败'
    busy.value = false
  }
}

const STATUS_LABEL: Record<string,string> = { success:'成功', failed:'失败', compiling:'编译中', submitted:'排队', cancelled:'已取消' }
function statusLabel(s: string) { return STATUS_LABEL[s] || s }
function clearChat() {
  messages.value = []
  api.post('/admin/qa/reset', {}).catch(() => {})
}

function openDraft(m: QaMsg) {
  const linked = m.sources_meta || taskOptions.value.find((x) => x.id === taskDbId.value)
  if (!linked) { ElMessage.warning('存草稿需挂载任务（经验库血缘要求）'); return }
  draft.skill_name = ''
  draft.content = `# ${m.citations?.[0]?.ref || 'QA 结论'}\n\n${m.answer}\n\n## 依据\n${(m.citations || []).map((c) => `- ${c.ref}: ${c.quote}`).join('\n')}`
  draft.task_db_id = linked.id ?? taskDbId.value
  draft.system_id = (linked as any).system_id ?? systems.value[0]?.system_id ?? 1
  draftVisible.value = true
}
async function saveDraft() {
  if (!draft.skill_name || draft.content.length < 20) { ElMessage.warning('经验名与内容必填（内容≥20字）'); return }
  draftBusy.value = true
  try {
    await api.post('/admin/qa/save-draft', draft)
    ElMessage.success('已提交审批队列（经验审核页可见）')
    draftVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '提交失败')
  } finally { draftBusy.value = false }
}
function toTicket(m: QaMsg) {
  // 不承诺"预填"（Workbench 不消费该参数=跳了不定位的半截功能）：复制问题+跳上报页，
  // 用户粘贴即可——兑现得了的交互 > 名义顺滑
  try { navigator.clipboard?.writeText(m.q) } catch { /* 非安全上下文降级：手动复制 */ }
  ElMessage.success('问题已复制，粘贴到工单描述即可（转工单=实机处理出口）')
  router.push('/workbench')
}
onMounted(init)
onBeforeUnmount(() => clearInterval(pollTimer))
</script>

<style scoped>
.page { display: block; }
.card { background: #fff; border-radius: 8px; padding: 16px 20px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.hint { color: #909399; font-size: 12px; }
.answer { white-space: pre-wrap; font-size: 14px; line-height: 1.7; }
.cite-box { margin-top: 10px; padding: 8px 12px; background: #f0f9eb; border-radius: 6px; font-size: 13px; }
.cite { margin-top: 4px; color: #529b2e; word-break: break-all; }
.cite-head { cursor: pointer; }
.cite-ctx { margin: 4px 0 0 14px; padding: 6px 8px; background: #fff; border-left: 3px solid #67c23a; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow: auto; }
.gen-box { margin-top: 10px; padding: 8px 12px; background: #fdf6ec; border-radius: 6px; font-size: 13px; }
.gen { margin-top: 4px; white-space: pre-wrap; color: #b88230; }
h4 { margin: 0 0 8px; }
</style>
