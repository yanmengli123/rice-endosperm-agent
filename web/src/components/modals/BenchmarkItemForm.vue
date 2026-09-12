<template>
  <a-drawer
    :open="visible"
    :title="isEdit ? `编辑题目 ${form.external_id || ''}` : '新增题目'"
    width="560"
    :mask-closable="false"
    :destroy-on-close="true"
    @close="handleClose"
  >
    <a-form ref="formRef" :model="form" :rules="rules" layout="vertical" class="item-form">
      <a-form-item label="问题（query）" name="query" required>
        <a-textarea
          v-model:value="form.query"
          :rows="2"
          :maxlength="500"
          show-count
          placeholder="用户真实口吻的问题，一题一意图，不要包含答案线索"
        />
      </a-form-item>

      <a-form-item label="参考答案（gold_answer）" name="goldAnswer" required>
        <a-textarea
          v-model:value="form.goldAnswer"
          :rows="3"
          :maxlength="4000"
          :disabled="form.answerType === 'unanswerable'"
          :placeholder="
            form.answerType === 'unanswerable'
              ? '不可回答题固定为拒答语，不可修改'
              : '自包含、事实密集、可核验；数值带单位；不写「根据文档…」'
          "
        />
        <div v-if="form.answerType === 'unanswerable'" class="field-hint">
          已锁定：{{ UNANSWERABLE_ANSWER }}
        </div>
      </a-form-item>

      <div class="form-row">
        <a-form-item label="答案类型" name="answerType" class="half">
          <a-select
            v-model:value="form.answerType"
            placeholder="选择类型"
            allow-clear
            @change="onAnswerTypeChange"
          >
            <a-select-option
              v-for="option in ANSWER_TYPE_OPTIONS"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="难度" name="difficulty" class="half">
          <a-select v-model:value="form.difficulty" placeholder="选择难度" allow-clear>
            <a-select-option value="easy">easy（单段单跳）</a-select-option>
            <a-select-option value="medium">medium（跨段综合）</a-select-option>
            <a-select-option value="hard">hard（推理/计算/时效）</a-select-option>
          </a-select>
        </a-form-item>
      </div>

      <a-form-item label="标签（tags）" name="tags">
        <a-select
          v-model:value="form.tags"
          mode="tags"
          placeholder="业务域/文档类型，如 expense、policy（最多 10 个）"
          :max-tag-count="6"
        />
      </a-form-item>

      <a-form-item label="关键事实点（must_include）" name="mustInclude">
        <a-select
          v-model:value="form.mustInclude"
          mode="tags"
          placeholder="答案必须包含的原子事实，每项一个，回车确认"
          :max-tag-count="10"
        />
        <div class="field-hint">保存时校验每一项是否出现在参考答案中</div>
      </a-form-item>

      <a-form-item label="参考文档块（gold_chunk_ids）">
        <div class="chunk-block">
          <a-button size="small" @click="chunkPickerVisible = true">
            <template #icon><ListChecks :size="14" /></template>
            从知识库选块
          </a-button>
          <div class="chunk-tags">
            <a-tag
              v-for="chunkId in form.goldChunkIds"
              :key="chunkId"
              closable
              @close="removeChunk(chunkId)"
            >
              {{ chunkId }}
            </a-tag>
            <span v-if="!form.goldChunkIds.length" class="field-hint">
              不手填内部块 ID，通过选块器勾选；问答基准可不选
            </span>
          </div>
        </div>
      </a-form-item>

      <a-form-item label="证据出处（evidence）">
        <div v-for="(entry, index) in form.evidence" :key="index" class="evidence-row">
          <a-input v-model:value="entry.file" placeholder="文件名" style="width: 160px" />
          <a-input v-model:value="entry.quote" placeholder="原文短引（可选）" style="flex: 1" />
          <a-input-number
            v-model:value="entry.page"
            placeholder="页码"
            :min="1"
            style="width: 90px"
          />
          <a-button type="text" danger size="small" @click="form.evidence.splice(index, 1)">
            <template #icon><Trash2 :size="14" /></template>
          </a-button>
        </div>
        <a-button
          v-if="form.evidence.length < 5"
          size="small"
          type="dashed"
          block
          @click="form.evidence.push({ file: '', quote: '', page: null })"
        >
          + 添加证据出处
        </a-button>
      </a-form-item>

      <div class="form-row">
        <a-form-item label="业务编号（external_id）" class="half">
          <a-input v-model:value="form.externalId" placeholder="留空自动生成 item-0001" />
        </a-form-item>
        <a-form-item label="文档版本（source_version）" class="half">
          <a-input v-model:value="form.sourceVersion" placeholder="如 V3-2026-06" />
        </a-form-item>
      </div>

      <a-form-item label="备注（notes）">
        <a-textarea
          v-model:value="form.notes"
          :rows="2"
          :maxlength="1000"
          placeholder="给审核者看的说明（可选）"
        />
      </a-form-item>

      <a-form-item label="拒答历史" v-if="reviewHistory.length">
        <div class="review-history">
          <div v-for="(entry, index) in reviewHistory" :key="index" class="review-entry">
            <a-tag
              :color="
                entry.action === 'reject' ? 'red' : entry.action === 'approve' ? 'green' : 'default'
              "
            >
              {{ { approve: '通过', reject: '打回', reset: '重置' }[entry.action] || entry.action }}
            </a-tag>
            <span class="review-meta">{{ entry.by }} · {{ entry.at }}</span>
            <span v-if="entry.reason" class="review-reason">原因：{{ entry.reason }}</span>
          </div>
        </div>
      </a-form-item>
    </a-form>

    <template #footer>
      <div class="form-footer">
        <div class="probe-area">
          <a-button
            size="small"
            :loading="probing"
            :disabled="!form.query?.trim()"
            @click="runProbe"
          >
            <template #icon><SearchCheck :size="14" /></template>
            试答探测
          </a-button>
          <span v-if="probeSummary" class="probe-summary" :class="{ hit: probeHasHit }">{{
            probeSummary
          }}</span>
        </div>
        <div class="form-actions">
          <a-button @click="handleClose">取消</a-button>
          <a-button type="primary" :loading="saving" @click="handleSave">
            {{ isEdit ? '保存' : '添加' }}
          </a-button>
        </div>
      </div>
    </template>

    <a-modal
      v-model:open="probeVisible"
      title="试答探测（当前知识库检索配置）"
      width="640px"
      :footer="null"
    >
      <div class="probe-results">
        <div
          v-for="item in probeResults"
          :key="item.rank"
          class="probe-item"
          :class="{ hit: item.hit }"
        >
          <div class="probe-head">
            <span class="probe-rank">#{{ item.rank }}</span>
            <span class="probe-chunk">{{ item.chunk_id || '无 chunk_id' }}</span>
            <a-tag v-if="item.hit" color="success" class="probe-tag">命中参考块</a-tag>
            <span v-if="typeof item.score === 'number'" class="probe-score"
              >score={{ item.score.toFixed(4) }}</span
            >
          </div>
          <div class="probe-content">{{ item.content }}</div>
        </div>
        <div v-if="!probeResults.length" class="probe-empty">没有检索到任何内容</div>
      </div>
    </a-modal>

    <ChunkPickerModal
      v-model:visible="chunkPickerVisible"
      :kb-id="kbId"
      :initial-chunk-ids="form.goldChunkIds"
      @confirm="onChunksPicked"
    />
  </a-drawer>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { ListChecks, SearchCheck, Trash2 } from '@lucide/vue'
import { evaluationApi } from '@/apis/knowledge_api'
import ChunkPickerModal from './ChunkPickerModal.vue'

const UNANSWERABLE_ANSWER = '信息不足，无法回答'
const ANSWER_TYPE_OPTIONS = [
  { value: 'fact', label: 'fact（事实型）' },
  { value: 'numeric', label: 'numeric（数值型）' },
  { value: 'list', label: 'list（列表型）' },
  { value: 'boolean', label: 'boolean（是否型）' },
  { value: 'procedure', label: 'procedure（流程型）' },
  { value: 'citation', label: 'citation（页码引用）' },
  { value: 'unanswerable', label: 'unanswerable（不可回答）' }
]

const props = defineProps({
  visible: { type: Boolean, default: false },
  kbId: { type: String, required: true },
  item: { type: Object, default: null }
})

const emit = defineEmits(['update:visible', 'saved'])

const formRef = ref()
const saving = ref(false)
const isEdit = computed(() => !!props.item?.item_id)

const defaultForm = () => ({
  query: '',
  goldAnswer: '',
  goldChunkIds: [],
  externalId: '',
  answerType: undefined,
  difficulty: undefined,
  tags: [],
  mustInclude: [],
  evidence: [],
  sourceVersion: '',
  notes: ''
})

const form = reactive(defaultForm())
const reviewHistory = ref([])

const rules = {
  query: [{ required: true, whitespace: true, message: '问题不能为空', trigger: 'blur' }]
}

const chunkPickerVisible = ref(false)
const probing = ref(false)
const probeVisible = ref(false)
const probeResults = ref([])
const probeHasHit = ref(false)
const probeSummary = ref('')

const onAnswerTypeChange = (value) => {
  if (value === 'unanswerable') {
    form.goldAnswer = UNANSWERABLE_ANSWER
    form.goldChunkIds = []
    form.mustInclude = []
  } else if (form.goldAnswer === UNANSWERABLE_ANSWER) {
    form.goldAnswer = ''
  }
}

const removeChunk = (chunkId) => {
  const index = form.goldChunkIds.indexOf(chunkId)
  if (index >= 0) form.goldChunkIds.splice(index, 1)
}

const onChunksPicked = (chunkIds) => {
  form.goldChunkIds = chunkIds
}

const buildPayload = () => ({
  query: form.query,
  gold_answer: form.goldAnswer || null,
  gold_chunk_ids: form.goldChunkIds,
  external_id: form.externalId?.trim() || null,
  answer_type: form.answerType || null,
  tags: form.tags,
  difficulty: form.difficulty || null,
  must_include: form.mustInclude,
  evidence: form.evidence
    .filter((entry) => entry.file?.trim())
    .map((entry) => ({
      file: entry.file.trim(),
      quote: entry.quote?.trim() || null,
      page: entry.page || null
    })),
  source_version: form.sourceVersion?.trim() || null,
  notes: form.notes?.trim() || null
})

const extractFieldErrors = (error) => {
  const detail = error?.response?.data?.detail
  if (detail && typeof detail === 'object' && detail.fields) {
    return Object.values(detail.fields).join('；')
  }
  if (typeof detail === 'string') return detail
  return error?.message || '保存失败'
}

const handleSave = async () => {
  await formRef.value.validate()
  saving.value = true
  try {
    const payload = buildPayload()
    const response = isEdit.value
      ? await evaluationApi.updateDatasetItem(props.item.datasetId, props.item.item_id, payload)
      : await evaluationApi.addDatasetItem(props.item.datasetId, payload)
    if (response.message === 'success') {
      message.success(isEdit.value ? '题目已更新（内容变更将回到草稿待复审）' : '题目已添加')
      emit('saved', response.data)
      emit('update:visible', false)
    }
  } catch (error) {
    if (error?.errorFields) return // 表单校验错误
    message.error(extractFieldErrors(error))
  } finally {
    saving.value = false
  }
}

const runProbe = async () => {
  probing.value = true
  try {
    const response = await evaluationApi.probeQuestion(props.kbId, {
      query: form.query.trim(),
      gold_chunk_ids: form.goldChunkIds,
      top_k: 5
    })
    const data = response.data
    probeResults.value = data.results || []
    probeHasHit.value = data.gold_hit_count > 0
    probeSummary.value =
      data.gold_total > 0
        ? `命中 ${data.gold_hit_count}/${data.gold_total}（recall@5=${data.recall_at_k}）`
        : `${probeResults.value.length} 条结果（未配置参考块）`
    probeVisible.value = true
  } catch (error) {
    message.error(error?.response?.data?.detail || '试答探测失败')
  } finally {
    probing.value = false
  }
}

const handleClose = () => {
  emit('update:visible', false)
}

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    Object.assign(form, defaultForm())
    reviewHistory.value = []
    probeSummary.value = ''
    probeResults.value = []
    if (props.item) {
      const meta = props.item.item_metadata || {}
      form.query = props.item.query || ''
      form.goldAnswer = props.item.gold_answer || ''
      form.goldChunkIds = [...(props.item.gold_chunk_ids || [])]
      form.externalId = props.item.external_id || ''
      form.answerType = meta.answer_type
      form.difficulty = meta.difficulty
      form.tags = [...(meta.tags || [])]
      form.mustInclude = [...(meta.must_include || [])]
      form.evidence = (meta.evidence || []).map((entry) => ({ ...entry }))
      form.sourceVersion = meta.source_version || ''
      form.notes = meta.notes || ''
      reviewHistory.value = [...(meta.review_history || [])].reverse()
    }
  }
)
</script>

<style lang="less" scoped>
.item-form {
  .form-row {
    display: flex;
    gap: 12px;

    .half {
      flex: 1;
      min-width: 0;
    }
  }

  .field-hint {
    font-size: 12px;
    color: var(--gray-400);
    margin-top: 4px;
    line-height: 1.4;
  }
}

.chunk-block {
  display: flex;
  flex-direction: column;
  gap: 8px;

  .chunk-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
}

.evidence-row {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
  align-items: center;
}

.review-history {
  .review-entry {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--gray-600);
    padding: 2px 0;

    .review-meta {
      color: var(--gray-400);
    }

    .review-reason {
      color: var(--color-error-700, #cf1322);
    }
  }
}

.form-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;

  .probe-area {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;

    .probe-summary {
      font-size: 12px;
      color: var(--gray-500);

      &.hit {
        color: var(--color-success-700, #389e0d);
        font-weight: 500;
      }
    }
  }

  .form-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }
}

.probe-results {
  max-height: 420px;
  overflow-y: auto;

  .probe-item {
    border: 1px solid var(--gray-150);
    border-radius: 6px;
    padding: 8px;
    margin-bottom: 8px;

    &.hit {
      border-color: var(--color-success-200, #b7eb8f);
      background: rgba(82, 196, 26, 0.06);
    }

    .probe-head {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--gray-600);

      .probe-rank {
        font-weight: 600;
      }

      .probe-chunk {
        font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
        word-break: break-all;
      }

      .probe-score {
        color: var(--gray-400);
      }
    }

    .probe-content {
      margin-top: 4px;
      font-size: 12px;
      color: var(--gray-700);
      line-height: 1.5;
      max-height: 80px;
      overflow: hidden;
      word-break: break-all;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
    }
  }

  .probe-empty {
    text-align: center;
    color: var(--gray-400);
    padding: 24px 0;
  }
}
</style>
