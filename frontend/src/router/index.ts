import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/auth/Login.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/',
      redirect: '/workbench'
    },
    // 用户前台路由
    {
      path: '/workbench',
      name: 'workbench',
      component: () => import('@/views/user/Workbench.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('@/views/user/Tasks.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/tickets',
      name: 'tickets',
      component: () => import('@/views/user/Tickets.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/contributions',
      name: 'contributions',
      component: () => import('@/views/user/Contributions.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/download',
      name: 'download',
      component: () => import('@/views/user/Download.vue'),
      meta: { requiresAuth: true }
    },
    // 管理员后台路由
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('@/views/admin/Dashboard.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/tickets',
      name: 'tickets-admin',
      component: () => import('@/views/admin/TicketsAdmin.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/review',
      name: 'review',
      component: () => import('@/views/admin/Review.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/skills',
      name: 'skilllib',
      component: () => import('@/views/admin/SkillLib.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/systems',
      name: 'systems-admin',
      component: () => import('@/views/admin/Systems.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/qa',
      name: 'qa-console',
      component: () => import('@/views/admin/QaConsole.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/audit-logs',
      name: 'audit-logs',
      component: () => import('@/views/admin/AuditLogs.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },
    {
      path: '/admin/users',
      name: 'users',
      component: () => import('@/views/admin/Users.vue'),
      meta: { requiresAuth: true, role: 'admin' }
    },

  ]
})

// 路由守卫：检查认证与角色权限
router.beforeEach((to, _from, next) => {
  const authStore = useAuthStore()
  const requiresAuth = to.meta.requiresAuth as boolean
  const requiredRole = to.meta.role as string
  
  // 不需要登录的页面，直接放行
  if (!requiresAuth) {
    // 已登录用户访问登录页，跳转到首页
    if (to.path === '/login' && authStore.isLoggedIn) {
      return next('/workbench')
    }
    return next()
  }
  
  // 需要登录的页面
  if (!authStore.isLoggedIn) {
    return next('/login')
  }
  
  // 检查角色权限
  if (requiredRole && authStore.user?.role !== requiredRole) {
    // 权限不足 → 回用户前台首页
    // （原逻辑 requiredRole==='admin' 时跳 /dashboard 本身就是 admin 路由，会死循环）
    return next('/workbench')
  }
  
  next()
})

export default router
