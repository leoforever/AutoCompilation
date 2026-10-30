<template>
  <div class="page">
    <div class="page-title">目标系统管理</div>
    <div class="page-sub">管理编译用的目标系统（每个系统对应一个 Docker 镜像）。worker 编译时按用户选的系统从 DB 拉镜像。</div>

    <div class="card">
      <div class="toolbar">
        <el-input
          v-model="searchText"
          placeholder="按名称搜索…"
          style="width:240px"
          clearable
          @keyup.enter="loadSystems"
        />
        <el-button type="primary" @click="loadSystems">🔍 查询</el-button>
        <el-button type="primary" @click="showAddDialog">+ 新增系统</el-button>
      </div>

      <el-table :data="filteredSystems" stripe style="width: 100%" v-loading="loading">
        <el-table-column type="index" label="#" width="56" />
        <el-table-column prop="name" label="系统名称" min-width="200" />
        <el-table-column label="编译镜像" min-width="280">
          <template #default="{ row }">
            <code class="img-code">{{ row.base_image }}</code>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="editSystem(row)">编辑</el-button>
            <el-button size="small" link type="danger" @click="deleteSystem(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-tip">
            还没有目标系统。
            <a @click="showAddDialog">+ 新增一个</a>
            （worker 编译时需要至少一个启用状态的系统才能跑）
          </div>
        </template>
      </el-table>
    </div>

    <!-- 新增 / 编辑 弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑目标系统' : '新增目标系统'"
      width="540px"
      @close="resetForm"
    >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="系统名称" prop="name">
          <el-input v-model="form.name" placeholder="如：openEuler 24.03 LTS" />
          <div class="form-hint">用户提交编译时从这个下拉选，唯一</div>
        </el-form-item>
        <el-form-item label="编译镜像" prop="base_image">
          <el-input v-model="form.base_image" placeholder="如：loongnix-build:23.2" />
          <div class="form-hint">完整镜像名（含 tag）。worker 会 docker pull 这个镜像</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_active" />
          <span class="muted" style="margin-left:8px">禁用后用户提交编译时看不到这个系统</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveSystem">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '@/utils/request'
import { ElMessage, ElMessageBox, type FormInstance } from 'element-plus'

// ============ 状态 ============
const systems = ref<any[]>([])
const loading = ref(false)
const saving = ref(false)
const searchText = ref('')

const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref<FormInstance | null>(null)
const form = ref({
  system_id: null as number | null,
  name: '',
  base_image: '',
  is_active: true,
})

const rules = {
  name: [{ required: true, message: '请输入系统名称', trigger: 'blur' }],
  base_image: [{ required: true, message: '请输入编译镜像', trigger: 'blur' }],
}

const filteredSystems = computed(() => {
  const q = searchText.value.trim().toLowerCase()
  if (!q) return systems.value
  return systems.value.filter(s => s.name.toLowerCase().includes(q))
})

// ============ 加载 ============
async function loadSystems() {
  loading.value = true
  try {
    const res = await api.get('/admin/systems')
    systems.value = res.data || []
  } finally {
    loading.value = false
  }
}

onMounted(loadSystems)

// ============ 新增 / 编辑 ============
function showAddDialog() {
  isEdit.value = false
  resetForm()
  dialogVisible.value = true
}

function editSystem(s: any) {
  isEdit.value = true
  form.value = {
    system_id: s.system_id,
    name: s.name,
    base_image: s.base_image,
    is_active: s.is_active,
  }
  dialogVisible.value = true
}

function resetForm() {
  form.value = {
    system_id: null,
    name: '',
    base_image: '',
    is_active: true,
  }
  formRef.value?.clearValidate()
}

async function saveSystem() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    if (isEdit.value && form.value.system_id) {
      await api.put(`/admin/systems/${form.value.system_id}`, {
        name: form.value.name,
        base_image: form.value.base_image,
        is_active: form.value.is_active,
      })
      ElMessage.success('系统已更新')
    } else {
      await api.post('/admin/systems', {
        name: form.value.name,
        base_image: form.value.base_image,
        is_active: form.value.is_active,
      })
      ElMessage.success('系统已创建')
    }
    dialogVisible.value = false
    await loadSystems()
  } catch {
    /* interceptor 报错 */
  } finally {
    saving.value = false
  }
}

async function deleteSystem(s: any) {
  await ElMessageBox.confirm(
    `确定要删除系统 "${s.name}" 吗？\n注意：已有编译任务引用此系统时删除会失败。`,
    '确认删除',
    { type: 'warning' },
  )
  try {
    await api.delete(`/admin/systems/${s.system_id}`)
    ElMessage.success('系统已删除')
    await loadSystems()
  } catch {
    /* interceptor */
  }
}
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.muted { color: var(--text3); font-size: 12px }
.toolbar { display: flex; gap: 10px; margin-bottom: 14px; align-items: center; flex-wrap: wrap }
.form-hint { font-size: 11px; color: var(--text3); margin-top: 2px }
.img-code {
  background: var(--border);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: "JetBrains Mono", monospace;
  font-size: 12px;
  color: var(--text2);
}
.empty-tip {
  padding: 24px 0;
  text-align: center;
  color: var(--text3);
  font-size: 13px;
}
.empty-tip a {
  color: var(--primary, #409eff);
  cursor: pointer;
  margin-left: 8px;
}
</style>
