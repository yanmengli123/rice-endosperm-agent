<template>
  <div class="paper-detail-mask" :class="{ embedded }" @click.self="!embedded && close()">
    <div class="paper-detail">
      <div class="detail-header">
        <h3>{{ paper?.title || rid }}</h3>
        <button class="btn close" @click="close">关闭</button>
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
        </div>

        <!-- 进行中任务进度条（任务中心同款阶段口径；数据来自本组件的 3s 轮询） -->
        <div v-if="activeJobs.length" class="job-progress-strip" role="status">
          <span v-for="job in activeJobs" :key="job.key" class="job-chip">
            <span class="job-dot" aria-hidden="true"></span>
            {{ job.label }}
          </span>
          <span class="job-note">进行中的任务会实时推进；完成后此处与任务中心同步消失</span>
        </div>

        <div class="detail-actions">
          <button class="btn" :disabled="sessionLoading" @click="startSession">文献专属会话</button>
          <button class="btn" :disabled="rendering" @click="doRender">
            {{ rendering ? '渲染入队中…' : '生成阅读视图' }}
          </button>
          <button
            class="btn"
            :disabled="enriching || !canEnrich"
            :title="enrichHint"
            @click="doEnrich"
          >
            {{ enriching ? '补全中…' : '补全元数据' }}
          </button>
          <button
            class="btn danger"
            :disabled="trashing"
            title="移入回收站（可恢复软删除；原因写入审计；不删除知识源证据）"
            @click="doTrash"
          >
            {{ trashing ? '回收中…' : '移入回收站' }}
          </button>
        </div>

        <div class="tabs" role="tablist" aria-label="文献详情页签">
          <button
            v-for="tab in tabs"
            :key="tab.key"
            class="tab"
            role="tab"
            :class="{ active: currentTab === tab.key }"
            :data-tab-key="tab.key"
            :id="`detail-tab-${tab.key}`"
            :aria-selected="currentTab === tab.key"
            :aria-controls="`detail-panel-${tab.key}`"
            :tabindex="currentTab === tab.key ? 0 : -1"
            @click="selectTab(tab.key)"
            @keydown="onTabKeydown"
          >
            {{ tab.label }}
          </button>
        </div>

        <ReaderTabs
          v-if="currentTab === 'reader'"
          role="tabpanel"
          :id="'detail-panel-reader'"
          :aria-labelledby="'detail-tab-reader'"
          :reader-target="readerTarget"
          :evidence-target="evidenceTarget"
          :rid="rid"
          :paper="paper"
          :version-token="readerVersion"
          @editing-change="editingChanged"
          @document-saved="load"
          @open-link="$emit('open-link', $event)"
          @assistant-action="$emit('assistant-action', $event)"
        />
        <AttachmentPanel
          v-else-if="currentTab === 'attachments'"
          role="tabpanel"
          id="detail-panel-attachments"
          aria-labelledby="detail-tab-attachments"
          :rid="rid"
        />
        <CardsPanel
          v-else-if="currentTab === 'cards'"
          role="tabpanel"
          id="detail-panel-cards"
          aria-labelledby="detail-tab-cards"
          :rid="rid"
          @editing-change="editingChanged"
          @saved="load"
        />
        <ReviewPanel
          v-else-if="currentTab === 'review'"
          role="tabpanel"
          id="detail-panel-review"
          aria-labelledby="detail-tab-review"
          :rid="rid"
          :version-token="readerVersion"
          @changed="load"
        />
        <MetadataPanel
          v-else
          role="tabpanel"
          id="detail-panel-overview"
          aria-labelledby="detail-tab-overview"
          :rid="rid"
          @saved="load"
          @editing-change="editingChanged"
        />
      </template>
    </div>
  </div>
</template>

<script>
import { computed, ref, onUnmounted, watch } from 'vue'
import { message } from 'ant-design-vue'
import { apiPost } from '@/apis/base'
import {
  getLiteraturePaper,
  literatureWorkspaceApi,
  triggerLiteratureEnrich,
  triggerLiteratureRender
} from '@/apis/literature_api'
import { litConfirm, litPrompt } from '@/utils/litDialogs'
import { useTablistKeyboard } from '@/composables/useTablistKeyboard'
import AttachmentPanel from '@/components/literature/AttachmentPanel.vue'
import CardsPanel from '@/components/literature/CardsPanel.vue'
import ReaderTabs from '@/components/literature/ReaderTabs.vue'
import ReviewPanel from '@/components/literature/ReviewPanel.vue'
import MetadataPanel from '@/components/literature/MetadataPanel.vue'
import { useUserStore } from '@/stores/user'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'

const POLL_INTERVAL_MS = 3000

// 复用已有本文会话：与 LiteratureView.askPaper 同一口径。此前两个入口行为
// 不一致（详情页每次新建、卡片入口先查后建），连点几次即产生多个同名线程。
const reuseSession = async (targetRid) => {
  const existing = await literatureWorkspaceApi.sessions(false)
  return (existing.items || []).find(
    (item) => item.session?.kind === 'paper' && item.session?.rid === targetRid
  )
}

export default {
  name: 'PaperDetail',
  components: {
    AttachmentPanel,
    CardsPanel,
    ReviewPanel,
    ReaderTabs,
    MetadataPanel
  },
  props: {
    rid: { type: String, required: true },
    embedded: { type: Boolean, default: false },
    readerTarget: { type: Object, default: null },
    evidenceTarget: { type: Object, default: null }
  },
  emits: ['close', 'session', 'open-link', 'editing-change', 'assistant-action'],
  setup(props, { emit }) {
    const sessionLoading = ref(false)
    // 文献会话永远留在文献工作台内由宿主切换（左右分栏问答），不再提供
    // 跳转 /agent 原生聊天页的出口——问答历史与阅读上下文必须同屏可见。
    const openSession = (threadId) => {
      emit('session', threadId)
    }
    const startSession = async () => {
      if (sessionLoading.value) return
      sessionLoading.value = true
      try {
        // 已存在本文会话时直接复用，避免连点产生多个同名线程
        // （2026-10-05 实测：连点 3 次即生成 3 个线程）。
        const reuse = await reuseSession(props.rid)
        if (reuse) {
          openSession(reuse.id)
          return
        }
        const result = await apiPost(
          `/api/literature/sessions/paper/${encodeURIComponent(props.rid)}`
        )
        if (!result?.thread?.id) throw new Error('会话建立未返回线程标识')
        openSession(result.thread.id)
      } catch (err) {
        message.error(`会话创建失败：${err?.message || err}`)
      } finally {
        sessionLoading.value = false
      }
    }
    const userStore = useUserStore()
    const paper = ref(null)
    const loading = ref(true)
    const rendering = ref(false)
    const enriching = ref(false)
    // Legacy PDF papers can backfill their own DOI from the authorized body.
    const canEnrich = computed(() => Boolean(paper.value?.doi || paper.value?.source_revision_id))
    const enrichHint = computed(() =>
      canEnrich.value
        ? '按 DOI 从 Crossref / OpenAlex / S2 补全空字段'
        : '请在元数据页填写 DOI 后补全；PDF 文献可先从原文抽取'
    )
    const currentTab = ref('reader')
    const editing = ref(false)
    const editingChanged = (value) => {
      editing.value = value
      emit('editing-change', value)
    }
    // tablist 键盘导航（roving tabindex + 方向键/Home/End），与点击同走 selectTab
    const { onTabKeydown } = useTablistKeyboard(
      (key) => selectTab(key),
      () => tabs.map((tab) => tab.key)
    )
    let tabChangePending = false
    const selectTab = async (key) => {
      if (disposed || tabChangePending || !tabs.some((tab) => tab.key === key)) return false
      if (key === currentTab.value) return true
      const rid = props.rid
      const previous = currentTab.value
      const owner = JSON.stringify([userStore.uid, userStore.departmentId, userStore.isLoggedIn])
      tabChangePending = true
      try {
        if (
          editing.value &&
          !(await litConfirm({
            title: '放弃未保存编辑？',
            content: '切换详情页会放弃未保存编辑。',
            okText: '放弃并切换',
            danger: true
          }))
        )
          return false
        if (
          disposed ||
          props.rid !== rid ||
          currentTab.value !== previous ||
          owner !== JSON.stringify([userStore.uid, userStore.departmentId, userStore.isLoggedIn])
        )
          return false
        editingChanged(false)
        currentTab.value = key
        return true
      } finally {
        tabChangePending = false
      }
    }
    watch(
      () => props.readerTarget,
      (target) => {
        if (target?.rid === props.rid) selectTab('reader')
      }
    )
    const close = async () => {
      const rid = props.rid
      if (
        editing.value &&
        !(await litConfirm({
          title: '放弃未保存编辑？',
          content: '关闭阅读器会放弃未保存编辑。',
          okText: '放弃并关闭',
          danger: true
        }))
      )
        return
      if (disposed || props.rid !== rid) return
      forceClose()
    }
    // 回收等终态动作后的关闭：内容已被处置，无需再确认未保存编辑
    const forceClose = () => {
      editingChanged(false)
      emit('close')
    }
    const readerVersion = computed(() =>
      JSON.stringify({
        source: paper.value?.source_revision_id,
        reading: paper.value?.reading_version,
        cards: paper.value?.card_revision,
        pointers: paper.value?.artifact_pointers,
        render: [
          paper.value?.render?.status,
          paper.value?.render?.markdown_uri,
          paper.value?.render?.render_version
        ]
      })
    )
    const tabs = [
      { key: 'reader', label: '阅读' },
      { key: 'attachments', label: '依附资料' },
      { key: 'cards', label: '卡片' },
      { key: 'review', label: '识别复核' },
      { key: 'overview', label: '元数据' }
    ]
    let pollTimer = null
    let loadGeneration = 0
    let disposed = false
    let loadController = null

    const load = async () => {
      if (disposed || !polling.value || !userStore.isLoggedIn || !userStore.uid) return
      loadController?.abort()
      const controller = new AbortController()
      loadController = controller
      const ticket = ++loadGeneration
      try {
        const result = await getLiteraturePaper(props.rid, controller.signal)
        if (ticket !== loadGeneration || disposed || !polling.value || controller.signal.aborted)
          return
        paper.value = result
        startPollingIfBusy()
      } catch (error) {
        if (!disposed && polling.value && !controller.signal.aborted && ticket === loadGeneration)
          message.error(error?.message || '文献读取失败')
      } finally {
        if (loadController === controller) loadController = null
        if (ticket === loadGeneration && polling.value) loading.value = false
      }
    }

    // 进行中任务汇总（进度条口径与任务中心一致：阶段权重，PENDING=排队/RUNNING=执行中）
    const activeJobs = computed(() => {
      const out = []
      const stageOf = (status) => (status === 'RUNNING' ? '执行中' : '排队中')
      const render = paper.value?.render
      if (render && ['PENDING', 'RUNNING'].includes(render.status || ''))
        out.push({ key: 'render', label: `阅读渲染 · ${stageOf(render.status)}` })
      return out
    })

    /** 渲染任务进行中时轮询刷新（组件卸载必须清理） */
    const startPollingIfBusy = () => {
      stopPolling()
      if (disposed || !polling.value) return
      if (['PENDING', 'RUNNING'].includes(paper.value?.render?.status || '')) {
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
      if (rendering.value) return
      rendering.value = true
      try {
        const result = await triggerLiteratureRender(props.rid)
        if (result?.reused)
          message.info(
            result.status === 'SUCCEEDED'
              ? '已复用现有阅读产物'
              : '阅读渲染正在处理中，已复用现有任务'
          )
        else
          message.success(
            result?.enqueue_deferred ? '阅读任务已保存，等待队列恢复调度' : '阅读任务已入队'
          )
        await load()
        startPollingIfBusy()
      } catch (error) {
        message.error(error?.message || '阅读渲染入队失败')
      } finally {
        rendering.value = false
      }
    }
    const doEnrich = async () => {
      if (enriching.value) return
      if (!canEnrich.value) {
        message.warning('该文献缺少 DOI，无法从外部源补全元数据')
        return
      }
      enriching.value = true
      try {
        const result = await triggerLiteratureEnrich(props.rid)
        if (result && result.ok === false) {
          message.warning(
            `元数据补全未完成：${result.reason || '外部源未返回数据'}（不会写入空数据）`
          )
        } else if (result && Array.isArray(result.filled) && result.filled.length) {
          message.success(`已从外部源补全：${result.filled.join('、')}`)
        } else
          message.info(
            result?.reason === 'meta_locked'
              ? '元数据已人工锁定，未自动改写'
              : '未发现需要补全的空字段'
          )
        await load()
      } catch (error) {
        message.error(error?.message || '元数据补全失败')
      } finally {
        enriching.value = false
      }
    }

    // 手动移入回收站：可恢复软删除（回收站恢复），原因必填进审计；
    // 与清洗共用同一粘性回收语义（顺带修掉上一轮记录的 enrich 静默失败盲区）。
    const trashing = ref(false)
    const doTrash = async () => {
      if (trashing.value) return
      const reason = await litPrompt({
        title: '移入回收站的原因（必填，写入审计）：',
        placeholder: '例如：重复导入 / 元数据有误',
        required: true,
        requiredHint: '必须填写移入回收站的原因（审计要求）',
        okText: '下一步'
      })
      if (!reason) return
      const confirmed = await litConfirm({
        title: '确认移入回收站？',
        content:
          '文献将从目录/检索/图谱隐藏，可在知识库工作台「回收站」恢复。',
        okText: '移入回收站',
        danger: true
      })
      if (!confirmed) return
      trashing.value = true
      try {
        await apiPost(`/api/literature/papers/${encodeURIComponent(props.rid)}/trash`, {
          reason
        })
        forceClose()
      } catch (error) {
        message.error(error?.message || '移入回收站失败')
      } finally {
        trashing.value = false
      }
    }

    const suspend = () => {
      loadGeneration++
      loadController?.abort()
      loadController = null
      stopPolling()
    }
    const polling = useLiteraturePollingScope(suspend, load)
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
    watch(
      () =>
        `${userStore.uid || ''}:${userStore.departmentId ?? ''}:${Boolean(userStore.isLoggedIn)}`,
      () => {
        loadGeneration++
        loadController?.abort()
        stopPolling()
        paper.value = null
        loading.value = true
        if (userStore.isLoggedIn && userStore.uid) load()
      },
      { flush: 'sync' }
    )
    onUnmounted(() => {
      disposed = true
      suspend()
      emit('editing-change', false)
    })

    return {
      sessionLoading,
      startSession,
      load,
      paper,
      userStore,
      loading,
      rendering,
      enriching,
      canEnrich,
      enrichHint,
      currentTab,
      selectTab,
      onTabKeydown,
      close,
      readerVersion,
      editingChanged,
      tabs,
      doRender,
      doEnrich,
      trashing,
      doTrash,
      activeJobs
    }
  }
}
</script>

<style lang="less" scoped>
.job-progress-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 10px;
  margin: 6px 0;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--color-field-bg, #f7f7f2);
  font-size: 12px;
}
.job-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--main-color, #4a7dbe);
  font-weight: 500;
}
.job-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--main-color, #4a7dbe);
  animation: job-pulse 1.6s ease-in-out infinite;
}
@keyframes job-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
.job-note {
  color: var(--color-text-tertiary, #999);
  font-size: 11px;
}
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
