import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { public: true, title: '注册' },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/chat',
    children: [
      {
        path: 'chat/:id?',
        name: 'chat',
        component: () => import('@/views/ChatView.vue'),
        meta: { title: '智能问答' },
      },
      {
        path: 'conversations',
        name: 'conversations',
        component: () => import('@/views/ConversationsView.vue'),
        meta: { title: '我的会话' },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('@/views/ProfileView.vue'),
        meta: { title: '个人设置' },
      },
      {
        path: 'admin/dashboard',
        name: 'dashboard',
        component: () => import('@/views/admin/DashboardView.vue'),
        meta: { title: '数据概览', admin: true },
      },
      {
        path: 'admin/knowledge-bases',
        name: 'knowledge-bases',
        component: () => import('@/views/admin/KnowledgeBaseView.vue'),
        meta: { title: '知识库管理', admin: true },
      },
      {
        path: 'admin/knowledge-bases/:kbId/documents',
        name: 'documents',
        component: () => import('@/views/admin/DocumentView.vue'),
        meta: { title: '文档管理', admin: true },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/chat',
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  document.title = to.meta.title
    ? `${to.meta.title} - 商品知识库问答`
    : '商品知识库问答'

  // 未登录只能访问公开页
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 已登录不再回登录页
  if (to.meta.public && auth.isLoggedIn) {
    return { name: 'chat' }
  }

  // 管理页做前端拦一层;真正的权限校验在后端(前端只是体验优化)
  if (to.meta.admin && !auth.isAdmin) {
    return { name: 'chat' }
  }

  return true
})

export default router
