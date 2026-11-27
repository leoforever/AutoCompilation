<template>
  <div class="page">
    <div class="page-title">编译执行机管理</div>

    <div class="card">
      <div class="toolbar">
        <el-button type="primary" @click="showAdd">+ 登记执行机</el-button>
        <el-button :loading="loading" @click="load">刷新状态</el-button>
        <span class="muted" style="margin-left:6px">
          在线 {{ counts.online }} · 冷却 {{ counts.cooldown }} · 摘除 {{ counts.removed }}
        </span>
      </div>

      <el-table :data="rows" v-loading="loading" size="small" row-key="id">
        <el-table-column type="index" label="#" width="50" />
        <el-table-column label="名称" min-width="150">
          <template #default="{ row }">
            <b>{{ row.name }}</b>
            <el-tag v-if="row.is_builtin" size="small" type="info" style="margin-left:6px">内置</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="地址" min-width="200">
          <template #default="{ row }">
            <code v-if="row.docker_host" class="host-code">{{ row.docker_host }}</code>
            <span v-else class="muted">本机 docker socket</span>
          </template>
        </el-table-column>
        <el-table-column label="架构" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.arch === 'loongarch64' ? 'primary' : 'warning'">{{ row.arch }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.status === 'online'" size="small" type="success">在线</el-tag>
            <el-tooltip v-else-if="row.status === 'cooldown'"
                        :content="'冷却到 ' + (row.cooldown_until || '—') + '（自检通过可提前回池）'">
              <el-tag size="small" type="warning">冷却中</el-tag>
            </el-tooltip>
            <el-tag v-else size="small" type="info">已摘除</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="在跑" width="70" align="center" prop="running">
          <template #default="{ row }">
            <b v-if="row.running > 0" style="color:var(--el-color-primary)">{{ row.running }}</b>
            <span v-else class="muted">0</span>
          </template>
        </el-table-column>
        <el-table-column label="核数" width="70" align="center">
          <template #default="{ row }">{{ row.nproc || '—' }}</template>
        </el-table-column>
        <el-table-column label="自检信息" min-width="230">
          <template #default="{ row }">
            <span v-if="!row.checks.version" class="muted">未自检{{ row.last_ping_at ? '（曾通 ' + row.last_ping_at + '）' : '' }}</span>
            <span v-else class="muted">
              {{ row.checks.version }} · {{ row.checks.disk_free_gb ? row.checks.disk_free_gb + 'G 余' : '盘未探' }}
              <template v-if="row.checks.arch && row.checks.arch !== row.arch">
                <el-tag size="small" type="danger" style="margin-left:4px">
                  上报 {{ row.checks.arch }} ≠ 登记 {{ row.arch }}
                </el-tag>
              </template>
              <span v-if="row.checks.checked_at" class="muted"> · {{ row.checks.checked_at }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="预热镜像" width="90" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="row.images.length" :content="row.images.join('\n')" placement="top">
              <span>{{ row.images.length }} 个</span>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" :loading="probing === row.id"
                       :disabled="row.status === 'removed'" @click="doProbe(row)">自检</el-button>
            <el-button v-if="!row.is_builtin" size="small" link type="primary" @click="showEdit(row)">编辑</el-button>
            <el-button v-if="!row.is_builtin && row.status !== 'cooldown'" size="small" link type="warning"
                       @click="setStatus(row, 'cooldown')">挂起</el-button>
            <el-button v-if="!row.is_builtin && row.status !== 'online'" size="small" link type="success"
                       @click="setStatus(row, 'online')">回池</el-button>
            <el-button v-if="!row.is_builtin" size="small" link type="danger" @click="doDelete(row)">{{ row._used ? '摘除' : '删除' }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-tip">内置控制机永远在列；点「+ 登记执行机」往池里接第一台。</div>
        </template>
      </el-table>
    </div>

    <!-- 登记 / 编辑 弹窗 -->
    <el-dialog v-model="dlgVisible" :title="isEdit ? '编辑执行机' : '登记执行机'" width="560">
      <el-form label-width="110px" size="default">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：10.40.80.91 试验田" maxlength="64" />
        </el-form-item>
        <el-form-item label="Docker 地址" required>
          <el-input v-model="form.docker_host" placeholder="ssh://root@10.40.80.91" maxlength="255" />
          <div class="form-hint">ssh 隧道起步（免密先行，远端零配置）；留空=本机——但本机已是内置项，勿重复登记</div>
        </el-form-item>
        <el-form-item label="架构">
          <el-select v-model="form.arch" style="width:100%">
            <el-option label="loongarch64（龙芯）" value="loongarch64" />
            <el-option label="loongarch64-abi1.0（旧世界 · 4.19 内核）" value="loongarch64-abi1.0" />
            <el-option label="x86_64" value="x86_64" />
            <el-option label="aarch64" value="aarch64" />
          </el-select>
          <div class="form-hint">任务只会派给架构匹配的执行机；填错=该架构任务无人认领（不是回落本机）</div>
        </el-form-item>
        <el-form-item label="核数">
          <el-input-number v-model="form.nproc" :min="0" :max="4096" />
          <div class="form-hint">可留 0，自检后自动回填</div>
        </el-form-item>
        <el-form-item label="预热镜像">
          <el-input v-model="form.imagesText" type="textarea" :rows="3"
                    placeholder="每行一个完整镜像名:tag（lcr.loongnix.cn/...）" />
          <div class="form-hint">仅作能力登记，不做派单否决——缺镜像时任务现场自动 pull（拉不通才失败）</div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.note" maxlength="200" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ isEdit ? '保存' : '拨号并登记' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '@/utils/request'
import { ElMessage, ElMessageBox } from 'element-plus'

const loading = ref(false)
const saving = ref(false)
const probing = ref(0)
const rows = ref<any[]>([])

const counts = computed(() => {
  const c = { online: 0, cooldown: 0, removed: 0 } as any
  rows.value.forEach((r) => { c[r.status] = (c[r.status] || 0) + 1 })
  return c
})

async function load() {
  loading.value = true
  try {
    const r: any = await api.get('/admin/executors')
    rows.value = (r?.data || r || {}).items || []
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally { loading.value = false }
}

const dlgVisible = ref(false)
const isEdit = ref(false)
const form = ref({ id: 0, name: '', docker_host: '', arch: 'loongarch64', nproc: 0, imagesText: '', note: '' })

function showAdd() {
  isEdit.value = false
  form.value = { id: 0, name: '', docker_host: '', arch: 'loongarch64', nproc: 0, imagesText: '', note: '' }
  dlgVisible.value = true
}
function showEdit(row: any) {
  isEdit.value = true
  form.value = {
    id: row.id, name: row.name, docker_host: row.docker_host, arch: row.arch,
    nproc: row.nproc || 0, imagesText: (row.images || []).join('\n'), note: row.note || '',
  }
  dlgVisible.value = true
}

async function save() {
  if (!form.value.name.trim() || !form.value.docker_host.trim()) {
    ElMessage.warning('名称与地址必填'); return
  }
  saving.value = true
  try {
    const payload: any = {
      name: form.value.name.trim(),
      docker_host: form.value.docker_host.trim(),
      arch: form.value.arch,
      nproc: form.value.nproc || 0,
      images: form.value.imagesText.split('\n').map((x) => x.trim()).filter(Boolean),
      note: form.value.note.trim(),
    }
    if (isEdit.value) {
      delete payload.docker_host   // 编辑不换址：地址变了要摘除重登（审计清晰）
      await api.put(`/admin/executors/${form.value.id}`, payload)
      ElMessage.success('已保存')
    } else {
      await api.post('/admin/executors', payload)
      ElMessage.success('已登记（拨号通过）')
    }
    dlgVisible.value = false
    load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally { saving.value = false }
}

async function doProbe(row: any) {
  probing.value = row.id
  try {
    const r: any = await api.post(`/admin/executors/${row.id}/probe`)
    const d = (r?.data || r || {})
    if (d.checks?.ok) ElMessage.success(`${row.name} 自检通过（${d.checks.version}${d.checks.disk_free_gb ? '，盘余 ' + d.checks.disk_free_gb + 'G' : ''}）`)
    else ElMessage.warning(`${row.name} 自检失败：${d.checks?.reason || '未知'}`)
    load()
  } catch (e: any) {
    ElMessage.error(e?.message || '自检请求失败')
  } finally { probing.value = 0 }
}

async function setStatus(row: any, st: string) {
  try {
    await api.put(`/admin/executors/${row.id}`, { status: st })
    ElMessage.success(st === 'online' ? '已回池' : '已挂起（1h 或自检通过自动回）')
    load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '操作失败') }
}

async function doDelete(row: any) {
  try {
    await ElMessageBox.confirm(
      `移除 ${row.name}？有历史任务时=软摘除（登记保留供审计），零任务=直接删除。在跑任务不会被本操作停止。`,
      '确认', { type: 'warning' })
  } catch { return }
  try {
    await api.delete(`/admin/executors/${row.id}`)
    ElMessage.success('已完成')
    load()
  } catch (e: any) { ElMessage.error(e?.response?.data?.detail || '删除失败') }
}

onMounted(load)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px; max-width: 900px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }
.toolbar { display: flex; gap: 10px; margin-bottom: 14px; align-items: center; flex-wrap: wrap }
.form-hint { font-size: 11px; color: var(--text3); margin-top: 2px; line-height: 1.5 }
.host-code {
  background: var(--border); padding: 2px 6px; border-radius: 3px;
  font-family: "JetBrains Mono", monospace; font-size: 12px; color: var(--text2);
}
.empty-tip { padding: 24px 0; text-align: center; color: var(--text3); font-size: 13px }
</style>
