<template>
  <div class="chat-wrap">
    <!-- 会话侧栏 -->
    <aside class="chat-sidebar">
      <div class="chat-sidebar-head">
        <el-button type="primary" style="width: 100%" @click="startNewChat">
          <el-icon style="margin-right: 4px"><Plus /></el-icon>
          新建对话
        </el-button>
      </div>

      <div class="chat-conv-list" v-loading="loadingConvs">
        <div
          v-for="conv in conversations"
          :key="conv.id"
          class="chat-conv-item"
          :class="{ active: conv.id === convId }"
          @click="openConversation(conv.id)"
        >
          <div class="chat-conv-main">
            <div class="chat-conv-title">{{ conv.title }}</div>
            <div class="chat-conv-time">{{ formatTime(conv.updated_at) }}</div>
          </div>

          <el-dropdown
            trigger="click"
            placement="bottom-end"
            @command="(cmd) => onConvCommand(cmd, conv)"
          >
            <el-icon class="conv-more" @click.stop><MoreFilled /></el-icon>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="rename">重命名</el-dropdown-item>
                <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <div v-if="!loadingConvs && !conversations.length" class="conv-empty">
          还没有历史对话
        </div>
      </div>
    </aside>

    <!-- 对话区 -->
    <section class="chat-main">
      <div class="chat-topbar">
        <el-tag type="info" effect="plain">
          <el-icon style="vertical-align: -2px"><Collection /></el-icon>
          自动检索全部知识库
        </el-tag>

        <el-tag v-if="currentRewrite" size="small" type="warning" effect="plain">
          已改写检索:{{ currentRewrite }}
        </el-tag>

        <div style="margin-left: auto; display: flex; gap: 8px; align-items: center">
          <el-tag v-if="generating" size="small" type="primary" effect="plain">
            {{ statusText || '生成中' }}
          </el-tag>
          <el-button
            v-if="generating"
            size="small"
            type="danger"
            plain
            @click="stopGeneration"
          >
            停止生成
          </el-button>
        </div>
      </div>

      <div ref="scrollRef" class="chat-messages" @scroll="onScroll">
        <div v-if="!messages.length && !loadingMessages" class="empty-state">
          <el-icon :size="46" color="#c8cfdb"><ChatLineRound /></el-icon>
          <h3>向知识库提问吧</h3>
          <p style="margin: 0">
            直接提问即可,系统会自动在全部知识库中检索,并标注每句话的来源
          </p>
          <div style="margin-top: 10px">
            <span
              v-for="q in exampleQuestions"
              :key="q"
              class="example-question"
              @click="input = q"
            >{{ q }}</span>
          </div>
        </div>

        <ChatMessage
          v-for="msg in messages"
          :key="msg.uid"
          :role="msg.role"
          :content="msg.content"
          :references="msg.references"
          :streaming="msg.streaming"
          :status="msg.status"
          :latency-ms="msg.latency_ms"
          :cache-hit="msg.cache_hit"
          :model="msg.model"
          :active-index="msg.activeRef"
          @cite="(idx) => onCite(msg, idx)"
        />
      </div>

      <div class="chat-input-area">
        <div class="chat-input-box">
          <el-input
            ref="inputRef"
            v-model="input"
            class="chat-textarea"
            type="textarea"
            :rows="1"
            :autosize="{ minRows: 1, maxRows: 6 }"
            placeholder="输入你的问题,Enter 发送,Shift + Enter 换行"
            resize="none"
            @keydown.enter.exact.prevent="send"
            :disabled="generating"
          />
          <div class="chat-input-actions">
            <span class="chat-hint">
              回答由大模型基于知识库生成,重要信息请以商品页为准
            </span>
            <el-button
              type="primary"
              :loading="generating"
              :disabled="!input.trim()"
              @click="send"
            >
              发送
            </el-button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ChatLineRound,
  Collection,
  MoreFilled,
  Plus,
} from '@element-plus/icons-vue'
import ChatMessage from '@/components/ChatMessage.vue'
import { convApi, streamChat } from '@/api'

const route = useRoute()
const router = useRouter()

const conversations = ref([])
const messages = ref([])
const convId = ref(null)
const input = ref('')
const generating = ref(false)
const statusText = ref('')
const currentRewrite = ref('')
const loadingConvs = ref(false)
const loadingMessages = ref(false)

const scrollRef = ref(null)
const inputRef = ref(null)

let controller = null
let uidSeq = 0
// 用户手动上滚后不再强制吸底,否则会打断阅读
let stickToBottom = true

const exampleQuestions = [
  '这款手机的电池容量是多少?',
  '支持多少瓦快充?',
  '保修政策是怎样的?',
  '现在有什么优惠活动?',
]

// ---------- 数据加载 ----------

async function loadConversations() {
  loadingConvs.value = true
  try {
    const data = await convApi.list({ limit: 100 })
    conversations.value = data.items
  } catch (err) {
    ElMessage.error(err.message || '加载会话失败')
  } finally {
    loadingConvs.value = false
  }
}

function toLocalMessage(m) {
  return {
    uid: `srv-${m.id}`,
    id: m.id,
    role: m.role,
    content: m.content,
    references: m.references || [],
    streaming: false,
    status: '',
    latency_ms: m.latency_ms,
    cache_hit: false,
    model: m.model,
    activeRef: null,
  }
}

async function loadMessages(id) {
  loadingMessages.value = true
  try {
    const detail = await convApi.get(id)
    messages.value = detail.messages.map(toLocalMessage)
    await nextTick()
    scrollToBottom(true)
  } catch (err) {
    ElMessage.error(err.message || '加载会话失败')
    messages.value = []
  } finally {
    loadingMessages.value = false
  }
}

// ---------- 会话切换 ----------

function startNewChat() {
  if (generating.value) stopGeneration()
  router.push({ name: 'chat' })
}

function openConversation(id) {
  if (id === convId.value) return
  if (generating.value) stopGeneration()
  router.push({ name: 'chat', params: { id: String(id) } })
}

// 路由参数是唯一事实来源:点侧栏、点浏览器前进后退都收敛到这里
watch(
  () => route.params.id,
  async (raw) => {
    const target = raw ? Number(raw) : null
    // 流式回答开始时服务端才分配 id,这里刚 push 过去,无需重新拉取
    if (target === convId.value) return

    convId.value = target
    currentRewrite.value = ''
    statusText.value = ''
    messages.value = []

    if (target) {
      await loadMessages(target)
    } else {
      await nextTick()
      inputRef.value?.focus()
    }
  },
  { immediate: true }
)

onMounted(loadConversations)

onBeforeUnmount(() => {
  controller?.abort()
})

// ---------- 发送与流式接收 ----------

function newLocalMessage(role, extra = {}) {
  const msg = {
    uid: `loc-${++uidSeq}`,
    id: null,
    role,
    content: '',
    references: [],
    streaming: false,
    status: '',
    latency_ms: 0,
    cache_hit: false,
    model: '',
    activeRef: null,
    ...extra,
  }
  messages.value.push(msg)
  return msg
}

async function send() {
  const question = input.value.trim()
  if (!question || generating.value) return

  input.value = ''
  currentRewrite.value = ''
  newLocalMessage('user', { content: question })
  const assistant = newLocalMessage('assistant', {
    streaming: true,
    status: '正在理解问题',
  })
  generating.value = true
  statusText.value = '正在理解问题'
  scrollToBottom(true)

  controller = new AbortController()
  let failed = false

  try {
    await streamChat(
      {
        conversation_id: convId.value,
        question,
      },
      (event) => handleEvent(event, assistant),
      controller.signal
    )
  } catch (err) {
    if (err.name === 'AbortError') {
      // 用户主动停止,后端已把已生成部分落库,不算错误
      assistant.content = assistant.content || '(已停止生成)'
    } else {
      failed = true
      assistant.content = assistant.content || `生成失败:${err.message}`
      ElMessage.error(err.message || '生成失败')
    }
  } finally {
    assistant.streaming = false
    assistant.status = ''
    generating.value = false
    statusText.value = ''
    controller = null
    if (!failed) loadConversations()
    scrollToBottom()
  }
}

function handleEvent(event, assistant) {
  switch (event.type) {
    case 'conversation':
      // 新会话:服务端已建好,把 id 同步进地址栏(不触发重载)
      if (!convId.value) {
        convId.value = event.conversation_id
        router.replace({ name: 'chat', params: { id: String(event.conversation_id) } })
      }
      break

    case 'status':
      assistant.status = event.message
      statusText.value = event.message
      break

    case 'rewrite':
      currentRewrite.value = event.query
      break

    case 'references':
      assistant.references = event.references || []
      break

    case 'token':
      assistant.content += event.content
      scrollToBottom()
      break

    case 'title':
      applyTitle(event.conversation_id, event.title)
      break

    case 'done':
      assistant.latency_ms = event.latency_ms || 0
      assistant.cache_hit = Boolean(event.cache_hit)
      break

    case 'error':
      assistant.content = assistant.content || `生成失败:${event.message}`
      ElMessage.error(event.message || '生成失败')
      break
  }
}

function stopGeneration() {
  controller?.abort()
  generating.value = false
}

function applyTitle(id, title) {
  const hit = conversations.value.find((c) => c.id === id)
  if (hit) hit.title = title
  else conversations.value.unshift({ id, title, updated_at: new Date().toISOString() })
}

// ---------- 引用交互 ----------

function onCite(msg, index) {
  msg.activeRef = msg.activeRef === index ? null : index
}

// ---------- 滚动 ----------

function onScroll() {
  const el = scrollRef.value
  if (!el) return
  stickToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
}

function scrollToBottom(force = false) {
  if (!force && !stickToBottom) return
  nextTick(() => {
    const el = scrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// ---------- 会话操作 ----------

async function onConvCommand(cmd, conv) {
  if (cmd === 'rename') {
    try {
      const { value } = await ElMessageBox.prompt('请输入新的会话名称', '重命名', {
        inputValue: conv.title,
        inputValidator: (v) => (v && v.trim() ? true : '名称不能为空'),
        confirmButtonText: '保存',
        cancelButtonText: '取消',
      })
      const updated = await convApi.rename(conv.id, value.trim())
      conv.title = updated.title
      ElMessage.success('已重命名')
    } catch (err) {
      if (err !== 'cancel' && err?.message) ElMessage.error(err.message)
    }
    return
  }

  if (cmd === 'delete') {
    try {
      await ElMessageBox.confirm(`确定删除会话「${conv.title}」?`, '删除会话', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
    try {
      await convApi.remove(conv.id)
      conversations.value = conversations.value.filter((c) => c.id !== conv.id)
      if (convId.value === conv.id) router.push({ name: 'chat' })
      ElMessage.success('已删除')
    } catch (err) {
      ElMessage.error(err.message || '删除失败')
    }
  }
}

// ---------- 展示辅助 ----------

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (sameDay) return `${hh}:${mm}`
  return `${d.getMonth() + 1}月${d.getDate()}日`
}
</script>

<style scoped>
/* 三个点必须是条目里的普通 flex 子项,不能用绝对定位:el-dropdown 会给自己套一个
   0×0 的包裹层,绝对定位的 right 是相对那个包裹层解析的,图标会被甩到卡片左边缘、
   一半压在标题上并被列表的 overflow 裁掉;那个包裹层本身还会在流里占掉一行高度,
   把条目从 55px 撑到 74px。这里的几何由 browser_check.py 的用例守着。 */
.conv-more {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--rag-text-muted);
  /* 常驻半透明而不是 hover 才出现:答辩演示时得让人一眼看出会话可以重命名/删除 */
  opacity: 0.35;
  transition: opacity 0.15s, background 0.15s;
}

.conv-more:hover {
  opacity: 1;
  background: #e7ecf6;
  color: var(--rag-primary);
}

.chat-conv-item:hover .conv-more,
.chat-conv-item.active .conv-more {
  opacity: 1;
}

.conv-empty {
  text-align: center;
  color: var(--rag-text-muted);
  font-size: 13px;
  padding: 24px 0;
}
</style>
