<template>
  <el-container v-if="authStore.isLoggedIn">
    <el-header height="56px" class="topbar">
      <div class="logo">龙</div>
      <div class="brand">
        龙芯智能编译平台
        <small>LoongArch Smart Compile Platform</small>
      </div>
      <div class="role-switch">
        <button 
          :class="{ active: role === 'user' }" 
          @click="setRole('user')"
        >用户前台</button>
        <button 
          :class="{ active: role === 'admin' }" 
          @click="setRole('admin')"
        >管理员后台</button>
      </div>
      <div class="spacer"></div>
      <el-input placeholder="搜索软件 / 工单 / 经验…" />
      <el-badge :value="3">
        <el-button circle>🔔</el-button>
      </el-badge>
      <el-dropdown @command="handleCommand">
        <el-avatar>{{ authStore.user?.username?.charAt(0) || 'U' }}</el-avatar>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="profile">个人中心</el-dropdown-item>
            <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </el-header>

    <el-container>
      <el-aside width="210px" class="sidebar">
        <div class="sidebar-group">
          {{ role === 'user' ? '用户前台' : '管理员后台' }}
        </div>
        <div 
          v-for="item in menuItems" 
          :key="item.id"
          :class="['nav-item', { active: currentRoute === item.id }]"
          @click="navigateTo(item.id)"
        >
          <span class="ico">{{ item.ico }}</span>
          {{ item.label }}
        </div>
      </el-aside>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
  
  <!-- 未登录时显示登录页 -->
  <router-view v-else />
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ElMessage } from 'element-plus'

const router = useRouter()
const authStore = useAuthStore()
const role = ref<'user' | 'admin'>(authStore.isAdmin ? 'admin' : 'user')

const menuItems = computed(() => {
  if (role.value === 'user') {
    return [
      { id: 'workbench', ico: '🚀', label: '在线编译工作台' },
      { id: 'tasks', ico: '📋', label: '编译任务管理' },
      { id: 'tickets', ico: '🐛', label: '问题上报中心' },
      { id: 'contributions', ico: '💡', label: '经验贡献中心' },
      { id: 'download', ico: '⬇️', label: '客户端下载入口' },
    ]
  } else {
    return [
      { id: 'dashboard', ico: '📊', label: '统计看板' },
      { id: 'tickets-admin', ico: '🎫', label: '报错工单管理' },
      { id: 'review', ico: '✅', label: 'Skill 经验审核' },
      { id: 'skilllib', ico: '📚', label: '经验库管理' },
      { id: 'users', ico: '👥', label: '用户权限管理' },
      { id: 'chat-admin', ico: '💬', label: '对话式答疑' },
    ]
  }
})

const currentRoute = computed(() => router.currentRoute.value.name as string)

function setRole(r: 'user' | 'admin') {
  role.value = r
}

function navigateTo(page: string) {
  router.push({ name: page })
}

function handleCommand(command: string) {
  if (command === 'logout') {
    authStore.logout()
    ElMessage.success('已退出登录')
    router.push('/login')
  } else if (command === 'profile') {
    ElMessage.info('个人中心功能开发中')
  }
}

onMounted(async () => {
  // 只有已登录用户才验证Token有效性
  if (authStore.token) {
    await authStore.fetchMe()
  }
})
</script>

<style>
:root {
  --primary: #3370ff;
  --primary-light: #e8f0ff;
  --success: #18b26b;
  --success-light: #e3f9f0;
  --warning: #f59e0b;
  --warning-light: #fef4e3;
  --danger: #ef4444;
  --danger-light: #fde8e8;
  --info: #6b7280;
  --bg: #f5f7fa;
  --card: #fff;
  --border: #e4e7ed;
  --text: #303133;
  --text2: #606266;
  --text3: #909399;
  --sidebar: #1f2d3d;
  --sidebar-active: #3370ff;
  --header-h: 56px;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--text);
}

.topbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: var(--header-h);
  background: #fff;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 20px;
  z-index: 100;
  gap: 14px;
}

.logo {
  width: 34px;
  height: 34px;
  border-radius: 8px;
  background: linear-gradient(135deg, #3370ff, #5b8cff);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 700;
  font-size: 15px;
}

.brand {
  font-weight: 600;
  font-size: 16px;
  white-space: nowrap;
}

.brand small {
  display: block;
  font-size: 11px;
  color: var(--text3);
  font-weight: 400;
}

.role-switch {
  margin-left: 8px;
  display: flex;
  background: var(--bg);
  border-radius: 8px;
  padding: 3px;
  gap: 2px;
}

.role-switch button {
  border: none;
  background: transparent;
  padding: 6px 14px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text2);
}

.role-switch button.active {
  background: var(--primary);
  color: #fff;
}

.spacer { flex: 1; }

.sidebar {
  position: fixed;
  top: var(--header-h);
  left: 0;
  bottom: 0;
  width: 210px;
  background: var(--sidebar);
  padding: 14px 0;
  overflow-y: auto;
  z-index: 90;
}

.sidebar-group {
  padding: 0 16px;
  font-size: 11px;
  color: #8ea0b5;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 12px 0 6px;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  color: #c3ccd8;
  cursor: pointer;
  border-left: 3px solid transparent;
  font-size: 13.5px;
}

.nav-item:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #fff;
}

.nav-item.active {
  background: rgba(51, 112, 255, 0.18);
  color: #fff;
  border-left-color: var(--primary);
}

.nav-item .ico { width: 18px; text-align: center; }

.main {
  margin-left: 210px;
  margin-top: var(--header-h);
  padding: 20px 24px;
  min-height: calc(100vh - var(--header-h));
}
</style>
