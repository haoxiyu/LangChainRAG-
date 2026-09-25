<template>
  <div class="msg-row" :class="role">
    <div class="msg-avatar" :class="role">
      <el-icon v-if="role === 'assistant'"><MagicStick /></el-icon>
      <el-icon v-else><User /></el-icon>
    </div>

    <div class="msg-main">
      <div
        v-if="showThinking"
        class="msg-bubble thinking"
      >
        <span class="thinking-dot" /><span class="thinking-dot" /><span class="thinking-dot" />
        <span style="margin-left: 6px">{{ status || '正在思考' }}</span>
      </div>

      <div
        v-else
        class="msg-bubble"
        :class="{ 'cursor-blink': streaming }"
        @click="onBubbleClick"
        v-html="bubbleHtml"
      />

      <div v-if="role === 'assistant' && !streaming && metaText" class="msg-meta">
        <el-icon><Timer /></el-icon>
        <span>{{ metaText }}</span>
        <el-tag v-if="cacheHit" size="small" type="success" effect="plain">
          语义缓存命中
        </el-tag>
      </div>

      <CitationList
        v-if="role === 'assistant' && references?.length"
        :references="references"
        :active-index="activeIndex"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { MagicStick, Timer, User } from '@element-plus/icons-vue'
import CitationList from './CitationList.vue'
import { renderAnswer, renderPlain } from '@/utils/markdown'

const props = defineProps({
  role: { type: String, required: true }, // 'user' | 'assistant'
  content: { type: String, default: '' },
  references: { type: Array, default: () => [] },
  streaming: { type: Boolean, default: false },
  status: { type: String, default: '' },
  latencyMs: { type: Number, default: 0 },
  cacheHit: { type: Boolean, default: false },
  model: { type: String, default: '' },
  activeIndex: { type: Number, default: null },
})

const emit = defineEmits(['cite'])

const displayed = ref('')
let timer = null

/**
 * 流式期间限频重渲染:逐 token 全量重跑 Markdown 解析在大回答下是浪费,
 * 攒到 ~60ms 渲染一次,肉眼仍是逐字效果,解析次数降一个数量级。
 */
watch(
  () => [props.content, props.streaming],
  ([content, streaming]) => {
    if (!streaming) {
      clearInterval(timer)
      timer = null
      displayed.value = content
      return
    }
    if (!timer) {
      timer = setInterval(() => {
        displayed.value = props.content
      }, 60)
    }
  },
  { immediate: true }
)

onUnmounted(() => clearInterval(timer))

const validIndexes = computed(
  () => new Set((props.references || []).map((r) => r.index))
)

const bubbleHtml = computed(() => {
  if (props.role !== 'assistant') return renderPlain(props.content)
  return renderAnswer(displayed.value, validIndexes.value)
})

// 还没有任何输出时显示「思考中」,避免出现空气泡
const showThinking = computed(
  () => props.role === 'assistant' && props.streaming && !displayed.value
)

const metaText = computed(() => {
  const parts = []
  if (props.latencyMs) parts.push(`耗时 ${(props.latencyMs / 1000).toFixed(2)}s`)
  if (props.model) parts.push(props.model)
  return parts.join(' · ')
})

function onBubbleClick(e) {
  const el = e.target
  if (!(el instanceof HTMLElement)) return
  if (!el.classList.contains('citation-ref') || el.classList.contains('invalid')) return
  emit('cite', Number(el.dataset.citation))
}
</script>

<style scoped>
.msg-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--rag-text-muted);
}

.thinking {
  display: flex;
  align-items: center;
  color: var(--rag-text-muted);
}

.thinking-dot {
  width: 6px;
  height: 6px;
  margin-right: 3px;
  border-radius: 50%;
  background: var(--rag-primary);
  opacity: 0.35;
  animation: thinking 1.2s infinite ease-in-out;
}
.thinking-dot:nth-child(2) {
  animation-delay: 0.2s;
}
.thinking-dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes thinking {
  0%,
  80%,
  100% {
    opacity: 0.25;
    transform: scale(0.85);
  }
  40% {
    opacity: 1;
    transform: scale(1);
  }
}
</style>
