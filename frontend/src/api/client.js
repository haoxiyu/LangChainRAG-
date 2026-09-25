import axios from 'axios'
import { ElMessage } from 'element-plus'

const client = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

// 请求拦截:自动带上 token
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('rag_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截:统一错误提示;401 时清理登录态并跳回登录页
client.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail

    if (status === 401) {
      localStorage.removeItem('rag_token')
      localStorage.removeItem('rag_user')
      // 已经在登录页就不再跳,避免循环
      if (!window.location.hash.includes('/login')) {
        ElMessage.error('登录已过期,请重新登录')
        window.location.hash = '#/login'
      }
    } else if (status === 429) {
      ElMessage.warning(detail || '操作过于频繁,请稍后再试')
    } else if (detail) {
      ElMessage.error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    } else if (error.code === 'ECONNABORTED') {
      ElMessage.error('请求超时,请检查网络或稍后重试')
    } else {
      ElMessage.error('请求失败,请稍后重试')
    }
    return Promise.reject(error)
  },
)

export default client
