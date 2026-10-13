<template>
  <div class="page">
    <div class="page-title">经验贡献中心</div>
    <div class="page-sub">成功编译后提交经验 · 查看审核状态 · 个人贡献记录</div>

    <!-- 统计卡片 -->
    <div class="grid g4" style="margin-bottom:16px">
      <div class="stat">
        <div class="num" style="color:var(--primary)">{{ stats.total }}</div>
        <div class="lbl">累计贡献</div>
      </div>
      <div class="stat">
        <div class="num" style="color:var(--success)">{{ stats.approved }}</div>
        <div class="lbl">已通过</div>
      </div>
      <div class="stat">
        <div class="num" style="color:var(--warning)">{{ stats.reviewing }}</div>
        <div class="lbl">审核中</div>
      </div>
      <div class="stat">
        <div class="num" style="color:var(--danger)">{{ stats.rejected }}</div>
        <div class="lbl">已驳回</div>
      </div>
    </div>

    <div class="card">
      <h3>📝 我的提交记录</h3>
      <el-table :data="experiences" stripe style="width: 100%">
        <el-table-column prop="experience_id" label="ID" width="80" />
        <el-table-column prop="skill_name" label="软件" />
        <el-table-column prop="system_name" label="系统" width="140" />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag :type="row.skill_type === 'generic' ? 'primary' : 'warning'" size="small">
              {{ row.skill_type === 'generic' ? '通用' : '专用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="submitted_at" label="提交时间" width="160">
          <template #default="{ row }">{{ formatDate(row.submitted_at) }}</template>
        </el-table-column>
        <el-table-column label="审核状态" width="100">
          <template #default="{ row }">
            <el-tag :type="expStatusType(row.status)" size="small">{{ expStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button size="small" @click="previewSkill(row)">预览 SKILL.md</el-button>
            <el-button v-if="row.status === 'rejected'" size="small" @click="showReason(row)">查看原因</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- SKILL.md 预览弹窗 -->
    <el-dialog v-model="skillPreviewVisible" title="📄 SKILL.md 预览" width="700px">
      <pre class="skill-content">{{ currentSkill?.skill_content }}</pre>
      <template #footer>
        <el-button @click="skillPreviewVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 驳回原因弹窗 -->
    <el-dialog v-model="reasonVisible" title="驳回原因" width="400px">
      <p class="reason-text">{{ currentExp?.rejection_reason || '未填写原因' }}</p>
      <template #footer>
        <el-button type="primary" @click="reasonVisible = false">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import api from '@/utils/request'

const experiences = ref<any[]>([])
const stats = ref({ total: 0, approved: 0, reviewing: 0, rejected: 0 })
const skillPreviewVisible = ref(false)
const reasonVisible = ref(false)
const currentSkill = ref<any>(null)
const currentExp = ref<any>(null)

async function loadExperiences() {
  const res = await api.get('/experience/my')
  experiences.value = res.data || []
  
  // 统计（superseded 不单列卡片，列表中以"已取代"标签呈现）
  stats.value = {
    total: experiences.value.length,
    approved: experiences.value.filter(e => e.status === 'approved').length,
    reviewing: experiences.value.filter(e => e.status === 'submitted' || e.status === 'under_review').length,
    rejected: experiences.value.filter(e => e.status === 'rejected').length,
  }
}

function expStatusType(status: string) {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    approved: 'success',
    rejected: 'danger',
    submitted: 'info',
    under_review: 'warning',
    synced: 'success',
    superseded: 'info',
  }
  return map[status] || 'info'
}

function expStatusText(status: string) {
  const map: Record<string, string> = {
    approved: '已通过',
    rejected: '已驳回',
    submitted: '待审核',
    under_review: '审核中',
    synced: '已同步',
    superseded: '已取代',
  }
  return map[status] || status
}

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function previewSkill(exp: any) {
  currentSkill.value = exp
  skillPreviewVisible.value = true
}

function showReason(exp: any) {
  currentExp.value = exp
  reasonVisible.value = true
}

onMounted(loadExperiences)
</script>

<style scoped>
.page-title { font-size: 18px; font-weight: 600; margin-bottom: 4px }
.page-sub { color: var(--text3); font-size: 12.5px; margin-bottom: 18px }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 16px }
.card h3 { font-size: 14.5px; font-weight: 600; margin-bottom: 12px }
.grid { display: grid; gap: 16px }
.g4 { grid-template-columns: repeat(4, 1fr) }
@media (max-width: 900px) { .g4 { grid-template-columns: repeat(2, 1fr) } }
.stat { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 16px }
.stat .num { font-size: 26px; font-weight: 700; margin: 2px 0 }
.stat .lbl { font-size: 12.5px; color: var(--text3) }
.skill-content { background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-size: 12.5px; overflow: auto; max-height: 500px; line-height: 1.7; white-space: pre-wrap }
.reason-text { font-size: 14px; line-height: 1.8; color: var(--text) }
</style>
