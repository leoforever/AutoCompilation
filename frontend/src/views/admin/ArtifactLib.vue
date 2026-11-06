<template>
  <div class="page">
    <!-- 磁盘简报（P3 只报不删，11-03 拍板）：管理员决策面板，普通用户整卡隐藏
         （11-04 实报：loadDisk 曾无条件打 /admin/disk 触发 403"无权访问"弹提示） -->
    <el-card v-if="isAdmin" shadow="never" class="disk-card">
      <div class="disk-row">
        <span>磁盘已用 <b>{{ disk.root_used_pct }}%</b>（剩 {{ disk.root_free_gb }}GB，警戒线 {{ disk.warn_pct }}%）</span>
        <el-divider direction="vertical" />
        <span>在架货架 <b>{{ disk.shelf_count }}</b> 件 / {{ disk.cache_mb }}MB（账本口径）</span>
        <el-tag v-if="disk.warn" type="warning" size="small" style="margin-left:10px">接近警戒线：请在本页决定去留（平台不会自动删货架）</el-tag>
        <template v-if="sel.length">
          <el-divider direction="vertical" />
          <el-button size="small" @click="selectAllSource">全选本来源</el-button>
          <el-button size="small" type="primary" plain :loading="batchDl" @click="doBatchDownload">批量下载 {{ sel.length }}</el-button>
        </template>
        <template v-if="isAdmin && sel.length">
          <el-divider direction="vertical" />
          <el-button size="small" type="success" @click="openBatch('approve')">批量通过 {{ sel.length }}</el-button>
          <el-button size="small" type="warning" @click="openBatch('reject')">批量驳回 {{ sel.length }}</el-button>
          <el-button v-if="canManage" size="small" type="danger" plain @click="openBatch('delete')">批量下架 {{ sel.length }}</el-button>
        </template>
        <span class="spacer" />
        <el-button v-if="isAdmin" size="small" :loading="reindexing" @click="doReindex">账本对账</el-button>
        <el-button v-if="canManage" type="primary" size="small" @click="uploadVisible = true">上传工具</el-button>
      </div>
    </el-card>

    <el-tabs v-if="isAdmin" v-model="tab" @tab-change="load">
      <el-tab-pane label="在架货架" name="shelf" />
      <el-tab-pane name="pending">
        <template #label>
          待晋升审批
          <el-badge v-if="pendingCount" :value="pendingCount" class="tab-badge" />
        </template>
      </el-tab-pane>

    </el-tabs>

    <div class="toolbar">
      <el-input v-model="search" placeholder="搜软件名 / 文件名 / 任务号" clearable style="width: 260px" @change="load" />
      <el-select v-model="source" placeholder="来源" clearable filterable style="width: 210px" @change="load">
        <el-option v-for="sc in sources" :key="sc" :label="sc === 'upload' ? '上传件' : sc" :value="sc" />
      </el-select>
      <el-select v-model="kind" placeholder="类型" clearable style="width: 130px" @change="load">
        <el-option v-for="k in KINDS" :key="k" :label="k" :value="k" />
      </el-select>
      <el-select v-model="systemId" placeholder="目标系统" clearable style="width: 200px" @change="load">
        <el-option v-for="sy in systems" :key="sy.system_id" :label="sy.name" :value="sy.system_id" />
      </el-select>
    </div>

    <el-table ref="tableRef" :data="rows" v-loading="loading" size="small" border
              @selection-change="(v: any[]) => sel = v">
      <el-table-column v-if="isAdmin" type="selection" width="42" :selectable="() => isAdmin" />
      <el-table-column type="index" label="#" width="52" />
      <el-table-column prop="name" label="文件名" min-width="220" show-overflow-tooltip />
      <el-table-column prop="name_norm" label="软件名" width="150" show-overflow-tooltip />
      <el-table-column label="系统" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.system_id === 0" size="small" type="warning">通用</el-tag>
          <span v-else>{{ sysName(row.system_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="kind" label="类型" width="105" />
      <el-table-column label="大小" width="90">
        <template #default="{ row }">{{ fmtSize(row.size_bytes) }}</template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="复用" width="88" align="center">
        <template #default="{ row }">{{ row.hits }}用/{{ row.views || 0 }}览</template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="最近使用" width="120">
        <template #default="{ row }">{{ (row.last_used_at || '').slice(5, 16) || '-' }}</template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="来源" width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag v-if="row.origin === 'upload'" size="small">上传·{{ row.uploader }}</el-tag>
          <span v-else>{{ row.source_task_key || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="构建上下文" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="{ unk: !row.build_env || row.build_env === '{}' }">{{ fmtEnv(row.build_env) }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="状态" width="92">
        <template #default="{ row }">
          <el-tag v-if="row.deleted_at" type="info" size="small">已下架</el-tag>
          <el-tag v-else-if="row.status === 'pending'" type="warning" size="small">待晋升</el-tag>
          <el-tag v-else-if="row.status === 'rejected'" type="danger" size="small">已驳回</el-tag>
          <el-tag v-else-if="row.cache_path" type="success" size="small">在架</el-tag>
          <el-tag v-else size="small">仅账本</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" :width="isAdmin ? 210 : 130" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="primary" plain @click="doDownload(row)">下载</el-button>
          <el-button size="small" @click="showDetail(row)">详情</el-button>
          <el-button v-if="isAdmin && row.status === 'pending'" size="small" type="success"
                     :disabled="!canManage" @click="doApprove(row)">通过</el-button>
          <el-button v-if="isAdmin && row.status === 'pending'" size="small" type="warning"
                     :disabled="!canManage" @click="openReject(row)">驳回</el-button>
          <el-button v-if="isAdmin && tab === 'shelf'" size="small" type="danger" plain
                     :disabled="!canManage" title="货架文件物理删除，不可恢复（账留墓碑）"
                     @click="openDelete(row)">下架</el-button>

        </template>
      </el-table-column>
    </el-table>
    <el-pagination style="margin-top:12px" layout="total, prev, pager, next" :total="total"
                   :page-size="20" v-model:current-page="page" @current-change="load" />

    <!-- 详情弹层：meta 全量 + 血缘 -->
    <el-dialog v-model="detailVisible" title="产物详情" width="640">
      <el-descriptions v-if="cur" :column="1" border size="small">
        <el-descriptions-item label="文件">{{ cur.name }}</el-descriptions-item>
        <el-descriptions-item label="软件名 / 版本">{{ cur.name_norm }} / {{ cur.version || '-' }}</el-descriptions-item>
        <el-descriptions-item label="类型 / 大小">{{ cur.kind }} / {{ fmtSize(cur.size_bytes) }}</el-descriptions-item>
        <el-descriptions-item label="目标系统">{{ sysName(cur.system_id) }}（镜像 {{ cur.image || '-' }}）</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="来源">{{ cur.origin === 'upload' ? `管理员上传 · ${cur.uploader}` : `任务晋升 · ${cur.source_task_key}` }}</el-descriptions-item>
        <el-descriptions-item label="构建上下文(ABI 依据)">{{ fmtEnv(cur.build_env, true) }}</el-descriptions-item>
        <el-descriptions-item label="身份钉(晋升时平台自验)">源码 commit {{ cur.source_commit || '-' }} · sha256 {{ (cur.sha256 || '').slice(0, 16) }}{{ cur.sha256 ? '…' : '' }}</el-descriptions-item>
        <el-descriptions-item label="运行需求(glibc 机提)">{{ cur.glibc_requires || '未检出/非ELF类' }}<template v-if="cur.system_id === 0"> · 按各系统基线自动配给</template></el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="复用次数 / 最近使用">{{ cur.hits }} / {{ cur.last_used_at || '-' }}</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="货架路径">{{ cur.cache_path || '（未上架）' }}</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="备注">{{ cur.note || '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>

    <!-- 驳回理由 -->
    <el-dialog v-model="rejectVisible" title="驳回晋升" width="480">
      <p style="margin:0 0 8px;color:var(--el-text-color-secondary);font-size:13px">
        驳回后文件留在任务晋升区随任务生命周期清理；理由留痕并计入审计。</p>
      <el-input v-model="rejectReason" type="textarea" :rows="3" placeholder="驳回理由（如：版本过旧/上下文缺失/与在架件重复）" />
      <template #footer>
        <el-button @click="rejectVisible = false">取消</el-button>
        <el-button type="warning" :disabled="!rejectReason.trim()" @click="doReject">确认驳回</el-button>
      </template>
    </el-dialog>

    <!-- 下架确认（简化拍板：下架=删货终局；restore/ledger 留后端应急，UI 不挂） -->
    <el-dialog v-model="delVisible" title="下架产物" width="480">
      <p>下架 <b>{{ cur?.name }}</b></p>
      <p class="tip">货架文件与 meta 将<b>物理删除，不可恢复</b>；账本留墓碑（复用史/指纹入审计可考）。
        需要再用它=重新上传或重编审批。</p>
      <template #footer>
        <el-button @click="delVisible = false">取消</el-button>
        <el-button type="danger" @click="doDelete">确认下架（删除文件）</el-button>
      </template>
    </el-dialog>

    <!-- 上传对话框（人放的免审直入；拍板6 权限=管理角色） -->
    <el-dialog v-model="batchVisible" :title="batchTitle" width="640">
      <el-alert v-if="batchAction === 'delete'" :closable="false" type="warning" style="margin-bottom:10px"
                title="下架将物理删除各件货架文件与 meta，不可恢复——下方清单逐件过目再决定" />
      <el-alert v-else-if="batchAction === 'approve'" :closable="false" type="info" style="margin-bottom:10px"
                title="通过后文件移入货架并对全部编译任务可见（同名件按'新版本赢'合并历史账）" />
      <el-table :data="sel" size="small" border max-height="240">
        <el-table-column prop="id" label="#" width="52" />
        <el-table-column prop="name" label="文件" min-width="200" show-overflow-tooltip />
        <el-table-column label="系统" width="130">
          <template #default="{ row }">{{ sysName(row.system_id) }}</template>
        </el-table-column>
        <el-table-column prop="kind" label="类型" width="80" />
        <el-table-column prop="status" label="状态" width="90" />
      </el-table>
      <div v-if="batchAction === 'reject'" style="margin-top:10px">
        <el-input v-model="batchReason" placeholder="统一驳回理由（逐件留痕）" maxlength="120" show-word-limit />
      </div>
      <div v-if="batchResult" style="margin-top:10px;font-size:12px">
        成功 {{ batchResult.ok.length }} 件<span v-if="batchResult.failed.length">；失败 {{ batchResult.failed.length }} 件：</span>
        <ul style="margin:4px 0;padding-left:18px">
          <li v-for="f in batchResult.failed" :key="f.id">#{{ f.id }} {{ f.name }} —— {{ f.why }}</li>
        </ul>
      </div>
      <template #footer>
        <el-button @click="batchVisible = false">关闭</el-button>
        <el-button :type="batchAction === 'delete' ? 'danger' : 'primary'" :loading="batchBusy" :disabled="!!batchResult" @click="doBatch">
          {{ batchAction === 'approve' ? '确认通过' : batchAction === 'reject' ? '确认驳回' : '确认下架（删除文件）' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="uploadVisible" title="上传工具到复用货架" width="560">
      <el-form label-width="110px">
        <el-form-item label="目标系统">
          <el-select v-model="up.system_id" style="width:100%" clearable
                     placeholder="留空=公共池（所有系统货架可见）">
            <el-option v-for="sy in systems" :key="sy.system_id" :label="sy.name" :value="sy.system_id" />
          </el-select>
          <div v-if="isCommonKindWarn" class="hint-warn">
            链接产物类（whl/so/rpm 等）绑定构建环境 glibc/ABI，不允许进公共池——请选目标系统
          </div>
          <div v-else class="hint-dim">
            公共池适合：工具母包（node/rust/cmake）、上游源码包/mirror——入库自动机提 glibc 需求，按各系统基线配给
          </div>
        </el-form-item>
        <el-form-item label="软件名" required>
          <el-input v-model="up.name" placeholder="如 node / rust / cmake（货架目录键）" />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="up.version" placeholder="如 v24.22.1（进文件名，AI 靠它认货）" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="up.note" type="textarea" :rows="2" placeholder="给后续管理员看的备注（选填）" />
        </el-form-item>
        <el-form-item label="文件" required>
          <input type="file" ref="fileInput" @change="onFile" />
          <div class="tip" v-if="up.file">已选 {{ up.file.name }}（{{ fmtSize(up.file.size) }}，≤2048MB）</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="doUpload">上传上架</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/utils/request'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const isAdmin = computed(() => auth.user?.role === 'admin')
// 简化拍板①（11-06）：驳回/下架全部=require_admin（任意管理员）；
// restore/ledger 后端仍 require_preset_admin（应急后门未上 UI）——isPreset 随退役钮一同退出前端
const canManage = computed(() => isAdmin.value)

const KINDS = ['whl', 'tgz', 'rpm', 'binary', 'node_binding', 'so', 'a', 'tool', 'other']
const tab = ref('shelf')
const rows = ref<any[]>([])
const tableRef = ref<any>(null)
const total = ref(0)
const page = ref(1)
const search = ref('')
const kind = ref('')
const systemId = ref<number | ''>('')
const loading = ref(false)
const pendingCount = ref(0)
const systems = ref<any[]>([])
const disk = reactive<any>({ root_used_pct: '-', root_free_gb: '-', shelf_count: '-', cache_mb: '-', warn: false, warn_pct: 85 })

const cur = ref<any>(null)
const detailVisible = ref(false)
const rejectVisible = ref(false)
const rejectReason = ref('')
const delVisible = ref(false)
const uploadVisible = ref(false)
const uploading = ref(false)
const reindexing = ref(false)
const sel = ref<any[]>([])
const source = ref('')
const sources = ref<string[]>([])
const batchDl = ref(false)
function selectAllSource() {
  // 勾满当前筛选（=选了来源时就是"该来源全部在架件"）——跨页场景先按筛选后本集导，2GB 上限兜底
  rows.value.forEach((r) => { if (!sel.value.includes(r)) tableRef.value?.toggleRowSelection(r, true) })
}
async function doBatchDownload() {
  batchDl.value = true
  try {
    const token = localStorage.getItem('token') || ''
    const r = await fetch('/api/artifacts/batch-download', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: sel.value.map((x) => x.id) }),
    })
    if (!r.ok) {
      let msg = `HTTP ${r.status}`
      try { msg = (await r.json()).detail || msg } catch { /* 非 JSON 保留码 */ }
      throw new Error(msg)
    }
    const blob = await r.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `shelf-batch-${sel.value.length}items.tar.gz`
    a.click(); URL.revokeObjectURL(a.href); ElMessage.success('批量包已开始下载')
  } catch (e: any) { ElMessage.error(e?.message || '批量下载失败') }
  finally { batchDl.value = false }
}
const batchVisible = ref(false)
const batchAction = ref<'approve' | 'reject' | 'delete'>('approve')
const batchReason = ref('批量驳回')
const batchBusy = ref(false)
const batchResult = ref<any>(null)
const batchTitle = computed(() => ({ approve: '批量审批上架', reject: '批量驳回', delete: '批量下架' }[batchAction.value]))
function openBatch(a: 'approve' | 'reject' | 'delete') {
  batchAction.value = a
  batchResult.value = null
  batchVisible.value = true
}
async function doBatch() {
  batchBusy.value = true
  try {
    const r: any = await api.post(`/admin/artifacts/batch-${batchAction.value}`, {
      ids: sel.value.map((x) => x.id), reason: batchReason.value,
    })
    batchResult.value = r?.data ?? { ok: [], failed: [] }
    ElMessage.success(r?.message || '批量完成')
    load(); if (isAdmin.value) loadDisk()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '批量操作失败') }
  finally { batchBusy.value = false }
}

const up = reactive<any>({ system_id: '' as any, name: '', version: '', note: '', file: null as File | null })

function fmtSize(b: number) {
  if (!b) return '-'
  if (b < 1024 * 1024) return `${Math.round(b / 1024)}KB`
  return `${(b / 1024 / 1024).toFixed(1)}MB`
}
function sysName(id: number) {
  if (id === 0) return '通用（全系统）'
  return systems.value.find((s) => s.system_id === id)?.name || `#${id}`
}
function fmtEnv(raw: string, full = false) {
  if (!raw || raw === '{}') return '⚠ 上下文未知'
  try {
    const o = JSON.parse(raw)
    const kv = Object.entries(o).map(([k, v]) => `${k}=${v}`)
    return full ? kv.join('  ') : kv.slice(0, 3).join(' ')
  } catch { return raw.slice(0, 60) }
}
function showDetail(row: any) { cur.value = row; detailVisible.value = true }

async function doDownload(row: any) {
  // 与任务产物下载同姿势：fetch+blob 带 JWT，后端人话（410 灭失/404 不在架）透传
  try {
    const token = localStorage.getItem('token') || ''
    const r = await fetch(`/api/artifacts/${row.id}/download`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!r.ok) {
      let msg = `HTTP ${r.status}`
      try { msg = (await r.json()).detail || msg } catch { /* 非 JSON 错体保留状态码 */ }
      throw new Error(msg)
    }
    const blob = await r.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.name.toLowerCase().startsWith(row.name_norm.toLowerCase())
      ? row.name : `${row.name_norm}-${row.name}`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) { ElMessage.error(e?.message || '下载失败') }
}

async function load() {
  loading.value = true
  try {
    if (!isAdmin.value) {
      // 普通用户：公开端点（服务端 SQL 钉死仅在架件；无 status 入口、无 pending/账本概念）
      const params: any = { page: page.value, page_size: 20 }
      if (search.value) params.search = search.value
      if (source.value) params.source = source.value
      if (kind.value) params.kind = kind.value
      if (systemId.value) params.system_id = systemId.value
      const t: any = await api.get('/artifacts', { params })
      const d = t?.data || t || {}
      rows.value = d.items || []
      sources.value = d.sources || sources.value
      total.value = d.total || 0
      return
    }
    const params: any = { page: page.value, page_size: 20 }
    if (search.value) params.search = search.value
    if (source.value) params.source = source.value
    if (kind.value) params.kind = kind.value
    if (systemId.value) params.system_id = systemId.value
    // 简化后仅剩两 tab，状态恒有——include_deleted 参数面退役（墓碑行查询不再暴露 UI）
    params.status = tab.value === 'shelf' ? 'approved' : 'pending' 
    const t: any = await api.get('/admin/artifacts', { params })
    const d = t?.data || t || {}          // 拦截器返回 ApiResponse 整包，取 .data（平台惯例同 SkillLib）
    rows.value = d.items || []
    sources.value = d.sources || sources.value
    total.value = d.total || 0
    if (tab.value !== 'pending') {
      const p: any = await api.get('/admin/artifacts', { params: { page: 1, page_size: 1, status: 'pending' } })
      pendingCount.value = (p?.data || p)?.total || 0
    }
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '加载失败') }
  finally { loading.value = false }
}

async function loadDisk() {
  if (!isAdmin.value) return          // 403 弹提示根治：普通用户不发 admin 请求
  try {
    Object.assign(disk, (await api.get('/admin/artifacts/disk'))?.data || {})
  } catch { /* 简报失败不挡页面 */ }
}
async function loadSystems() {
  try {
    const s: any = await api.get('/compile/options/systems')
    systems.value = s?.data || []
  } catch { systems.value = [] }
}

async function doApprove(row: any) {
  try {
    await api.post(`/admin/artifacts/${row.id}/approve`)
    ElMessage.success('已晋升上架（后续任务可见可复用）')
    load(); loadDisk()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '审批失败') }
}
function openReject(row: any) { cur.value = row; rejectReason.value = ''; rejectVisible.value = true }
async function doReject() {
  try {
    await api.post(`/admin/artifacts/${cur.value.id}/reject`, new URLSearchParams({ reason: rejectReason.value }),
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
    ElMessage.success('已驳回（理由留痕）')
    rejectVisible.value = false; load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '驳回失败') }
}
function openDelete(row: any) { cur.value = row; delVisible.value = true }
async function doDelete() {
  try {
    await api.delete(`/admin/artifacts/${cur.value.id}`)
    ElMessage.success('已下架（文件已删除）')
    delVisible.value = false; load(); loadDisk()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '下架失败') }
}
async function doReindex() {
  reindexing.value = true
  try {
    const r: any = await api.post('/admin/artifacts/reindex')
    const rd = r?.data || r
    ElMessage.success(`对账完成：新增${rd.created ?? 0} 更新${rd.updated ?? 0} 缺失标注${rd.missing_marked ?? 0}（漂移只报不删）`)
    load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '对账失败') }
  finally { reindexing.value = false }
}
function onFile(ev: Event) { up.file = (ev.target as HTMLInputElement).files?.[0] || null }
const LINKABLE_EXT = ['.whl', '.so', '.a', '.node', '.rpm', '.deb']
const isCommonKindWarn = computed(() => {
  // 第一层资格闸的前端联动提示（后端 422 是硬闸）：链接产物类不得进公共池
  const n = (up.file?.name || '').toLowerCase()
  return !up.system_id && LINKABLE_EXT.some((e) => n.endsWith(e))
})

async function doUpload() {
  if (!up.file || !up.name) { ElMessage.warning('软件名与文件必填；目标系统留空=上公共池'); return }
  if (isCommonKindWarn.value) { ElMessage.error('链接产物类（whl/so/rpm 等）不允许进公共池，请选目标系统'); return }
  const fd = new FormData()
  fd.append('file', up.file)
  fd.append('system_id', String(up.system_id || 0))
  fd.append('name', up.name)
  fd.append('version', up.version)
  fd.append('note', up.note)
  uploading.value = true
  try {
    const r: any = await api.post('/admin/artifacts/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },    // 照 Workbench 上传先例
    })
    ElMessage.success(r?.message || '已上架')
    uploadVisible.value = false
    Object.assign(up, { system_id: '', name: '', version: '', note: '', file: null })
    load(); loadDisk()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '上传失败') }
  finally { uploading.value = false }
}

onMounted(() => { loadSystems(); load(); loadDisk() })
</script>

<style scoped>
.hint-warn { color: var(--el-color-danger); font-size: 12px; line-height: 1.5; margin-top: 4px; }
.hint-dim { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; margin-top: 4px; }
.page { padding: 4px; }
.disk-card { margin-bottom: 8px; }
.disk-row { display: flex; align-items: center; font-size: 13px; gap: 4px; flex-wrap: wrap; }
.spacer { flex: 1; }
.toolbar { display: flex; gap: 10px; margin-bottom: 10px; align-items: center; }
.tip { color: var(--el-text-color-secondary); font-size: 12px; margin: 4px 0; }
.unk { color: var(--el-color-warning); }
.tab-badge { margin-left: 6px; }
</style>
