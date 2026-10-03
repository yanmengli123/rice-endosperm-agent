<template>
  <div class="paper-detail-mask" :class="{ embedded }" @click.self="!embedded && $emit('close')">
    <div class="paper-detail">
      <div class="detail-header">
        <h3>{{ paper?.title || rid }}</h3>
        <button class="btn close" @click="$emit('close')">关闭</button>
      </div>

      <div v-if="loading" class="detail-loading">加载中…</div>
      <template v-else-if="paper">
        <div class="detail-meta">
          <template v-if="paper.source">
            <router-link
              v-if="userStore.isAdmin"
              :to="`/extensions/knowledgebase/${encodeURIComponent(paper.source.kb_id)}`"
            >
              知识源：{{ paper.source.name }}
            </router-link>
            <span v-else>知识源：{{ paper.source.name }}</span>
            <span
              >{{ paper.source.imported ? '迁移阅读产物' : 'PDF 证据能力' }}：{{
                paper.source.evidence_status
              }}</span
            >
            <span>阅读修订：{{ paper.reading_version }}</span>
          </template>
          <span v-if="paper.doi">DOI: {{ paper.doi }}</span>
          <span v-if="paper.quartile">JCR {{ paper.quartile }}</span>
          <span>渲染: {{ paper.render?.status || '未渲染' }}</span>
          <span v-for="job in paper.compile_jobs" :key="job.level"
            >{{ job.level }}: {{ job.status }}</span
          >
          <span v-for="job in paper.translation_jobs" :key="`translation-${job.variant_kind}`">
            {{ job.variant_kind }}: {{ job.status }}
          </span>
        </div>
        <div v-if="paper.historical_compile_jobs?.length" class="detail-meta">
          来源已更新，{{ paper.historical_compile_jobs.length }}
          个历史笔记任务保留记录；当前笔记需重新编译。
        </div>

        <div class="detail-actions">
          <button class="btn" :disabled="sessionLoading" @click="startSession">文献专属会话</button>
          <button class="btn" :disabled="rendering" @click="doRender">
            {{ rendering ? '渲染入队中…' : '生成阅读视图' }}
          </button>
          <button class="btn" :disabled="translating" @click="doTranslate">请求翻译</button>
          <button
            v-for="level in ['L1', 'L2', 'L3']"
            :key="level"
            class="btn"
            :disabled="compiling === level"
            @click="doCompile(level)"
          >
            {{ compiling === level ? `${level} 入队中…` : `编译 ${level}` }}
          </button>
          <button class="btn" :disabled="enriching" @click="doEnrich">
            {{ enriching ? '补全中…' : '补全元数据' }}
          </button>
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

        <ReaderTabs
          v-if="currentTab === 'reader'"
          :rid="rid"
          @document-saved="load"
          @open-link="$emit('open-link', $event)"
        />
        <NotePanel
          v-else-if="currentTab === 'notes'"
          :rid="rid"
          :compile-jobs="paper.compile_jobs"
        />
        <AttachmentPanel v-else-if="currentTab === 'attachments'" :rid="rid" />
        <CardsPanel v-else-if="currentTab === 'cards'" :rid="rid" />
        <ReviewPanel v-else-if="currentTab === 'review'" :rid="rid" />
        <WorkflowPanel v-else-if="currentTab === 'workflow'" :rid="rid" />
        <MetadataPanel v-else :rid="rid" @saved="load" />
      </template>
    </div>
  </div>
</template>

<script>
import { ref, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { apiPost } from '@/apis/base'
import {
  getLiteraturePaper,
  triggerLiteratureCompile,
  triggerLiteratureEnrich,
  triggerLiteratureRender,
  triggerLiteratureTranslation
} from '@/apis/literature_api'
import NotePanel from '@/components/literature/NotePanel.vue'
import AttachmentPanel from '@/components/literature/AttachmentPanel.vue'
import CardsPanel from '@/components/literature/CardsPanel.vue'
import ReaderTabs from '@/components/literature/ReaderTabs.vue'
import ReviewPanel from '@/components/literature/ReviewPanel.vue'
import WorkflowPanel from '@/components/literature/WorkflowPanel.vue'
import MetadataPanel from '@/components/literature/MetadataPanel.vue'
import { useUserStore } from '@/stores/user'

const POLL_INTERVAL_MS = 3000

export default {
  name: 'PaperDetail',
  components: {
    NotePanel,
    AttachmentPanel,
    CardsPanel,
    ReviewPanel,
    ReaderTabs,
    WorkflowPanel,
    MetadataPanel
  },
  props: {
    rid: { type: String, required: true },
    embedded: { type: Boolean, default: false }
  },
  emits: ['close', 'session', 'open-link'],
  setup(props, { emit }) {
    const router = useRouter()
    const sessionLoading = ref(false)
    const startSession = async () => {
      sessionLoading.value = true
      try {
        const result = await apiPost(
          `/api/literature/sessions/paper/${encodeURIComponent(props.rid)}`
        )
        if (props.embedded) emit('session', result.thread.id)
        else await router.push(`/agent/${encodeURIComponent(result.thread.id)}`)
      } catch (err) {
        alert(`会话创建失败：${err?.message || err}`)
      } finally {
        sessionLoading.value = false
      }
    }
    const userStore = useUserStore()
    const paper = ref(null)
    const loading = ref(true)
    const rendering = ref(false)
    const translating = ref(false)
    const compiling = ref('')
    const enriching = ref(false)
    const currentTab = ref('reader')
    const tabs = [
      { key: 'reader', label: '阅读' },
      { key: 'notes', label: '笔记 (L1·L2·L3)' },
      { key: 'attachments', label: '依附资料' },
      { key: 'cards', label: '卡片' },
      { key: 'review', label: '识别复核' },
      { key: 'workflow', label: '处理流程' },
      { key: 'overview', label: '元数据' }
    ]
    let pollTimer = null
    let loadGeneration = 0
    let disposed = false

    const load = async () => {
      const ticket = ++loadGeneration
      try {
        const result = await getLiteraturePaper(props.rid)
        if (ticket !== loadGeneration || disposed) return
        paper.value = result
        startPollingIfBusy()
      } catch (error) {
        if (!disposed && ticket === loadGeneration) alert(error?.message || '文献读取失败')
      } finally {
        if (ticket === loadGeneration) loading.value = false
      }
    }

    /** 渲染/编译任务进行中时轮询刷新（组件卸载必须清理） */
    const startPollingIfBusy = () => {
      stopPolling()
      const busy = (paper.value?.compile_jobs || []).some((job) =>
        ['PENDING', 'RUNNING'].includes(job.status)
      )
      const translationBusy = (paper.value?.translation_jobs || []).some((job) =>
        ['PENDING', 'RUNNING'].includes(job.status)
      )
      if (
        busy ||
        translationBusy ||
        ['PENDING', 'RUNNING'].includes(paper.value?.render?.status || '')
      ) {
        pollTimer = setTimeout(load, POLL_INTERVAL_MS)
      }
    }
    const stopPolling = () => {
      if (pollTimer) {
        clearTimeout(pollTimer)
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
    const doCompile = async (level) => {
      compiling.value = level
      try {
        await triggerLiteratureCompile(props.rid, level)
        await load()
        startPollingIfBusy()
      } catch (error) {
        alert(error?.message || `${level} 编译入队失败`)
      } finally {
        compiling.value = ''
      }
    }
    const doEnrich = async () => {
      enriching.value = true
      try {
        await triggerLiteratureEnrich(props.rid)
        await load()
      } catch (error) {
        alert(error?.message || '元数据补全失败')
      } finally {
        enriching.value = false
      }
    }

    watch(
      () => props.rid,
      () => {
        stopPolling()
        paper.value = null
        loading.value = true
        currentTab.value = 'reader'
        load()
      },
      { immediate: true }
    )
    onUnmounted(() => {
      disposed = true
      loadGeneration++
      stopPolling()
    })

    return {
      sessionLoading,
      startSession,
      load,
      paper,
      userStore,
      loading,
      rendering,
      translating,
      compiling,
      enriching,
      currentTab,
      tabs,
      doRender,
      doTranslate,
      doCompile,
      doEnrich
    }
  }
}
</script>

<style lang="less" scoped>
.paper-detail-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.paper-detail-mask.embedded {
  position: static;
  background: transparent;
  display: block;
  min-width: 0;
  .paper-detail {
    width: 100%;
    box-sizing: border-box;
    max-height: none;
  }
}
.paper-detail {
  background: var(--color-bg-container);
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
  color: var(--color-text-secondary);
  margin: 8px 0;
}
.detail-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0;
}
.tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--gray-150);
  margin-bottom: 10px;
}
.tab {
  border: none;
  background: transparent;
  padding: 8px 14px;
  cursor: pointer;
  &.active {
    border-bottom: 2px solid var(--main-color);
    font-weight: 600;
  }
}
.meta-json {
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
