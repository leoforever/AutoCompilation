<template>
  <div class="page">
    <div class="page-title">经验库管理</div>
    <div class="page-sub">在线编辑、新增、禁用 通用/专用 Skill；支持从磁盘导入与重新同步</div>

    <!-- 统计卡片 -->
    <div class="stats">
      <div class="stat-card">
        <div class="stat-num">{{ stats.total }}</div>
        <div class="stat-label">总 Skill</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{{ stats.generic }}</div>
        <div class="stat-label">通用</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{{ stats.specific }}</div>
        <div class="stat-label">专用</div>
      </div>
      <div class="stat-card" :class="{ 'stat-fail': stats.inactive > 0 }">
        <div class="stat-num">{{ stats.inactive }}</div>
        <div class="stat-label">已禁用</div>
      </div>
      <div class="stat-card" :class="{ 'stat-warn': stats.experience > 0 }">
        <div class="stat-num">{{ stats.experience }}</div>
        <div class="stat-label">来自用户经验</div>
      </div>
    </div>

    <div class="card">
      <!-- 过滤区 -->
      <div class="toolbar">
        <el-select v-model="filterType" placeholder="全部类型" style="width:120px" clearable>
          <el-option label="通用" value="generic" />
          <el-option label="专用" value="specific" />
        </el-select>
        <el-select v-model="filterActive" placeholder="全部状态" style="width:120px" clearable>
          <el-option label="启用" :value="true" />
          <el-option label="禁用" :value="false" />
        </el-select>
        <el-input
          v-model="searchText"
          placeholder="按名称/描述搜索…"
          style="width:240px"
          clearable
          @keyup.enter="loadSkills"
        />
        <el-button type="primary" @click="loadSkills">🔍 查询</el-button>
        <el-button @click="showTrashDrawer">🗑️ 回收站</el-button>
        <el-button type="primary" @click="showAddDialog">+ 新增 Skill</el-button>
      </div>

      <!-- 列表 -->
      <el-table :data="skills" stripe style="width: 100%" v-loading="loading">
        <el-table-column type="index" label="#" width="56" />
        <el-table-column label="名称 / 描述" min-width="220">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span v-if="row.description" class="desc">{{ row.description }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag :type="row.type === 'generic' ? 'primary' : 'warning'" size="small">
              {{ row.type === 'generic' ? '通用' : '专用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="适用系统" width="120">
          <template #default="{ row }">
            <span class="muted">
              {{ row.system_ids?.length ? row.system_ids.length + ' 个' : '全部' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="80" />
        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <el-tag
              :type="row.source === 'experience' ? 'success' : 'info'"
              size="small"
              effect="plain"
            >
              {{ row.source === 'experience' ? '📥 用户经验' : '🛠️ 管理员' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-switch v-model="row.is_active" @change="toggleSkill(row)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="editSkill(row)">编辑</el-button>
            <el-button size="small" link type="danger" @click="deleteSkill(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新增/编辑弹窗 -->
    <el-dialog
      v-model="skillDialogVisible"
      :title="isEdit ? '编辑 Skill' : '新增 Skill'"
      width="780px"
      @close="resetForm"
    >
      <el-form :model="skillForm" :rules="formRules" ref="formRef" label-width="100px">
        <el-form-item label="Skill 名称" prop="name">
          <el-input
            v-model="skillForm.name"
            :disabled="isEdit"
            placeholder="小写字母 + 连字符，如 go-toolchain"
          />
          <div class="form-hint" v-if="!isEdit">
            目录名只能是 a-z 0-9 和单连字符，1-64 字符
          </div>
        </el-form-item>
        <el-form-item label="类型" prop="type">
          <el-radio-group v-model="skillForm.type" :disabled="isEdit">
            <el-radio value="generic">通用（generic）</el-radio>
            <el-radio value="specific">专用（specific）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="skillForm.version" placeholder="如：v1.0.0" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="skillForm.description"
            type="textarea"
            :rows="2"
            placeholder="一句话说明这个 Skill 做什么、AI 在什么场景下会读它（关键：命中关键词）"
            show-word-limit
            :maxlength="200"
          />
        </el-form-item>
        <el-form-item label="适用系统">
          <el-select
            v-model="skillForm.system_ids"
            multiple
            collapse-tags
            collapse-tags-tooltip
            placeholder="留空表示适用全部系统"
            style="width:100%"
          >
            <el-option
              v-for="s in systems"
              :key="s.system_id"
              :label="`${s.name} (${s.arch})`"
              :value="s.system_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="SKILL.md 内容">
          <el-tabs v-model="skillMdTab" class="md-tabs">
            <el-tab-pane label="编辑" name="edit">
              <el-input
                v-model="skillForm.skill_content"
                type="textarea"
                :rows="18"
                placeholder="---&#10;name: ...&#10;description: ...&#10;allowed-tools: [execute, read_file]&#10;---&#10;&#10;# Markdown 正文"
                spellcheck="false"
                style="font-family: 'JetBrains Mono', monospace; font-size: 12.5px"
              />
            </el-tab-pane>
            <el-tab-pane label="预览" name="preview">
              <pre class="md-preview">{{ skillForm.skill_content || '（无内容）' }}</pre>
            </el-tab-pane>
            <el-tab-pane label="frontmatter" name="fm">
              <div class="fm-info">
                <div class="fm-row">
                  <span class="fm-key">name</span>
                  <span class="fm-val">{{ parseFrontmatter(skillForm.skill_content).name || '—' }}</span>
                </div>
                <div class="fm-row">
                  <span class="fm-key">description</span>
                  <span class="fm-val">{{ parseFrontmatter(skillForm.skill_content).description || '—' }}</span>
                </div>
                <div class="fm-row">
                  <span class="fm-key">type</span>
                  <span class="fm-val">{{ parseFrontmatter(skillForm.skill_content).type || '—' }}</span>
                </div>
                <div class="fm-row">
                  <span class="fm-key">allowed-tools</span>
                  <span class="fm-val">{{ parseFrontmatter(skillForm.skill_content).allowedTools || '—' }}</span>
                </div>
                <div class="fm-warn" v-if="!parseFrontmatter(skillForm.skill_content).name">
                  ⚠️ 缺少 name 字段 — DeepAgents 会忽略这条 skill
                </div>
                <div class="fm-warn" v-if="!parseFrontmatter(skillForm.skill_content).description">
                  ⚠️ 缺少 description 字段 — AI 看不到这条 skill 的用途
                </div>
              </div>
            </el-tab-pane>
          </el-tabs>
        </el-form-item>
        <el-form-item label="run.sh 脚本">
          <el-input
            v-model="skillForm.script_content"
            type="textarea"
            :rows="4"
            placeholder="可选：scripts/run.sh 内容，会随 SKILL.md 一起存到磁盘"
            spellcheck="false"
            style="font-family: 'JetBrains Mono', monospace; font-size: 12.5px"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="skillDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveSkill">保存</el-button>
      </template>
    </el-dialog>

    <!-- 从磁盘导入 Drawer 已被移除：reconcile 在进入页面时自动跑 -->

    <!-- 回收站 Drawer -->
    <el-drawer
      v-model="trashDrawerVisible"
      title="🗑️ 回收站"
      size="540px"
      direction="rtl"
    >
      <div class="drawer-hint">
        软删的 skill 保留在 DB 里，可以恢复（写回磁盘）或永久删除。
      </div>
      <div v-if="trashedSkills.length === 0" class="empty">回收站为空</div>
      <div v-else class="trash-list">
        <div v-for="t in trashedSkills" :key="t.skill_id" class="trash-item">
          <div class="trash-head">
            <el-tag :type="t.type === 'generic' ? 'primary' : 'warning'" size="small">
              {{ t.type === 'generic' ? '通用' : '专用' }}
            </el-tag>
            <span class="trash-name">{{ t.name }}</span>
            <span class="trash-time">{{ fmtDateTime(t.deleted_at) }}</span>
          </div>
          <div v-if="t.description" class="trash-desc">{{ t.description }}</div>
          <div class="trash-actions">
            <el-button
              size="small"
              type="primary"
              :loading="trashActionKey === t.skill_id + ':restore'"
              @click="restoreSkill(t)"
            >恢复</el-button>
            <el-button
              size="small"
              type="danger"
              :loading="trashActionKey === t.skill_id + ':permanent'"
              @click="permanentDelete(t)"
            >永久删除</el-button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '@/utils/request'
import { fmtDateTime } from '@/utils/datetime'  // 10-27 vue-tsc2：模板原引用的 formatDate 从未定义（潜伏 bug），改现成 fmtDateTime
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'

// ============ 状态 ============
const skills = ref<any[]>([])
const systems = ref<any[]>([])
const loading = ref(false)
const saving = ref(false)

// 回收站
const trashDrawerVisible = ref(false)
const trashedSkills = ref<any[]>([])
const trashActionKey = ref<string | null>(null)

const filterType = ref('')
const filterActive = ref<boolean | ''>('')
const searchText = ref('')

const skillDialogVisible = ref(false)
const skillMdTab = ref('edit')
const isEdit = ref(false)
const formRef = ref<FormInstance | null>(null)
const skillForm = ref({
  skill_id: null as number | null,
  name: '',
  type: 'generic' as 'generic' | 'specific',
  version: '',
  description: '',
  system_ids: [] as number[],
  skill_content: '',
  script_content: '',
})

const formRules = {
  name: [{ required: true, message: '请输入 Skill 名称', trigger: 'blur' }],
  type: [{ required: true, message: '请选择类型', trigger: 'change' }],
}

// ============ 统计 ============
const stats = computed(() => {
  const all = skills.value
  return {
    total: all.length,
    generic: all.filter(s => s.type === 'generic').length,
    specific: all.filter(s => s.type === 'specific').length,
    inactive: all.filter(s => !s.is_active).length,
    experience: all.filter(s => s.source === 'experience').length,
  }
})

// ============ 工具函数 ============
function parseFrontmatter(content: string) {
  if (!content || !content.startsWith('---\n')) return {} as any
  const end = content.indexOf('\n---', 4)
  if (end < 0) return {} as any
  const fm = content.slice(4, end)
  const out: any = {}
  for (const line of fm.split('\n')) {
    const m = line.match(/^([\w-]+):\s*(.*)$/)
    if (!m) continue
    const key = m[1]
    let val: any = m[2].trim()
    if (val.startsWith('[') && val.endsWith(']')) {
      val = val.slice(1, -1).split(',').map((s: string) => s.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean)
      if (key === 'allowed-tools') out.allowedTools = val.join(', ')
      else out[key] = val
    } else if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1)
      out[key] = val
    } else {
      out[key] = val
    }
  }
  return out
}

function resetForm() {
  skillForm.value = {
    skill_id: null,
    name: '',
    type: 'generic',
    version: '',
    description: '',
    system_ids: [],
    skill_content: '',
    script_content: '',
  }
  skillMdTab.value = 'edit'
  formRef.value?.clearValidate()
}

// ============ 加载 ============
async function loadSkills() {
  loading.value = true
  try {
    const params: any = {}
    if (filterType.value) params.skill_type = filterType.value
    if (filterActive.value !== '') params.is_active = filterActive.value
    if (searchText.value) params.search = searchText.value
    const res = await api.get('/admin/skills', { params })
    skills.value = res.data || []
  } finally {
    loading.value = false
  }
}

async function loadSystems() {
  try {
    const res = await api.get('/admin/systems')
    systems.value = res.data || []
  } catch {
    // 静默失败，编辑时系统下拉会空
  }
}

onMounted(async () => {
  loadSystems()
  // 每次进入页面自动 reconcile（磁盘 → DB 同步）
  // 不展示结果弹窗，避免噪音；只在有变更时小提示
  try {
    const res = await api.post('/admin/skills/reconcile')
    const s = res.data || {}
    if (s.disk_only || s.db_only) {
      ElMessage.success(`已同步：新增 ${s.disk_only}，清理 ${s.db_only}`)
    }
  } catch {
    // 静默失败（reconcile 是 best-effort）
  }
  await loadSkills()
})

// ============ 新增/编辑 ============
function showAddDialog() {
  isEdit.value = false
  resetForm()
  // 新建时给个空 frontmatter 模板
  skillForm.value.skill_content = `---\nname: \ndescription: \nallowed-tools: [execute, read_file]\n---\n\n# Skill 标题\n\n## 适用场景\n\n## 操作步骤\n`
  skillDialogVisible.value = true
}

async function editSkill(skill: any) {
  isEdit.value = true
  skillForm.value = {
    skill_id: skill.skill_id,
    name: skill.name,
    type: skill.type,
    version: skill.version || '',
    description: skill.description || '',
    system_ids: Array.isArray(skill.system_ids) ? [...skill.system_ids] : [],
    // 3e：列表已 defer 掉正文，编辑器从详情端点单查填充
    skill_content: '',
    script_content: '',
  }
  skillMdTab.value = 'edit'
  skillDialogVisible.value = true
  try {
    const res: any = await api.get(`/admin/skills/${skill.skill_id}`)
    const d = res.data || {}
    skillForm.value.skill_content = d.skill_content || ''
    skillForm.value.script_content = d.script_content || ''
  } catch (e: any) {
    ElMessage.error('加载 Skill 正文失败: ' + (e?.message || ''))
  }
}

async function saveSkill() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    if (isEdit.value && skillForm.value.skill_id) {
      await api.put(`/admin/skills/${skillForm.value.skill_id}`, skillForm.value)
      ElMessage.success('Skill 已更新')
    } else {
      await api.post('/admin/skills', skillForm.value)
      ElMessage.success('Skill 已创建')
    }
    skillDialogVisible.value = false
    await loadSkills()
  } catch (e: any) {
    // 拦截器已经 ElMessage.error 过
  } finally {
    saving.value = false
  }
}

async function toggleSkill(skill: any) {
  try {
    await api.put(`/admin/skills/${skill.skill_id}`, { is_active: skill.is_active })
    ElMessage.success(skill.is_active ? '已启用' : '已禁用')
  } catch {
    // 失败回滚
    skill.is_active = !skill.is_active
  }
}

async function deleteSkill(skill: any) {
  await ElMessageBox.confirm(
    `确定要删除 Skill "${skill.name}" 吗？\n删除后可从回收站恢复。`,
    '确认删除',
    { type: 'warning' },
  )
  try {
    await api.delete(`/admin/skills/${skill.skill_id}`)
    ElMessage.success('Skill 已移到回收站')
    await loadSkills()
  } catch {
    /* interceptor 报错 */
  }
}

// ============ 回收站 ============
async function showTrashDrawer() {
  trashDrawerVisible.value = true
  await loadTrash()
}

async function loadTrash() {
  try {
    const res = await api.get('/admin/skills/trash')
    trashedSkills.value = res.data || []
  } catch {
    trashedSkills.value = []
  }
}

async function restoreSkill(t: any) {
  trashActionKey.value = `${t.skill_id}:restore`
  try {
    await api.post(`/admin/skills/${t.skill_id}/restore`)
    ElMessage.success(`已恢复 ${t.name}`)
    await loadTrash()
    await loadSkills()
  } catch {
    /* interceptor */
  } finally {
    trashActionKey.value = null
  }
}

async function permanentDelete(t: any) {
  await ElMessageBox.confirm(
    `确定要永久删除 "${t.name}" 吗？\n此操作不可恢复。`,
    '⚠️ 永久删除',
    { type: 'error' },
  )
  trashActionKey.value = `${t.skill_id}:permanent`
  try {
    await api.delete(`/admin/skills/${t.skill_id}/permanent`)
    ElMessage.success(`已永久删除 ${t.name}`)
    await loadTrash()
  } catch {
    /* interceptor */
  } finally {
    trashActionKey.value = null
  }
}
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }

.toolbar { display: flex; gap: 10px; margin-bottom: 14px; align-items: center; flex-wrap: wrap }

.stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 14px }
.stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; text-align: center }
.stat-num { font-size: 24px; font-weight: 700; color: var(--text) }
.stat-label { font-size: 12px; color: var(--text3); margin-top: 2px }
.stat-warn .stat-num { color: #f59e0b }
.stat-fail .stat-num { color: #94a3b8 }

.name-cell { display: flex; flex-direction: column; gap: 2px }
.name { font-weight: 500 }
.desc { font-size: 11.5px; color: var(--text3); max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }

.form-hint { font-size: 11px; color: var(--text3); margin-top: 2px }

.md-tabs { width: 100% }
.md-preview {
  background: #0f172a; color: #cbd5e1;
  border-radius: 6px; padding: 12px 14px;
  font-family: "JetBrains Mono", monospace; font-size: 12.5px;
  max-height: 360px; overflow: auto; line-height: 1.7;
  white-space: pre-wrap; word-break: break-all;
}

.fm-info { display: flex; flex-direction: column; gap: 8px; padding: 8px 4px }
.fm-row { display: grid; grid-template-columns: 100px 1fr; gap: 8px; align-items: start; font-size: 12.5px }
.fm-key { color: #64748b; font-family: "JetBrains Mono", monospace }
.fm-val { color: #cbd5e1; word-break: break-all }
.fm-warn { color: #f87171; font-size: 12px; margin-top: 4px; padding: 4px 8px; background: rgba(248,113,113,0.08); border-radius: 4px }

.drawer-hint { font-size: 12.5px; color: var(--text3); margin-bottom: 14px }
.drawer-hint code { background: var(--border); padding: 1px 6px; border-radius: 3px; font-size: 11.5px }
.empty { padding: 40px 0; text-align: center; color: var(--text3); font-size: 13px }
.discover-list { display: flex; flex-direction: column; gap: 10px }
.discover-item { border: 1px solid var(--border); border-radius: 8px; padding: 12px }
.discover-head { display: flex; align-items: center; gap: 10px; margin-bottom: 4px }
.discover-name { font-weight: 500; flex: 1 }
.discover-path { font-size: 11.5px; color: var(--text3); font-family: "JetBrains Mono", monospace; margin-bottom: 4px }
.discover-desc { font-size: 12px; color: var(--text2); line-height: 1.5 }

.trash-list { display: flex; flex-direction: column; gap: 10px }
.trash-item { border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: rgba(245, 158, 11, 0.04) }
.trash-head { display: flex; align-items: center; gap: 10px; margin-bottom: 4px }
.trash-name { font-weight: 500; flex: 1 }
.trash-time { font-size: 11.5px; color: var(--text3); font-family: "JetBrains Mono", monospace }
.trash-desc { font-size: 12px; color: var(--text2); line-height: 1.5; margin: 4px 0 }
.trash-actions { display: flex; gap: 8px; margin-top: 8px }
</style>
