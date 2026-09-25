<template>
  <div class="view-scroll">
    <el-row :gutter="20">
      <el-col :xs="24" :md="10">
        <el-card shadow="never" class="page-card">
          <template #header><span class="card-title">账号信息</span></template>

          <el-descriptions :column="1" border>
            <el-descriptions-item label="用户名">
              {{ auth.user?.username }}
            </el-descriptions-item>
            <el-descriptions-item label="角色">
              <el-tag :type="auth.isAdmin ? 'danger' : 'info'" effect="plain" size="small">
                {{ auth.isAdmin ? '管理员' : '普通用户' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="注册时间">
              {{ formatTime(auth.user?.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="权限说明">
              {{ auth.isAdmin ? '可管理知识库与文档' : '仅可进行知识库问答' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="14">
        <el-card shadow="never" class="page-card">
          <template #header><span class="card-title">修改密码</span></template>

          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            label-width="90px"
            style="max-width: 460px"
          >
            <el-form-item label="当前密码" prop="oldPassword">
              <el-input
                v-model="form.oldPassword"
                type="password"
                show-password
                placeholder="请输入当前密码"
              />
            </el-form-item>

            <el-form-item label="新密码" prop="newPassword">
              <el-input
                v-model="form.newPassword"
                type="password"
                show-password
                placeholder="6-64 位"
              />
            </el-form-item>

            <el-form-item label="确认密码" prop="confirm">
              <el-input
                v-model="form.confirm"
                type="password"
                show-password
                placeholder="请再次输入新密码"
              />
            </el-form-item>

            <el-form-item>
              <el-button type="primary" :loading="saving" @click="submit">
                保存修改
              </el-button>
              <el-button @click="reset">重置</el-button>
            </el-form-item>
          </el-form>

          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="修改成功后当前登录状态仍有效,下次登录请使用新密码。"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const formRef = ref(null)
const saving = ref(false)

const form = reactive({ oldPassword: '', newPassword: '', confirm: '' })

function validateConfirm(_rule, value, callback) {
  if (value !== form.newPassword) callback(new Error('两次输入的新密码不一致'))
  else callback()
}

const rules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 64, message: '密码长度 6-64 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    { validator: validateConfirm, trigger: 'blur' },
  ],
}

function reset() {
  formRef.value?.resetFields()
}

async function submit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  saving.value = true
  try {
    await auth.changePassword(form.oldPassword, form.newPassword)
    ElMessage.success('密码修改成功')
    reset()
  } catch (err) {
    ElMessage.error(err.message || '修改失败')
  } finally {
    saving.value = false
  }
}

function formatTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}
</script>
