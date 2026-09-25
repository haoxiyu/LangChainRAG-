import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi } from '@/api'

const TOKEN_KEY = 'rag_token'
const USER_KEY = 'rag_user'

function readStoredUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) || '')
  const user = ref(readStoredUser())
  const loading = ref(false)

  const isLoggedIn = computed(() => Boolean(token.value && user.value))
  // 只有管理员能看到知识库管理入口
  const isAdmin = computed(() => user.value?.role === 'admin')

  function persist(newToken, newUser) {
    token.value = newToken
    user.value = newUser
    localStorage.setItem(TOKEN_KEY, newToken)
    localStorage.setItem(USER_KEY, JSON.stringify(newUser))
  }

  async function login(username, password) {
    loading.value = true
    try {
      const data = await authApi.login({ username, password })
      persist(data.access_token, data.user)
      return data.user
    } finally {
      loading.value = false
    }
  }

  async function register(username, password) {
    return authApi.register({ username, password })
  }

  /** 用服务端数据刷新本地用户信息(权限变更后能及时反映)。 */
  async function refresh() {
    if (!token.value) return null
    const data = await authApi.me()
    user.value = data
    localStorage.setItem(USER_KEY, JSON.stringify(data))
    return data
  }

  async function changePassword(oldPassword, newPassword) {
    return authApi.changePassword({
      old_password: oldPassword,
      new_password: newPassword,
    })
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  return {
    token, user, loading,
    isLoggedIn, isAdmin,
    login, register, refresh, changePassword, logout,
  }
})
