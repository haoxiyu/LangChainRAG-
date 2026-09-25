<template>
  <div class="view-scroll">
    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="card-head">
          <span class="card-title">知识库管理</span>
          <div class="card-actions">
            <el-button :icon="Refresh" @click="load">刷新</el-button>
            <el-button type="primary" :icon="Plus" @click="openCreate">
              新建知识库
            </el-button>
          </div>
        </div>
      </template>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="每个知识库对应一类商品资料;上传文档后系统会自动解析、分块并向量化入库。"
        style="margin-bottom: 16px"
      />

      <el-table :data="rows" v-loading="loading" empty-text="还没有知识库,点击右上角新建">
        <el-table-column prop="name" label="名称" min-width="200">
          <template #default="{ row }">
            <el-link type="primary" @click="openDocuments(row)">{{ row.name }}</el-link>
          </template>
        </el-table-column>

        <el-table-column prop="description" label="描述" min-width="240">
          <template #default="{ row }">
            <span :title="row.description">{{ row.description || '-' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="文档数" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.document_count }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="分块数" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="success" effect="plain">{{ row.chunk_count }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="embedding_model" label="向量模型" width="180" />

        <el-table-column label="创建时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="240" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDocuments(row)">管理文档</el-button>
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? '编辑知识库' : '新建知识库'"
      width="480px"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如:星辰 X1 手机" maxlength="128" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            maxlength="1000"
            show-word-limit
            placeholder="简单描述这个知识库收录的商品范围"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { kbApi } from '@/api'

const router = useRouter()

const rows = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)
const formRef = ref(null)

const form = reactive({ name: '', description: '' })

const rules = {
  name: [
    { required: true, message: '请输入知识库名称', trigger: 'blur' },
    { max: 128, message: '名称最长 128 个字符', trigger: 'blur' },
  ],
}

async function load() {
  loading.value = true
  try {
    rows.value = await kbApi.list()
  } catch (err) {
    ElMessage.error(err.message || '加载知识库失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  form.name = ''
  form.description = ''
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = row
  form.name = row.name
  form.description = row.description || ''
  dialogVisible.value = true
}

async function submit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  saving.value = true
  try {
    const payload = { name: form.name.trim(), description: form.description.trim() }
    if (editing.value) {
      await kbApi.update(editing.value.id, payload)
      ElMessage.success('已更新')
    } else {
      await kbApi.create(payload)
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    load()
  } catch (err) {
    ElMessage.error(err.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  const hasDocs = row.document_count > 0
  try {
    await ElMessageBox.confirm(
      hasDocs
        ? `知识库「${row.name}」下有 ${row.document_count} 个文档、${row.chunk_count} 个分块,删除后一并清除且不可恢复。确定删除?`
        : `确定删除知识库「${row.name}」?`,
      '删除知识库',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await kbApi.remove(row.id)
    ElMessage.success('已删除')
    load()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

function openDocuments(row) {
  router.push({ name: 'documents', params: { kbId: String(row.id) } })
}

function formatTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
</script>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
}

.card-actions {
  display: flex;
  gap: 8px;
}
</style>
