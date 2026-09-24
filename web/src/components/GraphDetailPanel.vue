<template>
  <transition name="slide-fade-left">
    <div class="detail-panel" v-if="visible" @click="handleLinkClick">
      <div class="panel-header">
        <span class="panel-title">{{ title }}</span>
        <X :size="14" class="close-icon" @click="$emit('close')" />
      </div>
      <div class="panel-body">
        <template v-if="item">
          <template v-if="type === 'node'">
            <div :class="rowClass(item.data?.label)">
              <span class="detail-label">名称</span>
              <span class="detail-value">
                <DetailValue
                  :value="item.data?.label"
                  field-key="__label__"
                  :expanded-keys="expandedKeys"
                />
              </span>
            </div>
            <div class="detail-row">
              <span class="detail-label">ID</span>
              <span class="detail-value detail-id">{{ item.id }}</span>
            </div>
            <template v-if="item.data?.original?.labels">
              <div class="detail-row">
                <span class="detail-label">标签</span>
                <span class="detail-value">
                  <a-tag v-for="tag in item.data.original.labels" :key="tag" size="small">{{
                    tag
                  }}</a-tag>
                </span>
              </div>
            </template>
            <template v-if="item.data?.original?.properties">
              <div
                v-for="(value, key) in item.data.original.properties"
                :key="key"
                :class="rowClass(value)"
              >
                <span class="detail-label">{{ key }}</span>
                <span class="detail-value">
                  <DetailValue :value="value" :field-key="key" :expanded-keys="expandedKeys" />
                </span>
              </div>
            </template>
          </template>
          <template v-else-if="type === 'edge'">
            <div :class="rowClass(item.data?.label)">
              <span class="detail-label">类型</span>
              <span class="detail-value">
                <DetailValue
                  :value="item.data?.label"
                  field-key="__type__"
                  :expanded-keys="expandedKeys"
                />
              </span>
            </div>
            <template v-if="item.data?.original?.properties">
              <div
                v-for="(value, key) in filteredEdgeProperties"
                :key="key"
                :class="rowClass(value)"
              >
                <span class="detail-label">{{ key }}</span>
                <span class="detail-value">
                  <DetailValue :value="value" :field-key="key" :expanded-keys="expandedKeys" />
                </span>
              </div>
            </template>
          </template>

          <!-- 原文证据：图谱里每个节点/边点开即见原文语句（PostgreSQL mention 表，显示时逐条重验） -->
          <div class="evidence-section">
            <div class="evidence-header">
              <span class="evidence-title">原文证据</span>
              <a-tag
                v-if="evidence?.review_status"
                size="small"
                :color="reviewStatusColor(evidence.review_status)"
              >
                {{ reviewStatusLabel(evidence.review_status) }}
              </a-tag>
              <a-tag
                v-if="evidence?.trust_tier"
                size="small"
                :color="trustColor(evidence.trust_tier)"
                :title="trustHint(evidence.trust_tier)"
              >
                {{ trustLabel(evidence.trust_tier) }}
              </a-tag>
            </div>
            <div v-if="evidenceLoading" class="evidence-hint">加载原文中…</div>
            <div v-else-if="evidenceError" class="evidence-hint evidence-hint--error">
              {{ evidenceError }}
            </div>
            <template v-else-if="evidence">
              <div v-if="evidence.decision?.reason" class="evidence-hint decision-reason">
                最近决策：{{ historyActionLabel(evidence.decision.action) }}（{{
                  evidence.decision.actor_uid
                }}）—
                {{ evidence.decision.reason }}
              </div>
              <div v-if="type === 'edge'" class="evidence-meta">
                佐证 {{ evidence.support_count }} 处 · 文献 {{ evidence.literature_count }} 篇
                <span v-if="evidence.verification_summary?.DEGRADED" class="evidence-warn">
                  · {{ evidence.verification_summary.DEGRADED }} 条引文与当前原文不一致
                </span>
              </div>
              <div v-else class="evidence-meta">
                出现 {{ evidence.mention_count }} 处 · 文件 {{ evidence.file_count }} 个 · 关系
                {{ evidence.triple_count }} 条
              </div>

              <div v-if="type === 'node' && evidence.definition" class="evidence-definition">
                <div class="evidence-label">定义语句</div>
                <div class="mention-quote">
                  <QuoteHighlight :text="evidence.definition.quote" :quote="item.data?.label" />
                </div>
                <div class="evidence-source">{{ sourceLine(evidence.definition) }}</div>
              </div>
              <div v-if="type === 'node' && evidence.aliases?.length" class="evidence-aliases">
                <span class="evidence-label">别名</span>
                <a-tag v-for="alias in evidence.aliases" :key="alias" size="small">{{
                  alias
                }}</a-tag>
              </div>

              <div v-if="!evidence.mentions?.length" class="evidence-hint">
                该元素没有原文引文记录
              </div>
              <div
                v-for="(mention, index) in evidence.mentions"
                :key="mentionKey(mention, index)"
                class="evidence-mention"
                :class="{ 'evidence-mention--selected': isPinnedSelected(mention) }"
              >
                <div class="mention-badges">
                  <a-radio
                    v-if="mention.quote && reviewStatus !== 'CANONICAL'"
                    :checked="isPinnedSelected(mention)"
                    :title="`选择该引文作为本次人工决策的固定证据（${mention.verification}）`"
                    class="mention-pin-radio"
                    @change="selectedPinnedChunkId = mention.chunk_id"
                  >
                    固定此引文
                  </a-radio>
                  <a-tag v-if="mention.verification !== 'OK'" size="small" color="orange">
                    {{ verificationLabel(mention.verification) }}
                  </a-tag>
                  <a-tag v-if="mention.hedge" size="small" color="gold">推测性表述</a-tag>
                  <a-tag v-if="mention.trigger_verified" size="small" color="cyan"
                    >机器校验·触发词</a-tag
                  >
                  <a-tag v-if="mention.verifier_confirmed" size="small" color="cyan"
                    >机器校验·双模型复核</a-tag
                  >
                  <a-tag v-if="mention.pinned_by" size="small" color="blue">已固定</a-tag>
                  <span v-if="typeof mention.confidence === 'number'" class="mention-confidence">
                    置信度 {{ mention.confidence.toFixed(2) }}
                  </span>
                </div>
                <div v-if="mention.quote" class="mention-quote">
                  <QuoteHighlight
                    :text="
                      expandedMentions.has(mentionKey(mention, index))
                        ? mention.chunk_content
                        : mention.context || mention.quote
                    "
                    :quote="mention.quote"
                  />
                </div>
                <div v-else class="evidence-hint">旧数据无引文，重置图谱后重新构建即可回填</div>
                <div class="evidence-source">
                  <span>{{ sourceLine(mention) }}</span>
                  <span class="source-actions">
                    <a
                      v-if="mention.quote && mention.chunk_content"
                      class="expand-link"
                      @click.prevent="toggleMention(mentionKey(mention, index))"
                    >
                      {{
                        expandedMentions.has(mentionKey(mention, index))
                          ? '收起段落'
                          : '显示完整段落'
                      }}
                    </a>
                    <a
                      class="expand-link"
                      title="清除该段落未固定的图元素并重新抽取（已有决策不受影响）"
                      @click.prevent="reextractChunk(mention)"
                    >
                      ↻ 重抽此段落
                    </a>
                  </span>
                </div>
              </div>
            </template>
          </div>

          <!-- 人工审核：决策叠加层，reset/重抽后自动重放 -->
          <div v-if="reviewTargetId && evidence" class="review-section">
            <div class="evidence-header">
              <span class="evidence-title">审核操作</span>
            </div>
            <div v-if="reviewStatus === 'CANONICAL'" class="evidence-hint">
              规范层（托管导入）对象只读。
            </div>
            <div v-else class="review-actions">
              <a-button
                v-if="reviewStatus !== 'APPROVED'"
                size="small"
                type="primary"
                :loading="reviewBusy"
                @click="approveTarget"
              >
                ✓ 人工批准（固定所选引文）
              </a-button>
              <a-button size="small" :loading="reviewBusy" class="review-btn" @click="openEdit">
                ✎ 编辑
              </a-button>
              <a-button
                v-if="type === 'node'"
                size="small"
                :loading="reviewBusy"
                class="review-btn"
                @click="openAddRelation"
              >
                ＋ 补关系
              </a-button>
              <a-button
                v-if="reviewStatus !== 'REJECTED'"
                size="small"
                danger
                :loading="reviewBusy"
                class="review-btn"
                @click="rejectOpen = true"
              >
                ✗ {{ reviewStatus === 'APPROVED' ? '撤销批准' : '人工拒绝' }}
              </a-button>
            </div>
            <div
              v-if="reviewStatus !== 'APPROVED' && evidence?.mentions?.some((m) => m.quote)"
              class="pin-selection-hint"
            >
              将固定证据：
              <a-tag v-if="selectedPinnedMention" size="small" color="blue">
                {{ selectedPinnedMention.filename || selectedPinnedMention.chunk_id }}
                {{ selectedPinnedMention.verification === 'OK' ? '' : '（引文非 OK，建议换一条）' }}
              </a-tag>
              <span v-else>无可固定的引文</span>
            </div>
          </div>

          <div v-if="history.length" class="history-section">
            <div class="evidence-header">
              <span class="evidence-title">操作历史</span>
            </div>
            <div v-for="entry in history" :key="entry.id" class="history-entry">
              <span class="history-time">{{ formatTime(entry.created_at) }}</span>
              <span class="history-text">
                {{ entry.actor_uid }} · {{ historyActionLabel(entry.action) }}
                <template v-if="entry.reason"> — {{ entry.reason }}</template>
              </span>
            </div>
          </div>
        </template>
      </div>

      <!-- 拒绝理由（必填，审计可查） -->
      <a-modal
        v-model:open="rejectOpen"
        :title="reviewStatus === 'APPROVED' ? '撤销批准（填写理由）' : '人工拒绝（填写理由）'"
        :confirm-loading="reviewBusy"
        :ok-type="reviewStatus === 'APPROVED' ? 'default' : 'danger'"
        ok-text="确认"
        @ok="rejectTarget"
      >
        <a-form layout="vertical">
          <a-form-item label="原因代码（结构化，便于统计检索）">
            <a-select
              v-model:value="rejectReasonCode"
              :options="reasonCodeOptions"
              allow-clear
              placeholder="选择原因代码（可选）"
            />
          </a-form-item>
          <a-form-item label="说明（必填）">
            <a-textarea
              v-model:value="rejectReason"
              :rows="3"
              placeholder="必填：如「方向反了，句子说的是 B 抑制 A」"
            />
          </a-form-item>
        </a-form>
      </a-modal>

      <!-- 乐观并发冲突：他人已更新该对象（409），展示最新决策而非只报错 -->
      <a-modal
        v-model:open="conflictModal.open"
        title="该项已被其他审核人更新"
        ok-text="加载最新后重试"
        cancel-text="关闭"
        @ok="reloadAfterConflict"
      >
        <a-alert
          type="warning"
          show-icon
          :message="conflictModal.detail"
          style="margin-bottom: 10px"
        />
        <template v-if="conflictModal.latestAudit">
          <div class="conflict-latest">
            <div class="conflict-latest-title">对方最新的决定：</div>
            <div>
              {{ formatTime(conflictModal.latestAudit.created_at) }} ·
              {{ conflictModal.latestAudit.actor_uid }} ·
              {{ historyActionLabel(conflictModal.latestAudit.action) }}
              <template v-if="conflictModal.latestAudit.reason">
                — {{ conflictModal.latestAudit.reason }}
              </template>
            </div>
          </div>
        </template>
        <div v-else class="conflict-latest">未能拉取到最新决策记录，可直接重载证据后重试。</div>
      </a-modal>

      <!-- 编辑：边 = SUPERSEDE（旧 ID 自动拒绝，新 ID 验证）；节点 = 展示覆盖（不改身份） -->
      <a-modal
        v-model:open="editOpen"
        :title="type === 'edge' ? '编辑关系' : '编辑实体'"
        :confirm-loading="reviewBusy"
        ok-text="保存"
        @ok="submitEdit"
      >
        <template v-if="type === 'edge'">
          <a-form layout="vertical">
            <a-form-item label="关系类型（闭集词表）">
              <a-select
                v-model:value="editForm.relation_type"
                show-search
                :options="relationOptions"
                placeholder="选择谓词"
              />
            </a-form-item>
            <a-form-item label="方向">
              <a-radio-group v-model:value="editForm.reverse">
                <a-radio :value="false"
                  >正向（{{ evidence?.source?.name }} → {{ evidence?.target?.name }}）</a-radio
                >
                <a-radio :value="true"
                  >反向（{{ evidence?.target?.name }} → {{ evidence?.source?.name }}）</a-radio
                >
              </a-radio-group>
            </a-form-item>
            <a-form-item label="备注（审计可查）">
              <a-input v-model:value="editForm.note" placeholder="如「方向反了，已修正」" />
            </a-form-item>
          </a-form>
          <a-alert
            type="info"
            show-icon
            message="保存后旧关系自动标记为「已拒绝（被取代）」，新关系立即验证；同一句话再抽出旧关系时会自动保持拒绝。"
          />
        </template>
        <template v-else>
          <a-form layout="vertical">
            <a-form-item label="显示名（RENAME，不改身份）">
              <a-input v-model:value="editForm.display_name" placeholder="留空则不修改" />
            </a-form-item>
            <a-form-item label="实体类型（RETYPE，不改身份）">
              <a-select
                v-model:value="editForm.label"
                show-search
                allow-clear
                :options="entityOptions"
                placeholder="留空则不修改"
              />
            </a-form-item>
            <a-form-item label="补充别名（逗号分隔）">
              <a-input
                v-model:value="editForm.aliases"
                placeholder="如 OsCIN2, GRAIN INCOMPLETE FILLING 1"
              />
            </a-form-item>
          </a-form>
        </template>
      </a-modal>

      <!-- 补关系：手动新增（引文必须是所选段落原文的逐字子串，直接验证） -->
      <a-modal
        v-model:open="addOpen"
        title="补充关系（手动）"
        :confirm-loading="reviewBusy"
        ok-text="创建并验证"
        width="640px"
        @ok="submitAddRelation"
      >
        <a-form layout="vertical">
          <a-form-item label="对端实体（{{ evidence?.name }} 作为 subject）">
            <a-select
              v-model:value="addForm.target_entity_id"
              show-search
              :filter-option="false"
              :options="addCandidates"
              placeholder="输入名称搜索实体"
              @search="searchEntities"
            />
          </a-form-item>
          <a-form-item label="关系类型（闭集词表）">
            <a-select
              v-model:value="addForm.relation_type"
              show-search
              :options="relationOptions"
              placeholder="选择谓词"
            />
          </a-form-item>
          <a-form-item label="证据来源段落">
            <a-select v-model:value="addForm.chunk_id" @change="onAddChunkChange">
              <a-select-option
                v-for="m in evidence?.mentions || []"
                :key="m.chunk_id"
                :value="m.chunk_id"
              >
                {{ m.filename }}{{ m.section ? ` · ${m.section}` : '' }} · {{ m.chunk_id }}
              </a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item label="原文引文（逐字复制所选段落中的句子；可在上方展开段落复制）">
            <a-textarea v-model:value="addForm.evidence_quote" :rows="3" />
            <a class="expand-link" @click.prevent="fillQuoteFromChunk">用该段落的第一句</a>
          </a-form-item>
        </a-form>
      </a-modal>
    </div>
  </transition>
</template>

<script setup>
import { computed, reactive, ref, watch, defineComponent, h } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { X } from '@lucide/vue'
import { graphApi } from '@/apis/graph_api'
import {
  REASON_CODES,
  REVIEW_STATUS_META,
  TRUST_META,
  auditActionLabel,
  composeReason,
  reviewStatusColor,
  reviewStatusLabel,
  trustColor,
  trustHint,
  trustLabel
} from '@/utils/graph/reviewMeta'

const STACK_THRESHOLD = 50
const TRUNCATE_LIMIT = 100

const DetailValue = defineComponent({
  props: {
    value: [String, Number, Boolean, Object, Array],
    fieldKey: { type: String, required: true },
    expandedKeys: { type: Set, required: true }
  },
  setup(props) {
    return () => {
      const v = props.value
      if (typeof v !== 'string') return String(v ?? '')
      if (v.length <= TRUNCATE_LIMIT) return v
      if (props.expandedKeys.has(props.fieldKey)) {
        return [
          v,
          h(
            'a',
            {
              class: 'expand-link',
              onClick: (e) => {
                e.preventDefault()
                props.expandedKeys.delete(props.fieldKey)
              }
            },
            ' 收起'
          )
        ]
      }
      return [
        v.slice(0, TRUNCATE_LIMIT) + '...',
        h(
          'a',
          {
            class: 'expand-link',
            onClick: (e) => {
              e.preventDefault()
              props.expandedKeys.add(props.fieldKey)
            }
          },
          '展开'
        )
      ]
    }
  }
})

// 在文本中高亮逐字引文（首次出现）；找不到时原样显示
const QuoteHighlight = defineComponent({
  props: {
    text: { type: String, default: '' },
    quote: { type: String, default: '' }
  },
  setup(props) {
    return () => {
      const text = props.text || ''
      const quote = props.quote || ''
      const index = quote ? text.indexOf(quote) : -1
      if (index < 0) return h('span', { class: 'quote-text' }, text)
      return h('span', { class: 'quote-text' }, [
        text.slice(0, index),
        h('mark', { class: 'quote-mark' }, quote),
        text.slice(index + quote.length)
      ])
    }
  }
})

const props = defineProps({
  visible: Boolean,
  item: Object,
  type: String,
  kbId: String
})

const emit = defineEmits(['close', 'reviewed'])

const expandedKeys = reactive(new Set())
const expandedMentions = reactive(new Set())
const evidence = ref(null)
const evidenceLoading = ref(false)
const evidenceError = ref(null)
const history = ref([])
const reviewBusy = ref(false)
const rejectOpen = ref(false)
const rejectReason = ref('')
const editOpen = ref(false)
const editForm = reactive({
  relation_type: null,
  reverse: false,
  note: '',
  display_name: '',
  label: null,
  aliases: ''
})
const addOpen = ref(false)
const addForm = reactive({
  target_entity_id: null,
  relation_type: null,
  chunk_id: null,
  evidence_quote: ''
})
const addCandidates = ref([])
const vocabulary = ref({ entity_types: [], relation_types: [] })

const historyActionLabel = auditActionLabel
const verificationLabel = (status) =>
  status === 'DEGRADED' ? '引文与当前原文不一致' : status === 'MISSING' ? '无引文' : status
const mentionKey = (mention, index) => `${mention.chunk_id || 'chunk'}-${index}`
const toggleMention = (key) => {
  if (expandedMentions.has(key)) expandedMentions.delete(key)
  else expandedMentions.add(key)
}
const sourceLine = (mention) =>
  [
    mention.filename,
    mention.section,
    mention.literature,
    mention.page ? `第 ${mention.page} 页` : null
  ]
    .filter(Boolean)
    .join(' · ')
const formatTime = (iso) => (iso ? new Date(iso).toLocaleString() : '')

const reviewTargetId = computed(() => {
  const properties = props.item?.data?.original?.properties || {}
  return props.type === 'edge' ? properties.triple_id : properties.entity_id
})
const reviewStatus = computed(() => evidence.value?.review_status)
const reviewKind = computed(() => (props.type === 'edge' ? 'TRIPLE' : 'ENTITY'))
const relationOptions = computed(() =>
  (vocabulary.value.relation_types || []).map((t) => ({ value: t, label: t }))
)
const entityOptions = computed(() =>
  (vocabulary.value.entity_types || []).map((t) => ({ value: t, label: t }))
)
const firstQuotedMention = computed(() => evidence.value?.mentions?.find((m) => m.quote) || null)
// 显式证据选择：批准前必须看到将固定哪条引文（默认最优：已固定 > 排序最前的 OK 引文）
const selectedPinnedChunkId = ref(null)
const reasonCodeOptions = REASON_CODES.map((item) => ({ value: item.value, label: item.label }))
const rejectReasonCode = ref(null)
const conflictModal = reactive({ open: false, detail: '', latestAudit: null })

const defaultPinnedChunkId = (mentions) => {
  const withQuote = (mentions || []).filter((m) => m.quote)
  const pinned = withQuote.find((m) => m.pinned_by && m.verification === 'OK')
  if (pinned) return pinned.chunk_id
  const ok = withQuote.find((m) => m.verification === 'OK')
  return ok?.chunk_id || withQuote[0]?.chunk_id || null
}

const selectedPinnedMention = computed(
  () => evidence.value?.mentions?.find((m) => m.chunk_id === selectedPinnedChunkId.value) || null
)
const isPinnedSelected = (mention) =>
  Boolean(mention.chunk_id && mention.chunk_id === selectedPinnedChunkId.value)
const selectedAddMention = computed(
  () => evidence.value?.mentions?.find((m) => m.chunk_id === addForm.chunk_id) || null
)

const loadEvidence = async () => {
  evidence.value = null
  evidenceError.value = null
  expandedMentions.clear()
  if (!props.visible || !props.item || !props.kbId) return
  if (!reviewTargetId.value) {
    evidenceError.value =
      props.type === 'edge'
        ? '该边没有 triple_id，不是抽取图谱的关系边'
        : '该节点不是实体节点，没有原文引文'
    return
  }
  evidenceLoading.value = true
  try {
    const response =
      props.type === 'edge'
        ? await graphApi.getTripleEvidence(props.kbId, reviewTargetId.value)
        : await graphApi.getEntityEvidence(props.kbId, reviewTargetId.value)
    evidence.value = response?.data || null
    selectedPinnedChunkId.value = defaultPinnedChunkId(evidence.value?.mentions)
  } catch (e) {
    evidenceError.value = e?.response?.data?.detail || e?.message || '原文加载失败'
  } finally {
    evidenceLoading.value = false
  }
}

const loadHistory = async () => {
  history.value = []
  if (!props.visible || !reviewTargetId.value || !props.kbId) return
  try {
    const res = await graphApi.reviewAudit({
      kb_id: props.kbId,
      target_id: reviewTargetId.value,
      limit: 20
    })
    history.value = res?.data?.items || []
  } catch {
    history.value = []
  }
}

/** 乐观并发冲突（409）：展示他人最新决策并提供一键重载，而不是只弹错误 */
const handleReviewError = async (e, fallback) => {
  if (e?.response?.status === 409) {
    conflictModal.detail = e?.response?.data?.detail || '该项已被其他审核人更新'
    conflictModal.latestAudit = null
    conflictModal.open = true
    try {
      const res = await graphApi.reviewAudit({
        kb_id: props.kbId,
        target_id: reviewTargetId.value,
        limit: 1
      })
      conflictModal.latestAudit = res?.data?.items?.[0] || null
    } catch {
      /* 最新决策拉取失败不阻塞提示 */
    }
    return
  }
  message.error(e?.response?.data?.detail || e?.message || fallback)
}

const reloadAfterConflict = async () => {
  conflictModal.open = false
  await loadEvidence()
  await loadHistory()
}

const ensureVocabulary = async () => {
  if (vocabulary.value.relation_types.length) return
  try {
    const res = await graphApi.getVocabulary()
    vocabulary.value = res?.data || vocabulary.value
  } catch {
    /* 词表加载失败时编辑表单下拉为空，操作仍可输入 */
  }
}

const afterReview = async (successMessage) => {
  message.success(successMessage)
  await loadEvidence()
  await loadHistory()
  emit('reviewed')
}

const approveTarget = async () => {
  reviewBusy.value = true
  try {
    const res = await graphApi.reviewApprove({
      kb_id: props.kbId,
      target_kind: reviewKind.value,
      target_id: reviewTargetId.value,
      pinned_chunk_id: selectedPinnedChunkId.value || firstQuotedMention.value?.chunk_id,
      if_version: evidence.value?.review_version || undefined
    })
    await afterReview(
      res?.data?.unchanged
        ? '已是人工批准状态（幂等，未重复记录）'
        : '已人工批准：决策与 pinned 证据已记录，重抽/重建后自动恢复'
    )
  } catch (e) {
    await handleReviewError(e, '批准失败')
  } finally {
    reviewBusy.value = false
  }
}

const rejectTarget = async () => {
  if (!rejectReason.value.trim()) {
    message.warning('拒绝必须填写理由')
    return
  }
  reviewBusy.value = true
  try {
    await graphApi.reviewReject({
      kb_id: props.kbId,
      target_kind: reviewKind.value,
      target_id: reviewTargetId.value,
      reason: composeReason(rejectReasonCode.value, rejectReason.value),
      if_version: evidence.value?.review_version || undefined
    })
    rejectOpen.value = false
    rejectReason.value = ''
    rejectReasonCode.value = null
    await afterReview('已人工拒绝：图上投影与向量已清理，审计可查')
  } catch (e) {
    await handleReviewError(e, '拒绝失败')
  } finally {
    reviewBusy.value = false
  }
}

const openEdit = async () => {
  await ensureVocabulary()
  editForm.relation_type = evidence.value?.relation_type || null
  editForm.reverse = false
  editForm.note = ''
  editForm.display_name = ''
  editForm.label = null
  editForm.aliases = ''
  editOpen.value = true
}

const submitEdit = async () => {
  reviewBusy.value = true
  try {
    if (props.type === 'edge') {
      const payload = {
        kb_id: props.kbId,
        triple_id: reviewTargetId.value,
        note: editForm.note || undefined,
        if_version: evidence.value?.review_version || undefined
      }
      if (editForm.relation_type && editForm.relation_type !== evidence.value?.relation_type) {
        payload.relation_type = editForm.relation_type
      }
      if (editForm.reverse) payload.reverse = true
      const res = await graphApi.reviewEditTriple(payload)
      await afterReview(
        `已编辑：旧关系自动拒绝，新关系 ${res?.data?.new_triple_id?.slice(0, 8) || ''}… 已验证`
      )
    } else {
      const payload = {
        kb_id: props.kbId,
        entity_id: reviewTargetId.value,
        if_version: evidence.value?.review_version || undefined
      }
      if (editForm.display_name.trim()) payload.display_name = editForm.display_name.trim()
      if (editForm.label) payload.label = editForm.label
      const aliases = editForm.aliases
        .split(/[,，]/)
        .map((a) => a.trim())
        .filter(Boolean)
      if (aliases.length) payload.aliases = aliases
      await graphApi.reviewEditEntity(payload)
      await afterReview('已更新展示覆盖（身份不变），别名已入表')
    }
    editOpen.value = false
  } catch (e) {
    await handleReviewError(e, '编辑失败')
  } finally {
    reviewBusy.value = false
  }
}

const openAddRelation = async () => {
  await ensureVocabulary()
  addForm.target_entity_id = null
  addForm.relation_type = null
  addForm.chunk_id = firstQuotedMention.value?.chunk_id || null
  addForm.evidence_quote = ''
  addCandidates.value = []
  addOpen.value = true
}

const searchEntities = async (keyword) => {
  if (!keyword || keyword.length < 2) {
    addCandidates.value = []
    return
  }
  try {
    const res = await graphApi.getSubgraph({
      kb_id: props.kbId,
      node_label: keyword,
      max_depth: 0,
      max_nodes: 10,
      exclude_chunk: true
    })
    addCandidates.value = (res?.data?.nodes || [])
      .filter((node) => node.type !== 'Chunk' && node.properties?.entity_id)
      .map((node) => ({
        value: node.properties.entity_id,
        label: `${node.name}（${node.properties.label || node.type}）`
      }))
  } catch {
    addCandidates.value = []
  }
}

const onAddChunkChange = () => {
  addForm.evidence_quote = ''
}

const fillQuoteFromChunk = () => {
  const mention = selectedAddMention.value
  if (mention?.quote) {
    addForm.evidence_quote = mention.quote
  } else if (mention?.chunk_content) {
    addForm.evidence_quote = mention.chunk_content.split(/(?<=[.。！？!?])\s/)[0]?.trim() || ''
  }
}

const submitAddRelation = async () => {
  reviewBusy.value = true
  try {
    const res = await graphApi.reviewAddTriple({
      kb_id: props.kbId,
      source_entity_id: reviewTargetId.value,
      target_entity_id: addForm.target_entity_id,
      relation_type: addForm.relation_type,
      chunk_id: addForm.chunk_id,
      evidence_quote: addForm.evidence_quote,
      note: 'manual add from graph panel'
    })
    addOpen.value = false
    await afterReview(
      `已创建并验证（${res?.data?.triple_id?.slice(0, 8) || ''}…），重抽/重建后自动恢复`
    )
  } catch (e) {
    message.error(
      e?.response?.data?.detail || e?.message || '补关系失败（引文必须是所选段落原文的逐字子串）'
    )
  } finally {
    reviewBusy.value = false
  }
}

const reextractChunk = async (mention) => {
  if (!mention?.chunk_id) return
  reviewBusy.value = true
  try {
    const res = await graphApi.reviewReextract({ kb_id: props.kbId, chunk_id: mention.chunk_id })
    message.success(
      `重抽任务已提交（task ${res?.data?.task_id || ''}）：已固定的证据保留，已有决策会自动重放`
    )
    emit('reviewed')
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '重抽提交失败')
  } finally {
    reviewBusy.value = false
  }
}

watch(
  () => props.item,
  () => {
    expandedKeys.clear()
  }
)

watch([() => props.item, () => props.visible, () => props.kbId], loadEvidence, { immediate: true })
watch([() => props.item, () => props.visible], loadHistory, { immediate: true })

const isOverThreshold = (value) => typeof value === 'string' && value.length > STACK_THRESHOLD

const rowClass = (value) => {
  return isOverThreshold(value) ? 'detail-row detail-row--stack' : 'detail-row'
}

const title = computed(() => {
  return props.type === 'node' ? '节点详情' : '关系详情'
})

const filteredEdgeProperties = computed(() => {
  if (!props.item?.data?.original?.properties) return {}
  const properties = props.item.data.original.properties
  const filtered = {}
  const hiddenFields = ['source_id', 'target_id', '_id', 'truncate', 'review_status']
  Object.keys(properties).forEach((key) => {
    if (!hiddenFields.includes(key)) filtered[key] = properties[key]
  })
  return filtered
})
</script>

<style scoped lang="less">
.detail-panel {
  position: absolute;
  top: 60px;
  left: 10px;
  width: 380px;
  max-height: calc(100% - 60px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;
  user-select: auto;

  .panel-header {
    display: flex;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);

    .panel-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-1000);
    }

    .close-icon {
      margin-left: auto;
      cursor: pointer;
      color: var(--gray-500);
      transition: color 0.2s;

      &:hover {
        color: var(--gray-800);
      }
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.detail-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 6px 0;
  border-bottom: 1px solid var(--gray-50);

  &:last-child {
    border-bottom: none;
  }

  &--stack {
    flex-direction: column;
  }

  .detail-label {
    flex-shrink: 0;
    color: var(--gray-500);
    font-size: 12px;
    margin-right: 8px;
    margin-bottom: 2px;
  }

  .detail-value {
    text-align: right;
    color: var(--gray-800);
    font-size: 13px;
    word-break: break-all;

    &.detail-id {
      font-size: 11px;
      color: var(--gray-500);
    }

    :deep(.expand-link) {
      cursor: pointer;
      color: var(--main-700);
      font-size: 12px;
      white-space: nowrap;
      margin-left: 2px;

      &:hover {
        color: var(--main-500);
      }
    }
  }
}

.detail-row--stack {
  .detail-value {
    text-align: left;
  }
}

.evidence-section,
.review-section,
.history-section {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--gray-200);

  .evidence-header {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;

    .evidence-title {
      font-size: 12px;
      font-weight: 600;
      color: var(--gray-900);
    }
  }

  .evidence-meta {
    font-size: 12px;
    color: var(--gray-600);
    margin-bottom: 6px;

    .evidence-warn {
      color: var(--color-warning, #d48806);
    }
  }

  .evidence-hint {
    font-size: 12px;
    color: var(--gray-500);
    padding: 4px 0;

    &--error {
      color: var(--color-error, #cf1322);
    }
  }

  .decision-reason {
    color: var(--gray-600);
  }

  .evidence-label {
    font-size: 11px;
    color: var(--gray-500);
    margin-bottom: 2px;
  }

  .evidence-definition {
    padding: 6px 8px;
    margin-bottom: 6px;
    border-radius: 6px;
    background: var(--gray-25, var(--gray-50));
  }

  .evidence-aliases {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    margin-bottom: 6px;
  }

  .evidence-mention {
    padding: 6px 0;
    border-top: 1px dashed var(--gray-100);

    .mention-badges {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px;
      margin-bottom: 4px;

      .mention-confidence {
        font-size: 11px;
        color: var(--gray-500);
      }
    }
  }

  .mention-quote {
    font-size: 13px;
    line-height: 1.55;
    color: var(--gray-900);
    white-space: pre-wrap;
    word-break: break-word;

    :deep(.quote-mark) {
      background: var(--main-100, #fff1b8);
      color: inherit;
      padding: 0 1px;
      border-radius: 2px;
    }
  }

  .evidence-source {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    margin-top: 3px;
    font-size: 11px;
    color: var(--gray-500);

    .source-actions {
      display: flex;
      gap: 8px;
      white-space: nowrap;
    }

    .expand-link {
      cursor: pointer;
      color: var(--main-700);

      &:hover {
        color: var(--main-500);
      }
    }
  }
}

.review-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;

  .review-btn {
    margin-left: 0;
  }
}

.pin-selection-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--gray-600);
}

.evidence-mention {
  &--selected {
    background: var(--main-color-bg-hover, rgba(22, 119, 255, 0.08));
    border-radius: 6px;
    padding: 4px 6px;
    margin: 0 -6px;
  }

  .mention-pin-radio {
    margin-right: 4px;
    font-size: 12px;
  }
}

.conflict-latest {
  font-size: 13px;
  color: var(--gray-700);
  line-height: 1.6;

  .conflict-latest-title {
    font-weight: 600;
    color: var(--gray-900);
    margin-bottom: 4px;
  }
}

.history-entry {
  display: flex;
  gap: 8px;
  padding: 3px 0;
  font-size: 11px;
  color: var(--gray-600);

  .history-time {
    flex-shrink: 0;
    color: var(--gray-400);
  }

  .history-text {
    word-break: break-all;
  }
}

.slide-fade-left-enter-active {
  transition: all 0.25s ease-out;
}

.slide-fade-left-leave-active {
  transition: all 0.2s cubic-bezier(1, 0.5, 0.8, 1);
}

.slide-fade-left-enter-from,
.slide-fade-left-leave-to {
  transform: translateX(-20px);
  opacity: 0;
}
</style>
