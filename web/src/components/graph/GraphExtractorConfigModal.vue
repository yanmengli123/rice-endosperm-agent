<template>
  <a-modal
    :open="open"
    :title="isEditing ? '修改图谱抽取配置' : '配置图谱抽取器'"
    width="640px"
    :confirm-loading="saving"
    @ok="save"
    @cancel="close"
  >
    <a-form layout="vertical">
      <a-alert
        v-if="isEditing"
        class="config-warning"
        type="warning"
        show-icon
        message="修改配置仅影响后续构建；已构建的图谱不会自动重算，如需一致请重置后重新抽取。抽取器类型创建后不可修改。"
      />
      <a-form-item label="抽取器类型">
        <div class="extractor-type-cards" role="radiogroup" aria-label="抽取器类型">
          <div
            v-for="option in extractorTypeOptions"
            :key="option.value"
            class="extractor-type-card"
            :class="{
              active: form.extractor_type === option.value,
              disabled: isEditing || option.disabled
            }"
            role="radio"
            :aria-checked="form.extractor_type === option.value"
            :aria-disabled="isEditing || option.disabled"
            :tabindex="isEditing || option.disabled ? -1 : 0"
            @click="selectExtractorType(option)"
            @keydown.enter.prevent="selectExtractorType(option)"
            @keydown.space.prevent="selectExtractorType(option)"
          >
            <div class="card-header">
              <component :is="option.icon" class="type-icon" />
              <span class="type-title">{{ option.label }}</span>
            </div>
            <div class="card-description">{{ option.description }}</div>
            <div v-if="option.helper" class="card-helper" :class="{ warning: option.disabled }">
              {{ option.helper }}
            </div>
          </div>
        </div>
      </a-form-item>
      <a-form-item label="模型">
        <ModelSelectorComponent
          :model_spec="form.model_spec"
          placeholder="选择抽取模型"
          @select-model="(spec) => (form.model_spec = spec)"
        />
      </a-form-item>
      <a-form-item v-if="isScientificExtractor" label="抽取约束">
        <a-alert
          type="info"
          show-icon
          message="闭集词表科研抽取：实体类型与关系谓词固定为托管图谱白名单（16 类实体 / 21 种关系），按句窗抽取并经逐字校验门；不接受自定义 Schema。构建结果中的 extraction_stats 会给出候选数、拒绝分布与幻觉率。"
        />
      </a-form-item>
      <a-form-item v-else label="Schema">
        <a-textarea
          v-model:value="form.schema"
          :rows="6"
          placeholder="描述实体类型、关系类型和属性约束。后端会把 Schema 拼接到固定抽取 Prompt 中。"
        />
      </a-form-item>
      <div class="form-grid two-columns">
        <a-form-item label="并发队列数">
          <a-input-number
            v-model:value="form.concurrency_count"
            :min="1"
            :max="1000"
            :step="1"
            style="width: 100%"
          />
        </a-form-item>
        <a-form-item label="模型参数 JSON">
          <a-input v-model:value="form.model_params_text" placeholder='例如 {"temperature":0.1}' />
        </a-form-item>
      </div>
    </a-form>
  </a-modal>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { BrainCircuit, ScanText } from '@lucide/vue'
import { useConfigStore } from '@/stores/config'
import ModelSelectorComponent from '@/components/ModelSelectorComponent.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  kbId: { type: String, required: true },
  // graphBuildApi.getStatus 的返回（含 config / locked）
  status: { type: Object, default: null }
})
const emit = defineEmits(['update:open', 'saved'])

const configStore = useConfigStore()
const saving = ref(false)

const extractorTypeOptions = [
  {
    value: 'llm',
    label: 'LLM',
    description: '使用大模型按 Schema 抽取实体和关系',
    helper: '通用开放 Schema，适合非科研语料',
    icon: BrainCircuit,
    disabled: false
  },
  {
    value: 'llm_scientific',
    label: '科研闭集',
    description: '闭集词表 + 句窗抽取 + 逐字校验门，产出可度量的科研三元组',
    helper: '推荐用于人工整理的文献结果段 Markdown',
    icon: ScanText,
    disabled: false
  }
]

const form = reactive({
  extractor_type: 'llm',
  model_spec: '',
  schema: '',
  concurrency_count: 50,
  model_params_text: ''
})
const isEditing = computed(() => Boolean(props.status?.locked))
const isScientificExtractor = computed(() => form.extractor_type === 'llm_scientific')

const fillForm = () => {
  const config = props.status?.config
  const options = config?.extractor_options || {}
  form.extractor_type = config?.extractor_type || 'llm'
  form.model_spec = options.model_spec || configStore.config?.default_model || ''
  form.schema = options.schema || ''
  form.concurrency_count = Number(options.concurrency_count || 50)
  form.model_params_text = options.model_params ? JSON.stringify(options.model_params) : ''
}

watch(
  () => props.open,
  (open) => {
    if (open) fillForm()
  }
)

const selectExtractorType = (option) => {
  if (isEditing.value || option.disabled) return
  form.extractor_type = option.value
}

const parseModelParams = () => {
  const text = form.model_params_text.trim()
  if (!text) return {}
  let params
  try {
    params = JSON.parse(text)
  } catch {
    throw new Error('模型参数必须是合法 JSON 对象')
  }
  if (!params || Array.isArray(params) || typeof params !== 'object')
    throw new Error('模型参数必须是 JSON 对象')
  return params
}

const buildExtractorOptions = () => {
  const options = {
    model_spec: form.model_spec,
    concurrency_count: form.concurrency_count || 50,
    model_params: parseModelParams()
  }
  if (!isScientificExtractor.value) options.schema = form.schema.trim()
  return options
}

const close = () => emit('update:open', false)

const save = async () => {
  if (saving.value) return
  saving.value = true
  try {
    const { graphBuildApi } = await import('@/apis/knowledge_api')
    await graphBuildApi.configure(props.kbId, {
      extractor_type: form.extractor_type,
      extractor_options: buildExtractorOptions()
    })
    message.success(isEditing.value ? '图谱抽取配置已更新' : '图谱抽取配置已保存')
    emit('update:open', false)
    emit('saved')
  } catch (e) {
    console.error('Failed to configure graph build:', e)
    message.error(e?.response?.data?.detail || e?.message || '配置图谱抽取失败')
  } finally {
    saving.value = false
  }
}
</script>
<style scoped lang="less">
.config-warning {
  margin-bottom: 16px;
}
.extractor-type-cards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  .extractor-type-card {
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    padding: 14px;
    cursor: pointer;
    transition: all 0.2s ease;
    background: var(--gray-0);
    &:hover {
      border-color: var(--main-color);
    }
    &.active {
      border-color: var(--main-color);
      background: var(--main-10);
      box-shadow: 0 0 0 1px var(--main-20);
      .type-icon {
        color: var(--main-color);
      }
    }
    &.disabled {
      cursor: not-allowed;
      opacity: 0.72;
      background: var(--gray-50);
      &:hover {
        border-color: var(--gray-150);
      }
    }
    .card-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    .type-icon {
      width: 20px;
      height: 20px;
      color: var(--main-color);
      flex-shrink: 0;
    }
    .type-title {
      font-size: 15px;
      font-weight: 600;
      color: var(--gray-800);
    }
    .card-description {
      font-size: 13px;
      color: var(--gray-600);
      line-height: 1.5;
    }
    .card-helper {
      margin-top: 8px;
      font-size: 12px;
      color: var(--gray-500);
      &.warning {
        color: var(--color-warning-500);
      }
    }
  }
}
.form-grid.two-columns {
  display: grid;
  grid-template-columns: 180px 1fr;
  gap: 12px;
  @media (max-width: 640px) {
    grid-template-columns: 1fr;
  }
}
</style>
