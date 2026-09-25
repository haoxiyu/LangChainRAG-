<template>
  <div v-if="references?.length" class="citation-list">
    <div class="citation-head">
      <el-icon><Collection /></el-icon>
      <span>引用知识库片段 {{ references.length }} 条</span>
      <span style="margin-left: auto">点击可展开原文</span>
    </div>

    <div
      v-for="item in references"
      :key="item.chunk_id ?? item.index"
      class="citation-item"
      :class="{ 'is-active': item.index === activeIndex }"
      :ref="(el) => setItemRef(item.index, el)"
    >
      <div class="citation-item-top">
        <span class="citation-ref">{{ item.index }}</span>
        <!-- 检索跨全部知识库,不同库可能有同名文档,必须连库名一起标出来 -->
        <el-tag v-if="item.knowledge_base_name" size="small" type="primary" effect="plain">
          {{ item.knowledge_base_name }}
        </el-tag>
        <el-tag size="small" type="info" effect="plain">
          {{ item.filename || '未知来源' }}
        </el-tag>
        <el-tag v-if="item.chunk_index != null" size="small" effect="plain">
          第 {{ item.chunk_index + 1 }} 块
        </el-tag>
        <el-tag
          v-if="item.rerank_score != null"
          size="small"
          type="success"
          effect="plain"
        >
          相关度 {{ formatScore(item.rerank_score) }}
        </el-tag>
        <el-button
          link
          type="primary"
          size="small"
          style="margin-left: auto"
          @click="toggle(item.chunk_id ?? item.index)"
        >
          {{ expanded.has(item.chunk_id ?? item.index) ? '收起' : '展开原文' }}
        </el-button>
      </div>

      <div
        class="citation-body"
        :class="{ clamped: !expanded.has(item.chunk_id ?? item.index) }"
      >{{ item.content }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { Collection } from '@element-plus/icons-vue'

const props = defineProps({
  references: { type: Array, default: () => [] },
  /** 回答里被点击的 [n],用于高亮对应卡片并滚动到可见区域 */
  activeIndex: { type: Number, default: null },
})

const expanded = ref(new Set())
const itemEls = new Map()

function setItemRef(index, el) {
  if (el) itemEls.set(index, el)
  else itemEls.delete(index)
}

function toggle(key) {
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

function formatScore(score) {
  if (typeof score !== 'number') return '-'
  return score.toFixed(4)
}

// 点击回答里的 [n] 后把对应卡片滚进视野
watch(
  () => props.activeIndex,
  (idx) => {
    if (idx == null) return
    const el = itemEls.get(idx)
    el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }
)

// 切换消息(引用列表整体变化)时重置展开状态
watch(
  () => props.references,
  () => {
    expanded.value = new Set()
  }
)
</script>

<style scoped>
.citation-item.is-active {
  background: #f0f5ff;
  box-shadow: inset 3px 0 0 var(--rag-primary);
}
</style>
