<template>
  <div class="paper-detail-mask" @click.self="$emit('close')">
    <div class="paper-detail">
      <div class="detail-header">
        <h3>{{ paper?.title || rid }}</h3>
        <button class="btn close" @click="$emit('close')">关闭</button>
      </div>

      <div v-if="loading" class="detail-loading">加载中…</div>
      <template v-else-if="paper">
        <div class="detail-meta">
          <span v-if="paper.doi">DOI: {{ paper.doi }}</span>
          <span v-if="paper.quartile">JCR {{ paper.quartile }}</span>
          <span>渲染: {{ paper.render?.status || '未渲染' }}</span>
          <span v-for="job in paper.compile_jobs" :key="job.level">{{ job.level }}: {{ job.status }}</span>
        </div>

        <div class="detail-actions">
          <button class="btn" :disabled="rendering" @click="doRender">
            {{ rendering ? '渲染入队中…' : '生成阅读视图' }}
          </button>
          <button class="btn" :disabled="translating" @click="doTranslate">请求翻译</button>
        </div>

        <div class="tabs">
          <button
            v-for="tab in tabs"
            :key="tab.key"
            class="tab"
            :class="{ active: currentTab === tab.key }"
            @click="currentTab = tab.key"
          >
            {{ tab.label }}
          </button>
        </div>

        <NotePanel
          v-if="currentTab === 'notes'"
          :rid="rid"
          :compile-jobs="paper.compile_jobs"
        />
        <AttachmentPanel v-else-if="currentTab === 'attachments'" :rid="rid" />
        <CardsPanel v-else-if="currentTab === 'cards'" :rid="rid" />
        <ReviewPanel v-else-if="currentTab === 'review'" :rid="rid" />
        <div v-else class="overview">
          <pre class="meta-json">{{ JSON.stringify(paper, null, 2) }}</pre>
        </div>
      </template>
    </div>
  </div>
</template>

<script>
import { ref, onMounted, onUnmounted } from 'vue'
import { getLiteraturePaper, triggerLiteratureRender, triggerLiteratureTranslation } from '@/apis/literature_api'
import NotePanel from '@/components/literature/NotePanel.vue'
import AttachmentPanel from '@/components/literature/AttachmentPanel.vue'
import CardsPanel from '@/components/literature/CardsPanel.vue'
import ReviewPanel from '@/components/literature/ReviewPanel.vue'

const POLL_INTERVAL_MS = 3000

export default {
  name: 'PaperDetail',
  components: { NotePanel, AttachmentPanel, CardsPanel, ReviewPanel },
  props: {
    rid: { type: String, required: true }
  },
  emits: ['close'],
  setup(props) {
    const paper = ref(null)
    const loading = ref(true)
    const rendering = ref(false)
    const translating = ref(false)
    const currentTab = ref('notes')
    const tabs = [
      { key: 'notes', label: '笔记 (L1·L2·L3)' },
      { key: 'attachments', label: '依附资料' },
      { key: 'cards', label: '卡片' },
      { key: 'review', label: '识别复核' },
      { key: 'overview', label: '元数据' }
    ]
    let pollTimer = null

    const load = async () => {
      try {
        paper.value = await getLiteraturePaper(props.rid)
      } finally {
        loading.value = false
      }
    }

    /** 渲染/编译任务进行中时轮询刷新（组件卸载必须清理） */
    const startPollingIfBusy = () => {
      stopPolling()
      const busy = (paper.value?.compile_jobs || []).some((job) =>
        ['PENDING', 'RUNNING'].includes(job.status)
      )
      if (busy || ['PENDING', 'RUNNING'].includes(paper.value?.render?.status || '')) {
        pollTimer = setInterval(load, POLL_INTERVAL_MS)
      }
    }
    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
      }
    }

    const doRender = async () => {
      rendering.value = true
      try {
        await triggerLiteratureRender(props.rid)
        await load()
        startPollingIfBusy()
      } finally {
        rendering.value = false
      }
    }
    const doTranslate = async () => {
      translating.value = true
      try {
        await triggerLiteratureTranslation(props.rid)
        await load()
        startPollingIfBusy()
      } finally {
        translating.value = false
      }
    }

    onMounted(async () => {
      await load()
      startPollingIfBusy()
    })
    onUnmounted(stopPolling)

    return {
      paper,
      loading,
      rendering,
      translating,
      currentTab,
      tabs,
      doRender,
      doTranslate
    }
  }
}
</script>

<style lang="less" scoped>
.paper-detail-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.paper-detail {
  background: #fff;
  border-radius: 10px;
  width: min(920px, 92vw);
  max-height: 86vh;
  overflow: auto;
  padding: 16px 20px;
}
.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.detail-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  opacity: 0.75;
  margin: 8px 0;
}
.detail-actions {
  display: flex;
  gap: 8px;
  margin: 10px 0;
}
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border-color, #eee);
  margin-bottom: 10px;
}
.tab {
  border: none;
  background: transparent;
  padding: 8px 14px;
  cursor: pointer;
  &.active {
    border-bottom: 2px solid var(--primary-color, #1677ff);
    font-weight: 600;
  }
}
.meta-json {
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
