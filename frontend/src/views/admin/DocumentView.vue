<template>
  <div class="view-scroll">
    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="card-head">
          <div style="display: flex; align-items: center; gap: 10px">
            <el-button link :icon="ArrowLeft" @click="router.back()">返回</el-button>
            <span class="card-title">{{ kbName || '文档管理' }}</span>
            <el-tag v-if="kb" size="small" type="success" effect="plain">
              {{ kb.chunk_count }} 个分块
            </el-tag>
          </div>
          <div class="card-actions">
            <el-button :icon="Refresh" @click="loadDocuments">刷新</el-button>
          </div>
        </div>
      </template>

      <el-upload
        drag
        multiple
        :show-file-list="false"
        :http-request="doUpload"
        :before-upload="beforeUpload"
        :accept="acceptAttr"
        :disabled="uploading"
        style="margin-bottom: 18px"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          将文件拖到此处,或<em>点击选择文件</em>
        </div>
        <template #tip>
          <div class="el-upload__tip">
            支持 {{ allowedExt.join(' / ') }},单个文件不超过 {{ maxSizeMb }} MB。
            上传后自动解析、分块并向量化入库。
          </div>
        </template>
      </el-upload>

      <el-progress
        v-if="uploading"
        :percentage="uploadPercent"
        :stroke-width="6"
        style="margin-bottom: 16px"
      />

      <el-table :data="documents" v-loading="loading" empty-text="还没有文档,先上传一份商品资料">
        <el-table-column prop="filename" label="文件名" min-width="240" show-overflow-tooltip />

        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.file_type }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
        </el-table-column>

        <el-table-column label="状态" width="180">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small" effect="plain">
              {{ statusText(row.status) }}
            </el-tag>
            <el-tooltip v-if="row.status === 'failed' && row.error_message" :content="row.error_message">
              <el-icon style="margin-left: 6px; vertical-align: -2px; color: #f56c6c">
                <WarningFilled />
              </el-icon>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="分块数" width="90" align="center">
          <template #default="{ row }">{{ row.chunk_count }}</template>
        </el-table-column>

        <el-table-column label="上传时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>

        <el-table-column label="操作" width="240" align="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :disabled="row.status !== 'ready' || !row.chunk_count"
              @click="openChunks(row)"
            >
              查看分块
            </el-button>
            <el-button link type="primary" @click="reprocess(row)">重新处理</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 分块预览 -->
    <el-drawer v-model="drawerVisible" size="52%" :title="`分块预览 · ${currentDoc?.filename || ''}`">
      <div v-loading="chunkLoading">
        <div v-for="chunk in chunks" :key="chunk.id" class="chunk-item">
          <div class="chunk-head">
            <el-tag size="small" effect="plain">第 {{ chunk.chunk_index + 1 }} 块</el-tag>
            <span class="chunk-tokens">{{ chunk.token_count }} tokens</span>
          </div>
          <div class="chunk-body">{{ chunk.content }}</div>
        </div>

        <el-empty v-if="!chunkLoading && !chunks.length" description="暂无分块" :image-size="70" />
      </div>

      <template #footer>
        <el-pagination
          v-model:current-page="chunkPage"
          :page-size="chunkPageSize"
          :total="chunkTotal"
          layout="total, prev, pager, next"
          small
          background
          @current-change="loadChunks"
        />
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  Refresh,
  UploadFilled,
  WarningFilled,
} from '@element-plus/icons-vue'
import { docApi, kbApi, uploadApi } from '@/api'

const route = useRoute()
const router = useRouter()

const kbId = computed(() => Number(route.params.kbId))
const kb = ref(null)
const kbName = computed(() => kb.value?.name || '')
const documents = ref([])
const loading = ref(false)

const uploading = ref(false)
const uploadPercent = ref(0)

const drawerVisible = ref(false)
const currentDoc = ref(null)
const chunks = ref([])
const chunkTotal = ref(0)
const chunkPage = ref(1)
const chunkPageSize = ref(10)
const chunkLoading = ref(false)

// 扩展名与大小上限由服务端下发(见 backend/app/api/admin.py 的 /upload-limits)。
// 两边各写一份常量的后果是悄悄跑偏:这里曾写死 20MB 而后端是 50MB,20~50MB 的
// 文件在浏览器里就被拦下,还提示了一个错误的上限。下面的默认值只在请求返回前兜底。
const allowedExt = ref(['.pdf', '.docx', '.txt', '.md', '.xlsx', '.csv'])
const maxSizeMb = ref(50)
const acceptAttr = computed(() => allowedExt.value.join(','))

let pollTimer = null
// 拖入多个文件时 doUpload 会并发跑多次,谁都别抢着把进度条和禁用态关掉
let pendingUploads = 0

async function loadUploadLimits() {
  try {
    const data = await uploadApi.limits()
    if (data.allowed_extensions?.length) allowedExt.value = data.allowed_extensions
    if (data.max_upload_mb) maxSizeMb.value = data.max_upload_mb
  } catch {
    // 拿不到就用兜底值:预检只是体验优化,真正的校验始终在服务端
  }
}

async function loadKb() {
  try {
    kb.value = await kbApi.get(kbId.value)
  } catch (err) {
    ElMessage.error(err.message || '知识库不存在')
    router.push({ name: 'knowledge-bases' })
  }
}

async function loadDocuments() {
  loading.value = true
  try {
    documents.value = await docApi.list(kbId.value)
    schedulePollIfNeeded()
  } catch (err) {
    ElMessage.error(err.message || '加载文档失败')
  } finally {
    loading.value = false
  }
}

/** 有文档处于处理中时轮询刷新,处理完自动停止。 */
function schedulePollIfNeeded() {
  const processing = documents.value.some((d) => d.status === 'processing')
  if (processing && !pollTimer) {
    pollTimer = setInterval(async () => {
      const data = await docApi.list(kbId.value).catch(() => null)
      if (!data) return
      documents.value = data
      if (!data.some((d) => d.status === 'processing')) {
        clearInterval(pollTimer)
        pollTimer = null
        loadKb()
      }
    }, 3000)
  } else if (!processing && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
    loadKb()
  }
}

function beforeUpload(file) {
  const name = file.name.toLowerCase()
  const dot = name.lastIndexOf('.')
  const ext = dot >= 0 ? name.slice(dot) : ''
  if (!allowedExt.value.includes(ext)) {
    ElMessage.error(`不支持的文件类型:${ext || '未知'}`)
    return false
  }
  if (file.size > maxSizeMb.value * 1024 * 1024) {
    ElMessage.error(`文件不能超过 ${maxSizeMb.value} MB`)
    return false
  }
  return true
}

async function doUpload({ file }) {
  pendingUploads += 1
  uploading.value = true
  uploadPercent.value = 0
  try {
    await docApi.upload(kbId.value, file, (p) => {
      uploadPercent.value = p
    })
    ElMessage.success(`${file.name} 已上传,正在后台解析入库`)
    await loadDocuments()
  } catch (err) {
    ElMessage.error(err.message || '上传失败')
  } finally {
    pendingUploads -= 1
    if (pendingUploads === 0) {
      uploading.value = false
      uploadPercent.value = 0
    }
  }
}

async function reprocess(row) {
  try {
    await docApi.reprocess(kbId.value, row.id)
    ElMessage.success('已重新提交处理')
    loadDocuments()
  } catch (err) {
    ElMessage.error(err.message || '操作失败')
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除文档「${row.filename}」?其分块会一并移除。`,
      '删除文档',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await docApi.remove(kbId.value, row.id)
    ElMessage.success('已删除')
    loadDocuments()
  } catch (err) {
    ElMessage.error(err.message || '删除失败')
  }
}

async function openChunks(row) {
  currentDoc.value = row
  chunkPage.value = 1
  chunks.value = []
  drawerVisible.value = true
  await loadChunks()
}

async function loadChunks() {
  if (!currentDoc.value) return
  chunkLoading.value = true
  try {
    const data = await docApi.chunks(kbId.value, currentDoc.value.id, {
      page: chunkPage.value,
      page_size: chunkPageSize.value,
    })
    chunks.value = data.items
    chunkTotal.value = data.total
  } catch (err) {
    ElMessage.error(err.message || '加载分块失败')
  } finally {
    chunkLoading.value = false
  }
}

function statusType(status) {
  return { ready: 'success', processing: 'warning', failed: 'danger' }[status] || 'info'
}

function statusText(status) {
  return { ready: '已就绪', processing: '处理中', failed: '处理失败' }[status] || status
}

function formatSize(bytes) {
  if (!bytes) return '-'
  const units = ['B', 'KB', 'MB']
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

watch(kbId, () => {
  loadKb()
  loadDocuments()
})

onMounted(() => {
  loadKb()
  loadDocuments()
  loadUploadLimits()
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
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

.chunk-item {
  border: 1px solid var(--rag-border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 12px;
}

.chunk-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.chunk-tokens {
  font-size: 12px;
  color: var(--rag-text-muted);
}

.chunk-body {
  font-size: 13px;
  line-height: 1.75;
  color: #4b5563;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
