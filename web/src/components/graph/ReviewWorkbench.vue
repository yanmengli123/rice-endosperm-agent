<template>
  <div class="review-workbench">
    <div class="workbench-nav">
      <div class="nav-title">队列</div>
      <button
        v-for="queue in queues"
        :key="queue.key"
        class="nav-item"
        :class="{ active: activeQueue === queue.key }"
        type="button"
        @click="activeQueue = queue.key"
      >
        <span class="nav-label">{{ queue.label }}</span>
        <span v-if="queue.count" class="nav-count">{{ queue.count }}</span>
      </button>
      <div class="nav-hint">
        点击列表行，在右侧查看逐字证据并作出裁决；批准前必须确认将固定的引文。
      </div>
    </div>
    <div class="workbench-list">
      <GraphReviewQueue
        v-if="activeQueue === 'candidate'"
        embedded
        :open="true"
        :kb-id="kbId"
        @reviewed="$emit('reviewed')"
        @select="selectTriple"
      />
      <GateReviewQueue
        v-else-if="activeQueue === 'gate'"
        embedded
        :open="true"
        :kb-id="kbId"
        @reviewed="$emit('reviewed')"
      />
      <ConflictQueue
        v-else-if="activeQueue === 'conflict'"
        embedded
        :open="true"
        :kb-id="kbId"
        @reviewed="$emit('reviewed')"
      />
    </div>
    <div class="workbench-detail">
      <GraphDetailPanel
        :visible="Boolean(selectedItem)"
        :item="selectedItem"
        type="edge"
        :kb-id="kbId"
        @close="selectedItem = null"
        @reviewed="$emit('reviewed')"
      />
      <div v-if="!selectedItem" class="detail-placeholder">
        从左侧队列点击一条关系，这里会展示全部逐字引文（含上下文、文件、章节、页码）、
        机器校验徽标与人工决策入口。
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import GraphReviewQueue from '@/components/GraphReviewQueue.vue'
import GateReviewQueue from '@/components/GateReviewQueue.vue'
import ConflictQueue from '@/components/ConflictQueue.vue'
import GraphDetailPanel from '@/components/GraphDetailPanel.vue'

const props = defineProps({
  kbId: { type: String, required: true },
  summary: { type: Object, default: null }
})

defineEmits(['reviewed'])

const activeQueue = ref('candidate')
const selectedRecord = ref(null)

const queues = computed(() => [
  {
    key: 'candidate',
    label: '候选待审',
    count: Number(props.summary?.counts?.triples?.CANDIDATE ?? 0) || null
  },
  {
    key: 'gate',
    label: '门禁送审',
    count: Number(props.summary?.gates?.pending?._total ?? 0) || null
  },
  {
    key: 'conflict',
    label: '冲突登记',
    count: Number(props.summary?.conflicts?.open?._total ?? 0) || null
  }
])

// 队列行 → 详情面板：GraphDetailPanel 按内容哈希 triple_id 加载证据，这里做形状适配
const selectedItem = computed(() => {
  const record = selectedRecord.value
  if (!record) return null
  return {
    id: record.triple_id,
    data: {
      label: record.content || record.relation_type,
      original: {
        properties: {
          triple_id: record.triple_id,
          relation_type: record.relation_type
        }
      }
    }
  }
})

const selectTriple = (record) => {
  selectedRecord.value = record
}
</script>

<style scoped lang="less">
.review-workbench {
  display: grid;
  grid-template-columns: 200px minmax(360px, 1fr) minmax(340px, 420px);
  height: 100%;
  min-height: 0;
}

.workbench-nav {
  border-right: 1px solid var(--gray-200);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;

  .nav-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--gray-600);
    margin-bottom: 6px;
  }

  .nav-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: 13px;
    color: var(--gray-700);

    &:hover {
      background: var(--gray-50);
    }

    &.active {
      background: var(--main-color-bg-hover, rgba(22, 119, 255, 0.1));
      color: var(--main-color);
      font-weight: 600;
    }
  }

  .nav-count {
    font-size: 12px;
    font-weight: 700;
    color: var(--color-warning-600, #d97706);
    font-variant-numeric: tabular-nums;
  }

  .nav-hint {
    margin-top: auto;
    padding-top: 10px;
    font-size: 11px;
    line-height: 1.5;
    color: var(--gray-400);
  }
}

.workbench-list {
  min-width: 0;
  overflow-y: auto;
  padding: 10px;
}

.workbench-detail {
  position: relative;
  border-left: 1px solid var(--gray-200);
  overflow: hidden;

  :deep(.detail-panel) {
    position: absolute;
    inset: 0;
    width: 100%;
    max-height: 100%;
  }
}

.detail-placeholder {
  padding: 24px 16px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--gray-400);
}

@media (max-width: 1100px) {
  .review-workbench {
    grid-template-columns: 160px 1fr;

    .workbench-detail {
      display: none;
    }
  }
}
</style>
