<template>
  <div class="view-scroll">
    <el-card shadow="never" class="page-card">
    <template #header>
      <div class="card-head">
        <span class="card-title">我的会话</span>
        <div class="card-actions">
          <el-input
            v-model="keyword"
            placeholder="搜索会话标题"
            clearable
            style="width: 220px"
            :prefix-icon="Search"
            @keyup.enter="reload"
            @clear="reload"
          />
          <el-button :icon="Refresh" @click="reload">刷新</el-button>
          <el-button type="danger" plain :icon="Delete" @click="clearAll">
            清空全部
          </el-button>
        </div>
      </div>
    </template>

    <el-table :data="rows" v-loading="loading" empty-text="暂无会话记录">
      <el-table-column prop="title" label="会话标题" min-width="220">
        <template #default="{ row }">
          <el-link type="primary" @click="open(row)">{{ row.title }}</el-link>
        </template>
      </el-table-column>

      <el-table-column label="消息数" width="100" align="center">
        <template #default="{ row }">{{ row.message_count }}</template>
      </el-table-column>

      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>

      <el-table-column label="最近更新" width="180">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>

      <el-table-column label="操作" width="200" align="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="open(row)">继续对话</el-button>
          <el-button link type="primary" @click="rename(row)">重命名</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        background
        @current-change="load"
      />
    </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Refresh, Search } from '@element-plus/icons-vue'
import { convApi } from '@/api'

const router = useRouter()

const rows = ref([])
const loading = ref(false)
const keyword = ref('')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)

async function load() {
  loading.value = true
  try {
    const data = await convApi.list({
      keyword: keyword.value || undefined,
      offset: (page.value - 1) * pageSize.value,
      limit: pageSize.value,
    })
    rows.value = data.items
    total.value = data.total
  } catch (err) {
    ElMessage.error(err.message || '加载失败')
  } finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  load()
}

function open(row) {
  router.push({ name: 'chat', params: { id: String(row.id) } })
}

async function rename(row) {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的会话名称', '重命名', {
      inputValue: row.title,
      inputValidator: (v) => (v && v.trim() ? true : '名称不能为空'),
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    const updated = await convApi.rename(row.id, value.trim())
    row.title = updated.title
    ElMessage.success('已重命名')
  } catch (err) {
    if (err !== 'cancel' && err?.message) ElMessage.error(err.message)
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确定删除会话「${row.title}」?`, '删除会话', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await convApi.remove(row.id)
    ElMessage.success('已删除')
    load()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function clearAll() {
  try {
    await ElMessageBox.confirm(
      '将删除你的全部会话与聊天记录,该操作不可恢复。确定继续?',
      '清空全部会话',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await convApi.clearAll()
    ElMessage.success('已清空')
    reload()
  } catch (err) {
    ElMessage.error(err.message || '清空失败')
  }
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
  align-items: center;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
