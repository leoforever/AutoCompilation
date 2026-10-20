<template>
  <div class="page">
    <div class="page-title">Skill 经验审核</div>
    <div class="page-sub">审核用户提交的新编译经验 · 合规可用则入库，同步至云端库和所有客户端</div>

    <div class="grid g2">
      <!-- 待审核队列 -->
      <div class="card">
        <h3>
          📋 待审核队列
          <el-badge :value="pendingCount" class="badge-count" />
        </h3>
        <el-table :data="pendingExperiences" stripe style="width: 100%">
          <el-table-column prop="experience_id" label="ID" width="80" />
          <el-table-column label="软件" min-width="140">
            <template #default="{ row }">
              {{ row.skill_name }}
              <el-tag v-if="row.ticket_id" type="info" size="small" effect="plain" style="margin-left:6px">工单#{{ row.ticket_id }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag :type="row.skill_type === 'generic' ? 'primary' : 'warning'" size="small">
                {{ row.skill_type === 'generic' ? '通用' : '专用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="submitted_at" label="时间" width="140" />
          <el-table-column label="操作" width="220">
            <template #default="{ row }">
              <el-button size="small" type="success" @click="approveSkill(row)">通过</el-button>
              <el-button size="small" type="danger" @click="rejectSkill(row)">驳回</el-button>
              <el-button size="small" @click="previewSkill(row)">预览</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- SKILL.md 预览 -->
      <div class="card" v-if="currentSkill">
        <h3>📄 SKILL.md 预览</h3>
        <pre class="skill-content">{{ currentSkill.skill_content }}</pre>
        <div style="margin-top:12px;display:flex;gap:10px">
          <el-button type="success" @click="approveSkill(currentSkill)">✅ 通过并入库</el-button>
          <el-button type="danger" @click="rejectSkill(currentSkill)">❌ 驳回</el-button>
          <el-button @click="currentSkill = null">关闭</el-button>
        </div>
      </div>
      <div class="card" v-else style="display:flex;align-items:center;justify-content:center;min-height:200px">
        <span class="muted">选择左侧经验进行预览</span>
      </div>
    </div>

    <!-- 驳回原因输入 -->
    <el-dialog v-model="rejectVisible" title="驳回经验" width="400px">
      <el-input v-model="rejectReason" type="textarea" :rows="3" placeholder="请输入驳回原因（可选）" />
      <template #footer>
        <el-button @click="rejectVisible = false">取消</el-button>
        <el-button type="danger" @click="confirmReject">确认驳回</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import api from '@/utils/request'
import { ElMessage } from 'element-plus'

const pendingExperiences = ref<any[]>([])
const currentSkill = ref<any>(null)
const rejectVisible = ref(false)
const rejectReason = ref('')
const rejectingExp = ref<any>(null)

const pendingCount = computed(() => pendingExperiences.value.length)

async function loadPending() {
  const res = await api.get('/experience/pending')
  pendingExperiences.value = res.data || []
}

function previewSkill(exp: any) {
  currentSkill.value = exp
}

function approveSkill(exp: any) {
  api.post(`/experience/${exp.experience_id}/approve`)
    .then(() => {
      ElMessage.success('经验已通过审核')
      pendingExperiences.value = pendingExperiences.value.filter(e => e.experience_id !== exp.experience_id)
      currentSkill.value = null
    })
}

function rejectSkill(exp: any) {
  rejectingExp.value = exp
  rejectReason.value = ''
  rejectVisible.value = true
}

async function confirmReject() {
  if (!rejectingExp.value) return
  
  await api.post(`/experience/${rejectingExp.value.experience_id}/reject`, {
    reason: rejectReason.value || undefined
  })
  
  ElMessage.success('经验已驳回')
  pendingExperiences.value = pendingExperiences.value.filter(e => e.experience_id !== rejectingExp.value.experience_id)
  currentSkill.value = null
  rejectVisible.value = false
}

onMounted(loadPending)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 8px }
.grid { display: grid; gap: 16px }
.g2 { grid-template-columns: 1fr 1fr }
@media (max-width: 1100px) { .g2 { grid-template-columns: 1fr } }
.skill-content { background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 12.5px; overflow: auto; max-height: 400px; line-height: 1.7; white-space: pre-wrap }
.muted { color: var(--text3); font-size: 12px }
.badge-count { margin-left: 8px }
</style>
