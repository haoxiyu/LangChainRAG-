<template>
  <div class="main-layout">
    <aside class="sidebar">
      <div class="sidebar-brand">
        <el-icon :size="20"><ChatDotSquare /></el-icon>
        <span>商品知识库问答</span>
      </div>

      <nav class="sidebar-menu">
        <router-link to="/chat" class="menu-item" :class="{ active: isChat }">
          <el-icon><ChatLineRound /></el-icon>
          <span>智能问答</span>
        </router-link>
        <router-link
          to="/conversations"
          class="menu-item"
          :class="{ active: route.name === 'conversations' }"
        >
          <el-icon><Clock /></el-icon>
          <span>我的会话</span>
        </router-link>
        <router-link
          to="/profile"
          class="menu-item"
          :class="{ active: route.name === 'profile' }"
        >
          <el-icon><Setting /></el-icon>
          <span>个人设置</span>
        </router-link>

        <!-- 需求 6:仅管理员可见知识库管理入口 -->
        <template v-if="auth.isAdmin">
          <div class="menu-group-label">管理员</div>
          <router-link
            to="/admin/dashboard"
            class="menu-item"
            :class="{ active: route.name === 'dashboard' }"
          >
            <el-icon><DataAnalysis /></el-icon>
            <span>数据概览</span>
          </router-link>
          <router-link
            to="/admin/knowledge-bases"
            class="menu-item"
            :class="{ active: isKbAdmin }"
          >
            <el-icon><Collection /></el-icon>
            <span>知识库管理</span>
          </router-link>
        </template>
      </nav>

      <div class="sidebar-user">
        <div class="user-avatar">{{ avatarChar }}</div>
        <div class="user-meta">
          <div class="user-name">{{ auth.user?.username }}</div>
          <div class="user-role">{{ auth.isAdmin ? '管理员' : '普通用户' }}</div>
        </div>
        <el-tooltip content="退出登录" placement="top">
          <el-icon class="logout-icon" @click="onLogout"><SwitchButton /></el-icon>
        </el-tooltip>
      </div>
    </aside>

    <main class="content">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatDotSquare,
  ChatLineRound,
  Clock,
  Collection,
  DataAnalysis,
  Setting,
  SwitchButton,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 聊天页有多个子路由(/chat 与 /chat/:id),统一按前缀判断高亮
const isChat = computed(() => route.path.startsWith('/chat'))
const isKbAdmin = computed(() => route.path.startsWith('/admin/knowledge-bases'))
const avatarChar = computed(() => (auth.user?.username || '?').charAt(0).toUpperCase())

onMounted(async () => {
  // 进主界面时用服务端数据校准一次角色,避免本地缓存里的旧角色导致菜单显示错误
  try {
    await auth.refresh()
  } catch {
    // 401 已由 axios 拦截器统一处理(清 token 并跳登录),这里静默即可
  }
})

async function onLogout() {
  try {
    await ElMessageBox.confirm('确定要退出登录吗?', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户取消
  }
  auth.logout()
  ElMessage.success('已退出登录')
  router.push({ name: 'login' })
}
</script>

<style scoped>
.logout-icon {
  color: #7c8aa5;
  cursor: pointer;
  font-size: 16px;
  flex-shrink: 0;
}
.logout-icon:hover {
  color: #fff;
}
</style>
