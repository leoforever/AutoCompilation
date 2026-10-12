<template>
  <div class="page">
    <div class="page-title">在线编译工作台</div>
    <div class="page-sub">云端公共编译模式 —— 选择系统、提供源码，AI 自动匹配 Skill 并编排执行</div>

    <div class="grid g2">
      <!-- 编译参数区 -->
      <div class="card">
        <h3>⚙️ 编译参数</h3>

        <div class="field">
          <label>目标系统</label>
          <el-select v-model="form.systemId" placeholder="请选择目标系统" style="width:100%">
            <el-option
              v-for="sys in systems"
              :key="sys.system_id"
              :label="sys.name"
              :value="sys.system_id"
              :disabled="!sys.is_active"
            />
          </el-select>
        </div>

        <div class="field">
          <label>软件名称 / 版本</label>
          <el-input v-model="form.softwareName" placeholder="如：ollama v0.9.7" />
        </div>

        <div class="tabs">
          <div :class="['tab', { active: form.sourceType === 'upload' }]" @click="form.sourceType = 'upload'">上传源码包</div>
          <div :class="['tab', { active: form.sourceType === 'git' }]" @click="form.sourceType = 'git'">Git 地址</div>
        </div>

        <!-- 上传源码 -->
        <div v-if="form.sourceType === 'upload'" class="field">
          <div class="upload-area" @click="triggerUpload">
            <input ref="fileInput" type="file" accept=".tar.gz,.zip" style="display:none" @change="handleFileChange" />
            <div v-if="uploadedFile" class="notice ok">
              📦 已选择 <b>{{ uploadedFile.name }}</b>（{{ (uploadedFile.size / 1024 / 1024).toFixed(1) }} MB）
              <span class="muted">（upload 模式暂未开放，请使用 Git 地址）</span>
            </div>
            <div v-else class="upload-placeholder">
              点击或拖拽上传源码包（.tar.gz / .zip）<br>
              <span class="muted">支持最大 2 GB · 当前建议用 Git 地址</span>
            </div>
          </div>
        </div>

        <!-- Git 地址 -->
        <div v-else class="field">
          <label>Git 仓库地址</label>
          <el-input v-model="form.gitUrl" placeholder="https://github.com/xxx/xxx.git" />
          <label style="margin-top:10px">分支 / Tag</label>
          <el-input v-model="form.gitBranch" placeholder="main 或 v1.0.0" />
        </div>

        <div class="field">
          <label>编译模式</label>
          <el-radio-group v-model="form.compileMode">
            <el-radio value="container">🐳 容器编译（云端隔离）</el-radio>
            <el-radio value="local">💻 本地编译（需客户端）</el-radio>
          </el-radio-group>
        </div>

        <div style="display:flex;gap:10px">
          <el-button type="primary" @click="submitCompile" :loading="compiling">
            🚀 发起云端编译
          </el-button>
          <el-button @click="$router.push('/tasks')">查看历史任务</el-button>
        </div>
      </div>

      <!-- AI 技能匹配面板 -->
      <div class="card">
        <h3>
          🤖 AI 技能匹配
          <el-tag :type="matchStatus.type" size="small">{{ matchStatus.text }}</el-tag>
        </h3>

        <div v-if="!matchedSkills.length && !compiling" class="empty">
          发起编译后，大模型将自动匹配通用 + 专用 OpenClaw Skill
        </div>

        <div v-else-if="matchedSkills.length">
          <div class="field">
            <label>命中 Skill（{{ genericSkills.length }} 通用 · {{ specificSkills.length }} 专用）</label>
            <div class="skill-chips">
              <el-tag
                v-for="skill in matchedSkills"
                :key="skill.name"
                :type="skill.type === 'generic' ? 'primary' : 'warning'"
                style="margin: 3px 4px 3px 0"
              >
                {{ skill.name }}
                <el-tag v-if="skill.type === 'specific'" size="small" type="info" class="tag-specific">专用</el-tag>
              </el-tag>
            </div>
          </div>
        </div>

        <div v-else-if="compiling" class="empty">
          <el-icon class="is-loading"><Loading /></el-icon>
          AI 正在匹配 Skill…
        </div>
      </div>
    </div>

    <!-- 编译执行区 -->
    <div v-if="compiling || compileResult" class="card">
      <h3>
        📟 编译执行
        <el-tag :type="compileResult?.status === 'success' ? 'success' : compileResult?.status === 'failed' ? 'danger' : 'warning'" size="small">
          {{ compileResult?.status === 'success' ? '已完成' : compileResult?.status === 'failed' ? '失败' : '编译中' }}
        </el-tag>
        <span v-if="compileResult?.task_id" class="muted" style="margin-left:8px">task_id: {{ compileResult.task_id }}</span>
      </h3>

      <!-- 日志终端 -->
      <div class="term" ref="logTerminal">
        <div v-if="!logLines.length && compiling" class="empty">
          <el-icon class="is-loading"><Loading /></el-icon> 等待 worker 抢到任务…
        </div>
        <div v-for="(line, idx) in logLines" :key="idx" :class="['log-line', line.type]">
          {{ line.text }}
        </div>
      </div>

      <!-- 结果操作 -->
      <div v-if="compileResult?.status === 'success'" class="result-actions">
        <el-button type="primary" @click="submitExperience">
          💾 沉淀经验
        </el-button>
        <el-button @click="downloadArtifact">⬇️ 下载产物</el-button>
      </div>

      <div v-if="compileResult?.status === 'failed'" class="result-actions">
        <el-button type="danger" @click="reportTicket">
          🐛 一键上报工单
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
import api from '@/utils/request'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'

// ============== 表单状态 ==============
const form = reactive({
  systemId: null as number | null,
  softwareName: '',
  sourceType: 'git' as 'upload' | 'git',  // 默认 git，更符合当前可用路径
  gitUrl: '',
  gitBranch: 'main',
  compileMode: 'container' as 'container' | 'local',
})

const uploadedFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement>()

// ============== 系统列表 ==============
const systems = ref<any[]>([])
async function loadSystems() {
  try {
    const res = await api.get('/admin/systems', { params: { is_active: true } })
    const data = (res.data && (res.data.data || res.data)) || []
    systems.value = data
    if (data.length && !form.systemId) {
      form.systemId = data[0].system_id
    }
  } catch (e) {
    console.error('加载系统列表失败', e)
  }
}

// ============== 编译状态 ==============
const compiling = ref(false)
const logLines = ref<{ text: string; type: string }[]>([])
const compileResult = ref<{ status: 'success' | 'failed'; task_id?: string; artifact?: string } | null>(null)
const matchedSkills = ref<Array<{ name: string; type: 'generic' | 'specific' }>>([])
let ws: WebSocket | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null

// 任务 ID 持久化 key（切走再切回恢复用）
const TASK_STORAGE_KEY = 'workbench:task_id'

// ============== 页面挂载：恢复上次任务 ==============
// 抽函数：sessionStorage 恢复路径 + onMounted 主动接管 都用
async function restoreFromTaskId(taskId: string): Promise<boolean> {
  // 返回 true = 接管成功（继续显示该任务）
  // 返回 false = 跳过（任务不存在/已终态/权限不足/拉日志失败）
  try {
    const r: any = await api.get(`/compile/tasks/${taskId}`)
    const t = r?.data
    if (!t) return false
    // 拉完整日志文件（artifacts/{id}/build.log）
    let logText = ''
    const token = localStorage.getItem('token') || ''
    try {
      const resp = await fetch(`/api/compile/tasks/${taskId}/log/download`, {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {},
      })
      if (resp.ok) {
        const ct = resp.headers.get('content-type') || ''
        if (ct.includes('text/plain')) {
          logText = await resp.text()
        } else {
          const j = await resp.json()
          logText = j?.data?.log || ''
        }
      }
    } catch (e) {
      // fetch 失败 → 静默回退到 DB
    }
    if (!logText) logText = t.log_output || ''
    // 解析成 logLines
    if (logText) {
      logLines.value = logText.split('\n')
        .filter((line: string) => line.trim())
        .map((line: string) => {
          const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s+\[(\w+)\]\s+(.*)$/)
          if (m) return { text: `${m[1]} ${m[3]}`, type: mapLevel(m[2]) }
          return { text: line, type: 'info' }
        })
    }
    // 状态
    compileResult.value = { status: t.status, task_id: taskId }
    if (t.status === 'success' || t.status === 'failed' || t.status === 'cancelled') {
      // 已终态：清 sessionStorage + 不显示（Workbench = 进行中工作区）
      sessionStorage.removeItem(TASK_STORAGE_KEY)
      return false
    }
    // 未终态：判断是否 stale
    const updated = t.updated_at ? new Date(t.updated_at).getTime() : 0
    const ageMs = Date.now() - updated
    const STALE_MS = 10 * 60 * 1000
    if (ageMs > STALE_MS) {
      // 任务已超时：接管但提示用户
      compiling.value = false
      matchStatus.text = `任务已超时（${Math.round(ageMs / 60000)} 分钟无更新），worker 可能已崩溃`
      matchStatus.type = 'warning'
      return true  // 显示任务信息给用户看（不是接管 WS）
    }
    // 接管 + 订阅 WS
    sessionStorage.setItem(TASK_STORAGE_KEY, taskId)
    compiling.value = true
    matchStatus.text = '已恢复，继续接收日志'
    matchStatus.type = 'warning'
    subscribeWS(taskId)
    return true
  } catch (e: any) {
    // 403 / 404 / 其它 API 错：清 sessionStorage 避免下次又试
    if (e?.response?.status === 403 || e?.response?.status === 404) {
      sessionStorage.removeItem(TASK_STORAGE_KEY)
    }
    return false
  }
}

onMounted(async () => {
  loadSystems()
  // 1) 优先 sessionStorage（用户切走再切回）
  const savedTid = sessionStorage.getItem(TASK_STORAGE_KEY)
  if (savedTid) {
    const ok = await restoreFromTaskId(savedTid)
    if (ok) return
    // 失败 → 清掉 + 继续走第 2 步
  }
  // 2) 查"我名下"是否有 running 任务（覆盖：A 浏览器提交 → 关 A → 开 B → 进 Workbench 接管）
  try {
    const r: any = await api.get('/compile/tasks?status=compiling&page_size=5')
    // /compile/tasks 直接返 {total, tasks}，不走 ApiResponse
    const running = r?.tasks || []
    if (running.length) {
      const ok = await restoreFromTaskId(running[0].task_id)
      if (ok) return
    }
    // submitted 也接管（刚提交没 worker 抢）
    const sub: any = await api.get('/compile/tasks?status=submitted&page_size=5')
    const subs = sub?.tasks || []
    if (subs.length) {
      const ok = await restoreFromTaskId(subs[0].task_id)
      if (ok) return
    }
  } catch (e) {
    // 后端不可用 / 鉴权失败 → 静默
  }
  // 3) 都没有 → 空白进入
})

// ============== 匹配结果 ==============
const genericSkills = computed(() => matchedSkills.value.filter(s => s.type === 'generic'))
const specificSkills = computed(() => matchedSkills.value.filter(s => s.type === 'specific'))
const matchStatus = reactive({ text: '待匹配', type: 'info' as '' | 'success' | 'warning' | 'danger' | 'info' })

// ============== 文件上传（暂未开放，仅记录） ==============
function triggerUpload() {
  // spec 9.1 云端模式只走 git；upload 模式未实现，这里点击提示
  ElMessage.warning('当前为源码上传模式（未开放）；请切到「Git 地址」')
}

function handleFileChange(_e: Event) {
  // 占位保留
}

// ============== 提交编译（真实链路） ==============
async function submitCompile() {
  if (!form.systemId) { ElMessage.warning('请选择目标系统'); return }
  if (!form.softwareName) { ElMessage.warning('请填写软件名称'); return }
  if (form.sourceType !== 'git') {
    ElMessage.warning('当前仅支持 Git 地址模式（upload 暂未开放）'); return
  }
  if (!form.gitUrl) { ElMessage.warning('请填写 Git 仓库地址'); return }

  // 如果当前有正在跑的任务（可能是 onMounted 恢复的旧任务），确认是否覆盖
  // 旧任务在后端会继续跑到自然结束（除非用户主动 cancel），不影响新任务
  if (compiling.value) {
    try {
      await ElMessageBox.confirm(
        '当前有一个任务还在编译中（可能是切走时遗留）。开始新任务？\n旧任务会在后端继续跑到结束。',
        '开始新任务',
        { confirmButtonText: '开始新任务', cancelButtonText: '取消', type: 'warning' }
      )
    } catch {
      return  // 用户取消
    }
  }

  // 重置前先关旧 WS（避免和旧任务的 WS 混线）
  if (ws) { try { ws.close() } catch {} ws = null }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  sessionStorage.removeItem(TASK_STORAGE_KEY)

  compiling.value = true
  logLines.value = []
  compileResult.value = null
  matchedSkills.value = []
  matchStatus.text = '匹配中…'
  matchStatus.type = 'warning'

  // 1) POST /api/compile/submit
  let task_id = ''
  try {
    // request.ts 拦截器已 unwrap 了，返回值是后端的 {code, message, data}
    const r: any = await api.post('/compile/submit', {
      system_id: form.systemId,
      software_name: form.softwareName,
      source_type: form.sourceType,
      git_url: form.gitUrl,
      git_branch: form.gitBranch || 'main',
      compile_mode: form.compileMode,
    })
    task_id = r?.data?.task_id || ''
    if (!task_id) throw new Error(`响应无 task_id: ${JSON.stringify(r).slice(0, 200)}`)
    sessionStorage.setItem(TASK_STORAGE_KEY, task_id)  // 切走再切回恢复用
    appendLog('info', `[SUBMIT] task_id=${task_id}，等待 worker 抢到…`)
  } catch (e: any) {
    compiling.value = false
    appendLog('err', `[ERROR] 提交失败: ${e?.message || e}`)
    compileResult.value = { status: 'failed' }
    matchStatus.text = '失败'
    matchStatus.type = 'danger'
    return
  }

  // 2) 打开 WebSocket 接收实时日志
  subscribeWS(task_id)
}

// ============== WS 订阅（submitCompile + 恢复都用）==============
function subscribeWS(task_id: string) {
  // 后端 WS 路径：spec 已有 /ws/compile/log/{task_id}
  // 注意：request 的 baseURL 是 /api，WS 不能重复前缀
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsBase = location.host
  // 重新订阅前先关掉旧的（避免一个任务多个 WS）
  if (ws) { try { ws.close() } catch {} ws = null }
  // WS 不走 axios 拦截器：手动带 token（后端 ws.py 校验所有权）
  const wsToken = localStorage.getItem('token') || ''
  const wsUrl = wsToken
    ? `${protocol}//${wsBase}/ws/compile/log/${task_id}?token=${encodeURIComponent(wsToken)}`
    : `${protocol}//${wsBase}/ws/compile/log/${task_id}`
  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    appendLog('info', `[WS] 已连接到 worker.BROADCASTER`)
  }

  ws.onmessage = (ev) => {
    // 后端推的是 JSON：{ type: "log"|"status"|"connected", level, msg, ts, status }
    let payload: any = ev.data
    try { payload = JSON.parse(ev.data) } catch { /* 兼容纯字符串 */ }

    if (typeof payload === 'string') {
      appendLog('info', payload)
      return
    }

    if (payload.type === 'connected') {
      appendLog('info', `[WS] ${payload.task_id} 建立`)
      return
    }

    if (payload.type === 'log') {
      const level = payload.level || 'info'
      appendLog(mapLevel(level), payload.msg)
      // 从 ollama / go-toolchain 等 SKILL.md 命中提示里解析 skill 名
      extractSkillsFromLine(payload.msg)
      return
    }

    if (payload.type === 'status') {
      const s = payload.status as string
      if (s === 'submitted' || s === 'compiling') {
        matchStatus.text = '已匹配'
        matchStatus.type = 'success'
      }
      if (s === 'success' || s === 'failed') {
        compileResult.value = { status: s, task_id: payload.task_id }
        matchStatus.text = s === 'success' ? '已完成' : '失败'
        matchStatus.type = s === 'success' ? 'success' : 'danger'
        appendLog(s === 'success' ? 'ok' : 'err',
                  `[DONE] 状态=${s}  task_id=${payload.task_id}`)
        // 终态到达：关按钮 loading + 关 WS（不依赖后端主动 close）
        compiling.value = false
        // 终态已落库，切走再切回不必再恢复（避免 onMounted 时 fetch 一次）
        // 保留 sessionStorage 让用户主动刷新/重进时仍能看到结果
        if (ws) { try { ws.close() } catch {} ws = null }
      }
      return
    }
  }

  ws.onerror = (e) => {
    appendLog('err', `[WS] 错误：${e}`)
  }

  ws.onclose = () => {
    appendLog('dim', `[WS] 连接关闭`)
    cleanup()
  }
}

// 把后端 level 映射到 CSS class
function mapLevel(level: string): string {
  switch (level) {
    case 'error': return 'err'
    case 'warn':
    case 'warning': return 'warn'
    case 'success': return 'ok'
    case 'info':
    default: return 'info'
  }
}

// 从日志文本里提取已命中的 skill 名（按 SKILL.md 名字列表匹配）
function extractSkillsFromLine(text: string) {
  if (!text) return
  const known = [
    // 通用 Skill（实际 SKILL.md name）
    { name: 'go-toolchain', type: 'generic' },
    { name: 'nodejs-toolchain', type: 'generic' },
    { name: 'python-environment', type: 'generic' },
    // 专用 Skill
    { name: 'ollama-openeuler-2403', type: 'specific' },
    { name: 'llama-cpp-openeuler-2403', type: 'specific' },
  ]
  for (const s of known) {
    if (text.includes(s.name) && !matchedSkills.value.some(m => m.name === s.name)) {
      matchedSkills.value.push(s)
    }
  }
}

function appendLog(type: string, text: string) {
  logLines.value.push({ text, type })
  nextTick(() => {
    const el = document.querySelector('.term')
    if (el) el.scrollTop = el.scrollHeight
  })
}

function cleanup() {
  compiling.value = false
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function submitExperience() {
  if (!compileResult.value?.task_id) { ElMessage.warning('无 task_id'); return }
  api.post('/experience/submit', {
    task_id: compileResult.value.task_id,
    skill_name: 'auto-' + Date.now(),
    skill_content: '(从 workbench 一键沉淀)',
    skill_type: 'specific',
    system_id: form.systemId,
  }).then(() => ElMessage.success('已提交经验沉淀请求，等待管理员审核'))
    .catch(err => ElMessage.error('提交失败：' + (err?.message || err)))
}

function reportTicket() {
  ElMessage.info('已生成工单跳转到工单中心')
}

function downloadArtifact() {
  ElMessage.info('产物归档路径在后端 ARTIFACTS_DIR 下')
}

onUnmounted(() => {
  if (ws) try { ws.close() } catch {}
  cleanup()
})
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 8px }
.grid { display: grid; gap: 16px }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g2 { grid-template-columns: 1fr } }
.field { margin-bottom: 14px }
.field label { display: block; font-size: 12.5px; color: var(--text2); margin-bottom: 6px; font-weight: 500 }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 16px }
.tab { padding: 9px 16px; cursor: pointer; color: var(--text2); border-bottom: 2px solid transparent; font-size: 13.5px }
.tab.active { color: var(--primary); border-bottom-color: var(--primary); font-weight: 600 }
.upload-area { border: 1.5px dashed #b6c6e8; border-radius: 8px; padding: 16px; text-align: center; color: var(--text3); cursor: pointer; background: #f8faff }
.upload-placeholder { line-height: 1.8 }
.muted { color: var(--text3); font-size: 12px }
.skill-chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px }
.tag-specific { margin-left: 4px }
.steps { display: flex; gap: 0; margin: 14px 0 }
.step { flex: 1; text-align: center; position: relative; padding-top: 24px }
.step::before { content: ''; position: absolute; top: 9px; left: 50%; width: 100%; height: 2px; background: var(--border); z-index: 0 }
.step:first-child::before { display: none }
.step .bub { position: relative; z-index: 1; width: 18px; height: 18px; border-radius: 50%; background: #fff; border: 2px solid var(--border); margin: 0 auto 6px; display: flex; align-items: center; justify-content: center; font-size: 10px }
.step.done .bub { background: var(--success); border-color: var(--success); color: #fff }
.step.doing .bub { background: var(--primary); border-color: var(--primary); color: #fff; box-shadow: 0 0 0 4px var(--primary-light) }
.step .t { font-size: 11.5px; color: var(--text3) }
.step.done .t, .step.doing .t { color: var(--text); font-weight: 500 }
.term { background: #0f172a; color: #cbd5e1; border-radius: 8px; padding: 14px; font-family: "JetBrains Mono", monospace; font-size: 12.5px; max-height: 360px; overflow: auto; line-height: 1.7 }
.log-line.ok { color: #4ade80 }
.log-line.err { color: #f87171 }
.log-line.warn { color: #fbbf24 }
.log-line.dim { color: #64748b }
.log-line.ai { color: #60a5fa }
.log-line.info { color: #cbd5e1 }
.empty { color: var(--text3); text-align: center; padding: 24px; font-size: 13px }
.notice { background: var(--success-light); border: 1px solid #b5e8d3; color: #0f7a4d; border-radius: 8px; padding: 10px 14px; font-size: 12.5px; margin-bottom: 12px }
.notice.ok { background: var(--success-light); border-color: #b5e8d3; color: #0f7a4d }
.result-actions { margin-top: 12px; display: flex; gap: 10px; flex-wrap: wrap }
.is-loading { animation: spin 1s linear infinite }
@keyframes spin { from { transform: rotate(0deg) } to { transform: rotate(360deg) } }
</style>
