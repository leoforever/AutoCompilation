import axios from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截器：注入 Token
api.interceptors.request.use(
  (config) => {
    const authStore = useAuthStore()
    if (authStore.token) {
      config.headers.Authorization = `Bearer ${authStore.token}`
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器：统一错误处理
api.interceptors.response.use(
  (response) => {
    const result = response.data
    
    // 统一错误处理
    if (result.code !== 200 && result.code !== undefined) {
      ElMessage.error(result.message || '请求失败')
      return Promise.reject(new Error(result.message))
    }
    
    return result
  },
  (error) => {
    const status = error.response?.status
    const messages: Record<number, string> = {
      401: '请先登录',
      403: '无权访问',
      404: '请求资源不存在',
      500: '服务器错误',
    }
    
    if (status === 401) {
      const authStore = useAuthStore()
      authStore.logout()
      window.location.href = '/login'
    } else if (!error.response && /ECONNABORTED|ETIMEDOUT|timeout/i.test(error.code || error.message || '')) {
      ElMessage.error('请求超时：AI 生成较慢，请稍候重试')
    } else if (!error.response) {
      ElMessage.error('网络错误：无法连接服务器')
    } else {
      ElMessage.error(messages[status] || '网络错误')
    }
    return Promise.reject(error)
  }
)

export default api
