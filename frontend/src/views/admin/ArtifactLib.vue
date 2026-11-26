<template>
  <div class="page">
    <!-- 磁盘简报（P3 只报不删，11-03 拍板）：管理员决策面板，普通用户整卡隐藏
         （11-04 实报：loadDisk 曾无条件打 /admin/disk 触发 403"无权访问"弹提示） -->
    <el-card v-if="isAdmin" shadow="never" class="disk-card">
      <div class="disk-row">
        <span>数据盘已用 <b>{{ disk.data_used_pct }}%</b>（剩 {{ disk.data_free_gb }}GB，警戒线 {{ disk.warn_pct }}%）<span class="hint-dim" style="margin-left:6px">{{ disk.data_path }}</span></span>
        <el-divider direction="vertical" />
        <span>在架货架 <b>{{ disk.shelf_count }}</b> 件 / {{ disk.cache_mb }}MB（账本口径）</span>
        <el-tag v-if="disk.warn" type="warning" size="small" style="margin-left:10px">接近警戒线：请在本页决定去留（平台不会自动删货架）</el-tag>
        <template v-if="sel.length">
          <el-divider direction="vertical" />
          <el-button size="small" @click="selectAllSource">全选本来源</el-button>
        </template>
        <template v-if="isAdmin && sel.length">
          <el-divider direction="vertical" />
          <el-button size="small" type="primary" @click="batchAudienceTo('both')">批量入AI库 {{ sel.length }}</el-button>
          <el-button size="small" @click="batchAudienceTo('human')">批量移出AI库 {{ sel.length }}</el-button>
          <el-button v-if="canManage" size="small" type="danger" plain @click="openBatch('delete')">批量下架 {{ sel.length }}</el-button>
        </template>
        <span class="spacer" />
        <el-button v-if="isAdmin" size="small" @click="openDupes" :loading="dupLoading">重复检测{{ dupCount ? ` (${dupCount})` : '' }}</el-button>
        <el-button v-if="isAdmin" size="small" :loading="reindexing" @click="doReindex">账本对账</el-button>
        <el-button v-if="canManage" type="primary" size="small" @click="uploadVisible = true">上传工具</el-button>
      </div>
    </el-card>

    <!-- 货架 v3（11-25 拍板）：单表面无 tab——编译通过自动上架人人可见可下载；
         管理员动作只剩三件：选入/移出 AI 复用库、下架(物理删)、上传。 -->

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
      <!-- 11-25 v3：状态筛选与列表徽标一一对应（仅后台面） -->
      <el-select v-if="isAdmin" v-model="shelf" placeholder="状态" clearable style="width: 140px" @change="page = 1; load()">
        <el-option value="in" label="在架·已入选" />
        <el-option value="out" label="在架·未入选" />
        <el-option value="ledger" label="仅账本" />
      </el-select>
      <!-- 11-25 货架卫生：沉寂筛选（三账全零超 N 天，只列不删） -->
      <el-select v-if="isAdmin" v-model="idleDays" placeholder="沉寂" clearable style="width: 130px"
                 @change="page = 1; load()">
        <el-option :value="30" label="30天无活跃" />
        <el-option :value="60" label="60天无活跃" />
        <el-option :value="90" label="90天无活跃" />
      </el-select>
      <span v-if="isAdmin && idleStat" class="hint-dim" style="font-size:12px">候选 {{ idleStat.count }} 件 / {{ fmtSize(idleStat.bytes) }}</span>
      <!-- 11-25：批量下载前移到工具条，前台用户同款可用（后端公开端点本就全员同闸） -->
      <el-button v-if="sel.length" size="small" type="primary" plain :loading="batchDl"
                 style="margin-left:8px" @click="doBatchDownload">批量下载 {{ sel.length }}</el-button>
    </div>

    <el-table ref="tableRef" :data="rows" v-loading="loading" size="small" border
              @selection-change="(v: any[]) => sel = v">
      <el-table-column type="selection" width="42" />
      <el-table-column type="index" label="#" width="52" />
      <el-table-column prop="name" label="文件名" :min-width="isAdmin ? 150 : 220" show-overflow-tooltip />
      <el-table-column prop="name_norm" label="软件名" :width="isAdmin ? 205 : undefined" :min-width="isAdmin ? undefined : 170" show-overflow-tooltip />
      <el-table-column label="系统" :width="isAdmin ? 180 : undefined" :min-width="isAdmin ? undefined : 160" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag v-if="row.system_id === 0" size="small" type="warning">通用</el-tag>
          <span v-else>{{ sysName(row.system_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="kind" label="类型" width="70" />
      <el-table-column label="大小" width="70">
        <template #default="{ row }">{{ fmtSize(row.size_bytes) }}</template>
      </el-table-column>
      <!-- 人气账（11-25）：下载次数单列全员可见——人的账；旁边"复用"是机器账，分色不混 -->
      <el-table-column label="下载" width="58" align="center">
        <template #default="{ row }">
          <span v-if="row.dl_count" style="color:var(--el-color-primary)">{{ row.dl_count }}次</span>
          <span v-else class="hint-dim">0次</span>
        </template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="复用" width="60" align="center">
        <template #default="{ row }">{{ row.hits }}用/{{ row.views || 0 }}览</template>
      </el-table-column>
      <!-- 11-25 列表瘦身 v3（两向截图取证）：弹性列按面分配——admin 仅文件名吃富余；
           前台列少，富余分给 文件名/软件名/系统 三列（防单列真空带，vision 复验指出）。
           教训：来源 min-width 弹性曾虚胖、操作 190+hidden 曾裁钮，皆定宽修复 -->
      <el-table-column v-if="isAdmin" label="来源" width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag v-if="row.origin === 'upload'" size="small">上传·{{ row.uploader }}</el-tag>
          <span v-else>{{ row.source_task_key || '-' }}</span>
        </template>
      </el-table-column>
      <!-- AI 复用库并入状态列（v3：一个事实的两半——在架是身份，入选是属性） -->
      <el-table-column label="状态" :width="isAdmin ? 132 : 86">
        <template #default="{ row }">
          <template v-if="!isAdmin">
            <el-tag type="success" size="small">在架</el-tag>
          </template>
          <template v-else>
            <el-tag v-if="row.deleted_at" type="info" size="small">已下架</el-tag>
            <el-tag v-else-if="row.status === 'pending'" type="warning" size="small">待迁移</el-tag>
            <el-tag v-else-if="!row.cache_path" size="small">仅账本</el-tag>
            <el-tag v-else-if="(row.audience || 'both') === 'both'" type="success" size="small">在架·已入选</el-tag>
            <el-tag v-else type="success" size="small">在架·未入选</el-tag>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="操作" :width="isAdmin ? 230 : 120" fixed="right" class-name="ops-col">
        <template #default="{ row }">
          <!-- 11-25：文字钮防折行（四枚色块钮 200px 放不下曾致行高不齐）；颜色分语义 -->
          <el-button size="small" text type="primary" @click="doDownload(row)">下载</el-button>
          <el-button size="small" text @click="showDetail(row)">详情</el-button>
          <el-button v-if="isAdmin && row.cache_path && (row.audience || 'both') !== 'both'"
                     size="small" text type="success" :disabled="!canManage"
                     title="移入 AI 复用库：后续编译任务可从货架自动取用"
                     @click="setAudience(row, 'both')">入AI库</el-button>
          <el-button v-if="isAdmin && row.cache_path && (row.audience || 'both') === 'both'"
                     size="small" text :disabled="!canManage"
                     title="移出 AI 复用库：人仍可下载，AI 不再自动取用"
                     @click="setAudience(row, 'human')">出AI库</el-button>
          <el-button v-if="isAdmin && row.cache_path" size="small" text type="danger"
                     :disabled="!canManage" title="货架文件物理删除，不可恢复（账留墓碑）"
                     @click="openDelete(row)">下架</el-button>

        </template>
      </el-table-column>
    </el-table>
    <el-pagination style="margin-top:12px" layout="total, prev, pager, next" :total="total"
                   :page-size="20" v-model:current-page="page" @current-change="load" />

    <!-- 详情弹层：meta 全量 + 血缘 -->
    <el-dialog v-model="detailVisible" title="产物详情" width="720">
      <el-descriptions v-if="cur" :column="1" border size="small" width="100%" class="detail-desc">
        <el-descriptions-item label="文件">{{ cur.name }}</el-descriptions-item>
        <el-descriptions-item label="软件名 / 版本">{{ cur.name_norm }} / {{ cur.version || '-' }}</el-descriptions-item>
        <el-descriptions-item label="类型 / 大小">{{ cur.kind }} / {{ fmtSize(cur.size_bytes) }}</el-descriptions-item>
        <el-descriptions-item label="目标系统">{{ sysName(cur.system_id) }}（镜像 {{ cur.image || '-' }}）</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="来源">{{ cur.origin === 'upload' ? `管理员上传 · ${cur.uploader}` : `任务晋升 · ${cur.source_task_key}` }}</el-descriptions-item>
        <el-descriptions-item label="构建上下文(ABI 依据)">{{ fmtEnv(cur.build_env, true) }}</el-descriptions-item>
        <el-descriptions-item v-if="cur.git_src" label="源码仓库">
          <a :href="cur.git_src.url" target="_blank" rel="noopener" style="color:var(--el-color-primary)">{{ cur.git_src.url }}</a>
          <template v-if="cur.git_src.branch"> · branch <b>{{ cur.git_src.branch }}</b></template>
        </el-descriptions-item>
        <el-descriptions-item label="身份钉(入库时平台自验)">源码 commit {{ cur.source_commit || '-' }} · sha256 {{ (cur.sha256 || '').slice(0, 16) }}{{ cur.sha256 ? '…' : '' }}</el-descriptions-item>
        <el-descriptions-item label="运行需求(glibc 机提)">{{ cur.glibc_requires || '未检出/非ELF类' }}<template v-if="cur.system_id === 0"> · 按各系统基线自动配给</template></el-descriptions-item>
        <el-descriptions-item label="运行依赖库(ldd 实测)">{{ fmtLibs(cur.runtime_env) }}</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="复用次数 / 最近使用">{{ cur.hits }} / {{ cur.last_used_at || '-' }}</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="下载人气">{{ cur.dl_count || 0 }} 次 / {{ (cur.dl_last_at || '').slice(0, 16).replace('T', ' ') || '从未' }}</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin && cur.status === 'approved'" label="AI 复用库">
          <el-select v-model="curAudience" size="small" style="width:150px" @change="saveAudience">
            <el-option value="both" label="已入选（AI 可自动取用）" /><el-option value="human" label="未入选（仅人下载）" />
          </el-select>
          <span class="tip" style="margin-left:8px">即时生效（审计留痕）；人侧下载不受此开关影响</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="货架路径">{{ cur.cache_path || '（未上架）' }}</el-descriptions-item>
        <el-descriptions-item v-if="isAdmin" label="备注">{{ cur.note || '-' }}</el-descriptions-item>
      </el-descriptions>
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
      <el-alert :closable="false" type="warning" style="margin-bottom:10px"
                title="下架将物理删除各件货架文件与 meta，不可恢复——下方清单逐件过目再决定" />
      <el-table :data="sel" size="small" border max-height="240">
        <el-table-column prop="id" label="#" width="52" />
        <el-table-column prop="name" label="文件" min-width="200" show-overflow-tooltip />
        <el-table-column label="系统" width="130">
          <template #default="{ row }">{{ sysName(row.system_id) }}</template>
        </el-table-column>
        <el-table-column prop="kind" label="类型" width="80" />
        <el-table-column prop="status" label="状态" width="90" />
      </el-table>
      <div v-if="batchResult" style="margin-top:10px;font-size:12px">
        成功 {{ batchResult.ok.length }} 件<span v-if="batchResult.failed.length">；失败 {{ batchResult.failed.length }} 件：</span>
        <ul style="margin:4px 0;padding-left:18px">
          <li v-for="f in batchResult.failed" :key="f.id">#{{ f.id }} {{ f.name }} —— {{ f.why }}</li>
        </ul>
      </div>
      <template #footer>
        <el-button @click="batchVisible = false">关闭</el-button>
        <el-button type="danger" :loading="batchBusy" :disabled="!!batchResult" @click="doBatch">
          确认下架（删除文件）
        </el-button>
      </template>
    </el-dialog>

    <!-- 11-25 货架卫生 D2：重复组面板（只报告，合并逐组人拍板） -->
    <el-dialog v-model="dupVisible" title="重复检测（同系统 · 内容指纹一致）" width="760">
      <div v-if="!dupGroups.length" class="tip">货架干净——无同系统同内容指纹的重复。</div>
      <div v-for="g in dupGroups" :key="g.system_id + g.sha256" class="dup-group">
        <div class="dup-head">
          <b>{{ g.items[0].name_norm }}</b> · {{ sysName(g.system_id) }} · 指纹 {{ g.sha256 }}… · {{ g.items.length }} 行
        </div>
        <el-table :data="g.items" size="small" border>
          <el-table-column width="46">
            <template #default="{ row }">
              <el-radio v-model="g._keep" :value="row.id"><span /></el-radio>
            </template>
          </el-table-column>
          <el-table-column prop="id" label="#" width="56" />
          <el-table-column prop="name" label="文件名" min-width="180" show-overflow-tooltip />
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.on_disk" type="success" size="small">文件在架</el-tag>
              <el-tag v-else size="small" type="info">仅账本</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="账" width="120">
            <template #default="{ row }">{{ row.hits }}用/{{ row.dl_count }}下</template>
          </el-table-column>
          <el-table-column label="大小" width="80">
            <template #default="{ row }">{{ fmtSize(row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="来源" width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.origin === 'upload' ? '上传' : row.source_task_key }}</template>
          </el-table-column>
        </el-table>
        <div class="dup-actions">
          <el-button size="small" type="warning" :loading="batchBusy"
                     :disabled="!canManage" @click="doDedupe(g)">
            合并 {{ g.items.length - 1 }} 件到所选保留行
          </el-button>
        </div>
      </div>
      <template #footer><el-button @click="dupVisible = false">关闭</el-button></template>
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
          <el-progress v-if="uploading" :percentage="upPct" style="width:100%;margin-top:8px" />
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
import { useRoute } from 'vue-router'

const auth = useAuthStore()
const route = useRoute()
// 货架 v3（11-25 拍板）：管理面由入口决定而非角色——admin 走前台"/artifacts"
// 与普通用户所见完全一致；仅后台入口"/admin/artifacts"（菜单=货架管理）给管理动作。
const isAdmin = computed(() => auth.user?.role === 'admin' && route.path.startsWith('/admin/'))
// 简化拍板①（11-06）：驳回/下架全部=require_admin（任意管理员）；
// restore/ledger 后端仍 require_preset_admin（应急后门未上 UI）——isPreset 随退役钮一同退出前端
const canManage = computed(() => isAdmin.value)

const KINDS = ['whl', 'tgz', 'rpm', 'binary', 'node_binding', 'so', 'a', 'tool', 'other']
const rows = ref<any[]>([])
const tableRef = ref<any>(null)
const total = ref(0)
const page = ref(1)
const search = ref('')
const kind = ref('')
const systemId = ref<number | ''>('')
const shelf = ref('')   // 11-25 v3 状态筛选：in/out/ledger
const idleDays = ref<number | ''>('')   // 11-25 沉寂筛选：30/60/90
const idleStat = ref<any>(null)
const loading = ref(false)
const systems = ref<any[]>([])
const disk = reactive<any>({ data_used_pct: '-', data_free_gb: '-', data_path: '', shelf_count: '-', cache_mb: '-', warn: false, warn_pct: 85 })

const cur = ref<any>(null)
const detailVisible = ref(false)
const delVisible = ref(false)
const uploadVisible = ref(false)
const uploading = ref(false)
const upPct = ref(0)
const reindexing = ref(false)
// 11-25 货架卫生 D2：重复检测面板
const dupVisible = ref(false)
const dupLoading = ref(false)
const dupGroups = ref<any[]>([])
const dupCount = computed(() => dupGroups.value.length)
async function openDupes() {
  dupLoading.value = true
  try {
    const r: any = await api.get('/admin/artifacts/dupes')
    const d = r?.data || r || {}
    dupGroups.value = (d.groups || []).map((g: any) => ({ ...g, _keep: g.keep_suggest }))
    dupVisible.value = true
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '检测失败') }
  finally { dupLoading.value = false }
}
async function doDedupe(g: any) {
  batchBusy.value = true
  try {
    const r: any = await api.post('/admin/artifacts/dedupe', { keep_id: g._keep })
    ElMessage.success(r?.message || '已合并')
    await openDupes()          // 重扫刷新组列表
    load(); loadDisk()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '合并失败') }
  finally { batchBusy.value = false }
}
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
    sel.value.forEach((x) => { x.dl_count = (x.dl_count || 0) + 1 })   // 11-25 就地反映（后端整包已发出即逐件记账）
  } catch (e: any) { ElMessage.error(e?.message || '批量下载失败') }
  finally { batchDl.value = false }
}
const batchVisible = ref(false)
const batchAction = ref<'delete'>('delete')   // v3：批量仅剩下架
const batchBusy = ref(false)
const batchResult = ref<any>(null)
const batchTitle = computed(() => '批量下架')
function openBatch(a: 'delete') {
  batchAction.value = a
  batchResult.value = null
  batchVisible.value = true
}
// v3 批量入/出 AI 库（逐件走单件 audience 端点——服务端统一审计/INDEX 重建，
// 上限 100 件逐发毫秒级，不另造批量端点）
async function batchAudienceTo(aud: 'both' | 'human') {
  batchBusy.value = true
  try {
    let okN = 0
    for (const x of sel.value) {
      await api.post(`/admin/artifacts/${x.id}/audience`, new URLSearchParams({ audience: aud }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
      okN++
    }
    ElMessage.success(`已${aud === 'both' ? '移入' : '移出'} AI 复用库 ${okN} 件`)
    load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '批量操作失败') }
  finally { batchBusy.value = false }
}
async function setAudience(row: any, aud: 'both' | 'human') {
  try {
    await api.post(`/admin/artifacts/${row.id}/audience`, new URLSearchParams({ audience: aud }),
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
    ElMessage.success(aud === 'both' ? '已移入 AI 复用库（后续任务可自动取用）' : '已移出 AI 复用库（人下载不受影响）')
    load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '操作失败') }
}
async function doBatch() {
  batchBusy.value = true
  try {
    const r: any = await api.post(`/admin/artifacts/batch-${batchAction.value}`, {
      ids: sel.value.map((x) => x.id),
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
function fmtLibs(raw: string) {
  if (!raw || raw === '[]') return '未记录（whl/node 类可省）'
  try {
    const a = JSON.parse(raw)
    return Array.isArray(a) && a.length ? a.join('  ') : '未记录（whl/node 类可省）'
  } catch { return raw.slice(0, 80) }
}
function showDetail(row: any) { cur.value = row; curAudience.value = (row.audience || 'both') === 'ai' ? 'both' : (row.audience || 'both'); detailVisible.value = true }
const curAudience = ref('both')
async function saveAudience() {
  try {
    await api.post(`/admin/artifacts/${cur.value.id}/audience`,
      new URLSearchParams({ audience: curAudience.value }),
      { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
    ElMessage.success('用途已更新（两边视图即时生效）')
    load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '更新失败'); curAudience.value = cur.value.audience || 'both' }
}

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
    row.dl_count = (row.dl_count || 0) + 1   // 11-25：下载即走 blob，就地 +1 免整表刷新
  } catch (e: any) { ElMessage.error(e?.message || '下载失败') }
}

async function load() {
  loading.value = true
  try {
    if (!isAdmin.value) {
      // 用户前台（含 admin 走前台入口）：公开端点，服务端钉死仅在架件
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
    // 货架 v3：后台单表=在架件（墓碑行查询不暴露 UI 的旧拍板不变）
    params.status = 'approved'
    if (shelf.value) params.shelf = shelf.value
    if (idleDays.value) params.idle_days = idleDays.value
    const t: any = await api.get('/admin/artifacts', { params })
    const d = t?.data || t || {}          // 拦截器返回 ApiResponse 整包，取 .data（平台惯例同 SkillLib）
    rows.value = d.items || []
    sources.value = d.sources || sources.value
    total.value = d.total || 0
    idleStat.value = d.idle_stat || null
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
    upPct.value = 0
    const r: any = await api.post('/admin/artifacts/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      // 11-09 修"上传成功没提示"：全局 30s 超时必掐大文件（rust 280MB 案）——
      // 浏览器 abort 但后端流式接收多半已完成=静默成功假象。仅此调用长超时 30min，勿动全局
      timeout: 30 * 60 * 1000,
      onUploadProgress: (ev: any) => { if (ev.total) upPct.value = Math.round((ev.loaded * 100) / ev.total) },
    })
    ElMessage.success(r?.message || '已上架')
    uploadVisible.value = false
    Object.assign(up, { system_id: '', name: '', version: '', note: '', file: null })
    load(); loadDisk()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (detail) ElMessage.error(detail)
    else if (/timeout|ECONNABORTED|network|ERR_NETWORK/i.test(`${e?.code || ''} ${e?.message || ''}`))
      ElMessage.error('上传连接中断：服务器可能仍在接收完毕，请稍后刷新列表确认——勿重复上传（同名件会产生重复货架行）')
    else ElMessage.error('上传失败')
  }
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
/* 详情弹框（11-25 用户报"左列太短拉特别长"）：标签列定宽，内容列自适应换行 */
.detail-desc :deep(.el-descriptions__cell.el-descriptions__label) { width: 150px; min-width: 150px; }
.detail-desc :deep(.el-descriptions__content) { word-break: break-word; }
/* 操作列单行排齐（截图复验：文字钮默认 12px 间距致四钮折两行、行高不齐） */
:deep(.ops-col .cell) { white-space: nowrap; overflow: visible; }
:deep(.ops-col .el-button + .el-button) { margin-left: 4px; }
.dup-group { margin-bottom: 14px; }
.dup-head { font-size: 13px; margin-bottom: 6px; color: var(--el-text-color-primary); }
.dup-actions { margin-top: 6px; text-align: right; }
</style>
