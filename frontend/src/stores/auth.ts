import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/utils/request'

export interface User {
  user_id: number
  username: string
  role: string
  is_active: boolean
  last_active?: string
}

export interface LoginResult {
  access_token: string
  token_type: string
  expires_in: number
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem('token') || '')
  const user = ref<User | null>(null)

  // 从 localStorage 恢复登录状态
  const savedUser = localStorage.getItem('user')
  if (savedUser) {
    try {
      user.value = JSON.parse(savedUser)
    } catch {}
  }

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

  async function login(username: string, password: string) {
    const formData = new FormData()
    formData.append('username', username)
    formData.append('password', password)
    
    const res: any = await api.post('/auth/login', formData)
    
    token.value = res.data.access_token
    localStorage.setItem('token', res.data.access_token)
    // 10-23 修复：login 响应里没有 user 对象（旧代码 res.user → undefined 落 localStorage，
    // user 一直 null → isAdmin false / 路由守卫把 admin 路由弹回 workbench）。补拉 /auth/me。
    await fetchMe()
    return res
  }

  async function register(username: string, password: string, role: string = 'user') {
    const res: any = await api.post('/auth/register', { username, password, role })
    return res
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  async function fetchMe() {
    try {
      const res: any = await api.get('/auth/me')
      user.value = res.data
      localStorage.setItem('user', JSON.stringify(res.data))
    } catch {
      logout()
    }
  }

  return { token, user, isLoggedIn, isAdmin, login, register, logout, fetchMe }
})
