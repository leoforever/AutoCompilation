<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">用户权限管理</div>
        <div class="page-sub">普通用户 / 管理员权限区分 · 操作日志审计</div>
      </div>
      <el-button type="primary" @click="openCreate">＋ 新增用户</el-button>
    </div>

    <div class="grid g2">
      <!-- 用户列表 -->
      <!-- 卡片顶部 + tab 切换 + 新增按钮 -->
      <div class="card">
        <div class="user-head">
          <h3>👥 用户列表</h3>
          <el-radio-group v-model="userFilter" size="small" @change="loadUsers">
            <el-radio-button value="active">正常</el-radio-button>
            <el-radio-button value="deleted">已注销</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="users" stripe style="width: 100%">
            <el-table-column prop="username" label="用户名">
              <template #default="{ row }">
                {{ row.deleted_at ? (row.original_username || row.username) : row.username }}
                <el-tag v-if="row.deleted_at" type="info" size="small" style="margin-left:4px">已注销</el-tag>
              </template>
            </el-table-column>
          <el-table-column label="角色" width="90">
            <template #default="{ row }">
              <el-tag :type="row.role === 'admin' ? 'success' : 'primary'" size="small">
                {{ row.role === 'admin' ? '管理员' : '普通用户' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="last_active" label="最近活跃" width="150" />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
                {{ row.is_active ? '正常' : '已禁用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="320">
            <template #default="{ row }">
              <!-- 已注销用户：只显示恢复按钮 -->
              <el-button v-if="row.deleted_at" size="small" type="success" @click="restoreUser(row)">恢复</el-button>
              <template v-else>
                <el-button size="small" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" type="warning" plain @click="openResetPwd(row)">重置密码</el-button>
                <el-button
                  v-if="row.is_active"
                  size="small"
                  type="danger"
                  plain
                  :disabled="row.user_id === meId"
                  @click="toggleUser(row, false)"
                >禁用</el-button>
                <el-button
                  v-else
                  size="small"
                  type="success"
                  plain
                  @click="toggleUser(row, true)"
                >启用</el-button>
                <!-- admin 用户与本人不可删除 -->
                <el-button
                  v-if="row.role !== 'admin' && row.user_id !== meId"
                  size="small"
                  type="danger"
                  @click="removeUser(row)"
                >注销</el-button>
              </template>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 操作日志 -->
      <div class="card">
        <h3>📜 操作日志审计</h3>
        <el-table :data="auditLogs" stripe style="width: 100%" size="small">
          <el-table-column prop="created_at" label="时间" width="150" />
          <el-table-column prop="username" label="用户" width="100" />
          <el-table-column prop="action" label="操作" width="120" />
          <el-table-column prop="target" label="对象" />
        </el-table>
      </div>
    </div>

    <!-- 新增用户 -->
    <el-dialog v-model="createVisible" title="＋ 新增用户" width="420px">
      <el-form label-width="70px" @submit.prevent="doCreate">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" placeholder="3-50 字符" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="createForm.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="createForm.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving"
          :disabled="!createForm.username || createForm.password.length < 6" @click="doCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑用户（角色 + 状态） -->
    <el-dialog v-model="editVisible" :title="`✎ 编辑用户 — ${editTarget?.username || ''}`" width="420px">
      <el-form label-width="70px">
        <el-form-item label="角色">
          <el-radio-group v-model="editForm.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="editForm.is_active" active-text="启用" inactive-text="禁用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="doEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetPwdVisible" :title="`🔑 重置密码 — ${resetPwdTarget?.username || ''}`" width="420px">
      <el-form @submit.prevent="doResetPwd">
        <el-form-item label="新密码">
          <el-input v-model="newPassword" type="password" show-password
            placeholder="至少 6 位" minlength="6" />
        </el-form-item>
        <div class="reset-hint">该用户当前密码将立即失效，用新密码登录。</div>
      </el-form>
      <template #footer>
        <el-button @click="resetPwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="resetting" :disabled="newPassword.length < 6" @click="doResetPwd">
          确认重置
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '@/utils/request'
import { fmtDateTime } from '@/utils/datetime'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const meId = computed(() => auth.user?.user_id ?? -1)

const users = ref<any[]>([])
const auditLogs = ref<any[]>([])
const userFilter = ref<'active' | 'deleted'>('active')
const saving = ref(false)

const createVisible = ref(false)
const createForm = ref({ username: '', password: '', role: 'user' })

const editVisible = ref(false)
const editTarget = ref<any>(null)
const editForm = ref({ role: 'user', is_active: true })

const resetPwdVisible = ref(false)
const resetPwdTarget = ref<any>(null)
const newPassword = ref('')
const resetting = ref(false)

function formatDate(dateStr: string) {
  if (!dateStr) return '-'
  return fmtDateTime(dateStr, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

async function loadUsers() {
  const include = userFilter.value === 'deleted'
  const res = await api.get('/admin/users', { params: { include_deleted: include } })
  users.value = (res.data || []).map((u: any) => ({
    ...u,
    last_active: formatDate(u.last_active)
  }))
}

async function loadAuditLogs() {
  // TODO: 实现审计日志接口
  auditLogs.value = [
    { created_at: '10:26', username: 'admin', action: '提交经验', target: '#E-209 ollama v0.9.7' },
    { created_at: '10:25', username: 'admin', action: '发起编译', target: '#1023 ollama' },
    { created_at: '09:48', username: 'zhangwei', action: '提交经验', target: '#E-205 python v3.12' },
    { created_at: '09:30', username: 'liming', action: '上报工单', target: '#TK-20260920-011' },
    { created_at: '09:15', username: 'admin', action: '审核通过', target: '#E-205 python v3.12' },
  ]
}

function openCreate() {
  createForm.value = { username: '', password: '', role: 'user' }
  createVisible.value = true
}

async function doCreate() {
  saving.value = true
  try {
    const res: any = await api.post('/admin/users', createForm.value)
    ElMessage.success(res?.message || '用户已创建')
    createVisible.value = false
    loadUsers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    saving.value = false
  }
}

function openEdit(user: any) {
  editTarget.value = user
  editForm.value = { role: user.role, is_active: user.is_active }
  editVisible.value = true
}

async function doEdit() {
  if (!editTarget.value) return
  saving.value = true
  try {
    const res: any = await api.put(`/admin/users/${editTarget.value.user_id}`, editForm.value)
    ElMessage.success(res?.message || '用户已更新')
    editVisible.value = false
    loadUsers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

function openResetPwd(user: any) {
  resetPwdTarget.value = user
  newPassword.value = ''
  resetPwdVisible.value = true
}

async function doResetPwd() {
  if (!resetPwdTarget.value || newPassword.value.length < 6) return
  resetting.value = true
  try {
    const res: any = await api.put(
      `/admin/users/${resetPwdTarget.value.user_id}/password`,
      { new_password: newPassword.value }
    )
    ElMessage.success(res?.message || `用户 ${resetPwdTarget.value.username} 密码已重置`)
    resetPwdVisible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重置失败')
  } finally {
    resetting.value = false
  }
}

async function toggleUser(user: any, isActive: boolean) {
  try {
    await api.put(`/admin/users/${user.user_id}`, { is_active: isActive })
    ElMessage.success(isActive ? '用户已启用' : '用户已禁用')
    loadUsers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function removeUser(user: any) {
  try {
    await ElMessageBox.confirm(
      `注销用户 ${user.username} 后将停用账号，TA 的所有任务/工单/经验/审计记录都会保留（统计照常可用），并可在"已注销"列表中恢复。\n\n如需彻底匿名化（GDPR 等场景），请在平台管理中处理。`,
      '注销用户',
      { type: 'warning', confirmButtonText: '确认注销', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' }
    )
  } catch { return }  // 用户取消
  try {
    const res: any = await api.delete(`/admin/users/${user.user_id}`)
    ElMessage.success(res?.message || '用户已注销')
    userFilter.value = 'active'  // 当前操作的人回到"正常"列表（被注销的不在里面）
    loadUsers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '注销失败')
  }
}

async function restoreUser(user: any) {
  try {
    await ElMessageBox.confirm(
      `恢复用户 ${user.original_username || user.username} 后将重新激活账号，密码需走"重置密码"流程。\n\n若原用户名已被他人占用，恢复会失败，请先在用户列表中修改占用方用户名。`,
      '恢复用户',
      { type: 'info', confirmButtonText: '确认恢复', cancelButtonText: '取消' }
    )
  } catch { return }
  try {
    const res: any = await api.post(`/admin/users/${user.user_id}/restore`)
    ElMessage.success(res?.message || '用户已恢复')
    userFilter.value = 'deleted'  // 留在"已注销" tab 看到状态变化
    loadUsers()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '恢复失败')
  }
}

onMounted(() => {
  loadUsers()
  loadAuditLogs()
})
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px }
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g2 { grid-template-columns: 1fr } }
.reset-hint { color: var(--text3); font-size: 12px; margin-top: 4px }
.user-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px }
.user-head h3 { margin: 0 }
</style>
