<template>
  <div class="view-scroll" v-loading="loading">
    <div class="stat-grid">
      <div v-for="card in statCards" :key="card.label" class="stat-card">
        <div class="stat-label">{{ card.label }}</div>
        <div class="stat-value" :style="card.color ? { color: card.color } : null">
          {{ card.value }}<span v-if="card.suffix" class="stat-suffix">{{ card.suffix }}</span>
        </div>
      </div>
    </div>

    <el-row :gutter="20">
      <el-col :xs="24" :md="12">
        <div class="panel" style="margin-bottom: 20px">
          <h3 class="panel-title">各知识库分块占比</h3>
          <div v-if="breakdown.length">
            <div v-for="item in breakdown" :key="item.id" class="kb-row">
              <div class="kb-row-head">
                <span>{{ item.name }}</span>
                <span class="kb-row-num">{{ item.chunks }} 块 · {{ item.percent }}%</span>
              </div>
              <el-progress
                :percentage="item.percent"
                :stroke-width="10"
                :show-text="false"
              />
            </div>
          </div>
          <el-empty v-else description="还没有知识库数据" :image-size="70" />
        </div>
      </el-col>

      <el-col :xs="24" :md="12">
        <div class="panel" style="margin-bottom: 20px">
          <h3 class="panel-title">运行时状态</h3>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="缓存">
              <el-tag :type="cache.enabled ? 'success' : 'info'" size="small" effect="plain">
                {{ cache.enabled ? '已启用' : '未启用' }}
              </el-tag>
              <span v-if="cache.enabled" style="margin-left: 8px">
                {{ cache.entries ?? 0 }} 条 / {{ formatBytes(cache.size_bytes) }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="BM25 稀疏索引">
              <span v-if="bm25.length">
                全局索引常驻内存
                ({{ bm25Total }} 块)
              </span>
              <span v-else>尚未加载(首次检索时构建)</span>
            </el-descriptions-item>
            <el-descriptions-item label="文档处理">
              <el-tag size="small" type="success" effect="plain">
                就绪 {{ stats.documents_ready ?? 0 }}
              </el-tag>
              <el-tag size="small" type="warning" effect="plain" style="margin-left: 6px">
                处理中 {{ stats.documents_processing ?? 0 }}
              </el-tag>
              <el-tag size="small" type="danger" effect="plain" style="margin-left: 6px">
                失败 {{ stats.documents_failed ?? 0 }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <div class="panel">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
            <h3 class="panel-title" style="margin: 0">用户列表</h3>
            <el-button size="small" :icon="Refresh" @click="loadStats">刷新</el-button>
          </div>
          <el-table :data="users" size="small" empty-text="暂无用户">
            <el-table-column prop="username" label="用户名" />
            <el-table-column label="角色" width="100">
              <template #default="{ row }">
                <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" size="small" effect="plain">
                  {{ row.role === 'admin' ? '管理员' : '用户' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="注册时间" width="170">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { adminApi } from '@/api'

const loading = ref(false)
const stats = ref({})
const users = ref([])

const cache = computed(() => stats.value.cache || {})
const breakdown = computed(() => stats.value.knowledge_base_breakdown || [])
const bm25 = computed(() => stats.value.bm25_indexes || [])
const bm25Total = computed(() =>
  bm25.value.reduce((sum, i) => sum + (i.chunks || 0), 0)
)

const statCards = computed(() => [
  { label: '用户总数', value: stats.value.users ?? 0 },
  { label: '知识库', value: stats.value.knowledge_bases ?? 0 },
  { label: '文档总数', value: stats.value.documents ?? 0 },
  {
    label: '分块总数',
    value: stats.value.chunks ?? 0,
    suffix: '块',
  },
  { label: '会话总数', value: stats.value.conversations ?? 0 },
  { label: '消息总数', value: stats.value.messages ?? 0 },
  {
    label: '入库失败文档',
    value: stats.value.documents_failed ?? 0,
    color: (stats.value.documents_failed ?? 0) > 0 ? '#f56c6c' : undefined,
  },
])

async function loadStats() {
  loading.value = true
  try {
    const [s, u] = await Promise.all([adminApi.stats(), adminApi.users({ limit: 50 })])
    stats.value = s
    users.value = u
  } catch (err) {
    ElMessage.error(err.message || '加载统计失败')
  } finally {
    loading.value = false
  }
}

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let n = bytes
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function formatTime(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

onMounted(loadStats)
</script>

<style scoped>
.kb-row {
  margin-bottom: 14px;
}

.kb-row-head {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 5px;
}

.kb-row-num {
  color: var(--rag-text-muted);
}
</style>
