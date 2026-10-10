<template>
  <div class="page">
    <div class="page-title">用户权限管理</div>
    <div class="page-sub">普通用户 / 管理员权限区分 · 操作日志审计</div>

    <div class="grid g2">
      <!-- 用户列表 -->
      <div class="card">
        <h3>👥 用户列表</h3>
        <el-table :data="users" stripe style="width: 100%">
          <el-table-column prop="username" label="用户名" />
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
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button size="small" @click="editUser(row)">编辑</el-button>
              <el-button 
                v-if="row.is_active" 
                size="small" 
                type="danger" 
                @click="toggleUser(row, false)"
              >禁用</el-button>
              <el-button 
                v-else 
                size="small" 
                type="success" 
                @click="toggleUser(row, true)"
              >启用</el-button>
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/utils/request'
import { ElMessage } from 'element-plus'

const users = ref<any[]>([])
const auditLogs = ref<any[]>([])

function formatDate(dateStr: string) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

async function loadUsers() {
  const res = await api.get('/admin/users')
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

function editUser(user: any) {
  ElMessage.info(`编辑用户 ${user.username}`)
}

async function toggleUser(user: any, isActive: boolean) {
  await api.put(`/admin/users/${user.user_id}`, { is_active: isActive })
  ElMessage.success(isActive ? '用户已启用' : '用户已禁用')
  loadUsers()
}

onMounted(() => {
  loadUsers()
  loadAuditLogs()
})
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g2 { grid-template-columns: 1fr } }
</style>
