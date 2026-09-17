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
              >
                <div class="mention-badges">
                  <a-tag v-if="mention.verification !== 'OK'" size="small" color="orange">
                    {{ verificationLabel(mention.verification) }}
                  </a-tag>
                  <a-tag v-if="mention.hedge" size="small" color="gold">推测性表述</a-tag>
                  <a-tag v-if="mention.trigger_verified" size="small" color="green"
                    >触发词验证</a-tag
                  >
                  <a-tag v-if="mention.verifier_confirmed" size="small" color="green"
                    >双模型复核</a-tag
                  >
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
                  <a
                    v-if="mention.quote && mention.chunk_content"
                    class="expand-link"
                    @click.prevent="toggleMention(mentionKey(mention, index))"
                  >
                    {{
                      expandedMentions.has(mentionKey(mention, index)) ? '收起段落' : '显示完整段落'
                    }}
                  </a>
                </div>
              </div>
            </template>
          </div>
        </template>
      </div>
    </div>
  </transition>
</template>

<script setup>
import { computed, reactive, ref, watch, defineComponent, h } from 'vue'
import { X } from '@lucide/vue'
import { graphApi } from '@/apis/graph_api'

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

defineEmits(['close'])

const expandedKeys = reactive(new Set())
const expandedMentions = reactive(new Set())
const evidence = ref(null)
const evidenceLoading = ref(false)
const evidenceError = ref(null)

const TRUST_META = {
  VERIFIED_CORROBORATED: {
    label: '已验证 · 多文献',
    color: 'green',
    hint: '触发词或双模型验证通过，且有 ≥2 篇文献佐证'
  },
  VERIFIED_SINGLE: {
    label: '已验证 · 单源',
    color: 'blue',
    hint: '触发词或双模型验证通过，仅单一来源'
  },
  CANDIDATE: {
    label: 'AI 候选',
    color: 'default',
    hint: '仅通过逐字校验，尚未经语义验证或人工审定'
  }
}

const trustLabel = (tier) => TRUST_META[tier]?.label || tier
const trustColor = (tier) => TRUST_META[tier]?.color || 'default'
const trustHint = (tier) => TRUST_META[tier]?.hint || ''
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

const loadEvidence = async () => {
  evidence.value = null
  evidenceError.value = null
  expandedMentions.clear()
  if (!props.visible || !props.item || !props.kbId) return
  const properties = props.item.data?.original?.properties || {}
  const id = props.type === 'edge' ? properties.triple_id : properties.entity_id
  if (!id) {
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
        ? await graphApi.getTripleEvidence(props.kbId, id)
        : await graphApi.getEntityEvidence(props.kbId, id)
    evidence.value = response?.data || null
  } catch (e) {
    evidenceError.value = e?.response?.data?.detail || e?.message || '原文加载失败'
  } finally {
    evidenceLoading.value = false
  }
}

watch(
  () => props.item,
  () => {
    expandedKeys.clear()
  }
)

watch([() => props.item, () => props.visible, () => props.kbId], loadEvidence, { immediate: true })

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
  const hiddenFields = ['source_id', 'target_id', '_id', 'truncate']
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
  width: 360px;
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

.evidence-section {
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

    .expand-link {
      cursor: pointer;
      color: var(--main-700);
      white-space: nowrap;

      &:hover {
        color: var(--main-500);
      }
    }
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
