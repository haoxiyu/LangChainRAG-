<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">商品知识库问答</h1>
      <p class="auth-subtitle">基于 LangChain 的企业级 RAG 问答系统</p>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            placeholder="请输入用户名"
            :prefix-icon="User"
            autocomplete="username"
          />
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            :prefix-icon="Lock"
            show-password
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>

        <el-button
          type="primary"
          size="large"
          style="width: 100%"
          :loading="auth.loading"
          @click="onSubmit"
        >
          登录
        </el-button>
      </el-form>

      <div class="auth-footer">
        还没有账号?<router-link to="/register">立即注册</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const formRef = ref(null)

const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function onSubmit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  try {
    const user = await auth.login(form.username.trim(), form.password)
    ElMessage.success(`欢迎回来,${user.username}`)
    // 登录前若被守卫拦下,登录后回到原目标页
    const redirect = route.query.redirect
    router.push(typeof redirect === 'string' ? redirect : { name: 'chat' })
  } catch (err) {
    ElMessage.error(err.message || '登录失败')
  }
}
</script>
