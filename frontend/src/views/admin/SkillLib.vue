<template>
  <div class="page">
    <div class="page-title">经验库管理</div>
    <div class="page-sub">在线编辑、新增、禁用、分类管理 OpenClaw 通用/专用 Skill</div>

    <div class="card">
      <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center;flex-wrap:wrap">
        <el-select v-model="filterType" placeholder="全部类型" style="width:120px">
          <el-option label="全部类型" value="" />
          <el-option label="通用" value="generic" />
          <el-option label="专用" value="specific" />
        </el-select>
        <el-select v-model="filterActive" placeholder="全部状态" style="width:120px">
          <el-option label="全部状态" value="" />
          <el-option label="启用" :value="true" />
          <el-option label="禁用" :value="false" />
        </el-select>
        <el-input v-model="searchText" placeholder="搜索 Skill 名称…" style="width:200px" />
        <el-button @click="loadSkills">🔍 查询</el-button>
        <el-button type="primary" @click="showAddDialog">+ 新增 Skill</el-button>
      </div>

      <el-table :data="skills" stripe style="width: 100%">
        <el-table-column prop="skill_id" label="ID" width="80" />
        <el-table-column prop="name" label="Skill 名称" />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag :type="row.type === 'generic' ? 'primary' : 'warning'" size="small">
              {{ row.type === 'generic' ? '通用' : '专用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="适用系统" width="140">
          <template #default="{ row }">
            <span class="muted">{{ row.system_ids?.length || 0 }} 个系统</span>
          </template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="80" />
        <el-table-column label="同步状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.sync_status === 'synced' ? 'success' : 'info'" size="small">
              {{ row.sync_status === 'synced' ? '已同步' : '待同步' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-switch 
              v-model="row.is_active" 
              @change="toggleSkill(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button size="small" @click="editSkill(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="deleteSkill(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新增/编辑弹窗 -->
    <el-dialog v-model="skillDialogVisible" :title="isEdit ? '编辑 Skill' : '新增 Skill'" width="600px">
      <el-form :model="skillForm" label-width="100px">
        <el-form-item label="Skill 名称">
          <el-input v-model="skillForm.name" />
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="skillForm.type">
            <el-radio value="generic">通用</el-radio>
            <el-radio value="specific">专用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="skillForm.version" placeholder="如：v1.0.0" />
        </el-form-item>
        <el-form-item label="SKILL.md 内容">
          <el-input v-model="skillForm.skill_content" type="textarea" :rows="10" placeholder="请输入 SKILL.md 内容..." />
        </el-form-item>
        <el-form-item label="执行脚本">
          <el-input v-model="skillForm.script_content" type="textarea" :rows="5" placeholder="请输入 run.sh 脚本内容（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="skillDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveSkill">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/utils/request'
import { ElMessage, ElMessageBox } from 'element-plus'

const skills = ref<any[]>([])
const filterType = ref('')
const filterActive = ref('')
const searchText = ref('')

const skillDialogVisible = ref(false)
const isEdit = ref(false)
const skillForm = ref({
  skill_id: null as number | null,
  name: '',
  type: 'generic' as 'generic' | 'specific',
  version: '',
  skill_content: '',
  script_content: '',
})

async function loadSkills() {
  const params: any = {}
  if (filterType.value) params.skill_type = filterType.value
  if (filterActive.value !== '') params.is_active = filterActive.value
  if (searchText.value) params.search = searchText.value
  
  const res = await api.get('/admin/skills', { params })
  skills.value = res.data || []
}

function showAddDialog() {
  isEdit.value = false
  skillForm.value = { skill_id: null, name: '', type: 'generic', version: '', skill_content: '', script_content: '' }
  skillDialogVisible.value = true
}

function editSkill(skill: any) {
  isEdit.value = true
  skillForm.value = { ...skill }
  skillDialogVisible.value = true
}

async function saveSkill() {
  if (!skillForm.value.name) {
    ElMessage.warning('请输入 Skill 名称')
    return
  }
  
  if (isEdit.value && skillForm.value.skill_id) {
    await api.put(`/admin/skills/${skillForm.value.skill_id}`, skillForm.value)
    ElMessage.success('Skill 已更新')
  } else {
    await api.post('/admin/skills', skillForm.value)
    ElMessage.success('Skill 已创建')
  }
  
  skillDialogVisible.value = false
  loadSkills()
}

async function toggleSkill(skill: any) {
  await api.put(`/admin/skills/${skill.skill_id}`, { is_active: skill.is_active })
  ElMessage.success(skill.is_active ? 'Skill 已启用' : 'Skill 已禁用')
}

async function deleteSkill(skill: any) {
  await ElMessageBox.confirm(`确定要删除 Skill "${skill.name}" 吗？`, '确认删除', { type: 'warning' })
  await api.delete(`/admin/skills/${skill.skill_id}`)
  ElMessage.success('Skill 已删除')
  loadSkills()
}

onMounted(loadSkills)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }
</style>
