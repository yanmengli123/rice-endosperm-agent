<template>
  <a-modal
    v-model:open="visible"
    title="导入 OpenAPI 工具"
    @ok="handleImport"
    :confirmLoading="importing"
    @cancel="visible = false"
    :maskClosable="false"
    width="640px"
    class="tool-import-modal"
  >
    <a-alert
      type="info"
      show-icon
      class="import-alert"
      message="导入生成的是草稿工具"
      description="每个操作生成一个 DRAFT 自定义工具（不自动启用）。请导入后逐个测连、补充凭据后再启用。"
    />
    <a-form layout="vertical" class="extension-form">
      <a-form-item label="目标 Base URL" required class="form-item">
        <a-input
          v-model:value="baseUrl"
          placeholder="https://api.example.com（建议显式指定，覆盖文档 servers）"
        />
        <small class="field-hint">同样受 SSRF 校验约束：HTTPS、公网地址。</small>
      </a-form-item>
      <a-form-item label="标识前缀" class="form-item">
        <a-input v-model:value="namePrefix" placeholder="生成 slug 的前缀，默认 api" />
      </a-form-item>
      <a-form-item label="OpenAPI 文档（JSON 或 YAML）" required class="form-item">
        <a-textarea
          v-model:value="documentText"
          placeholder='粘贴 openapi: 3.x 文档内容，或 {"openapi": "3.0.0", "paths": {...}}'
          :rows="10"
          class="doc-textarea"
        />
      </a-form-item>
    </a-form>
    <div v-if="result" class="import-result">
      <div class="result-line">
        成功生成 <b>{{ result.created.length }}</b> 个草稿工具
        <span v-if="result.created.length">：{{ result.created.join('、') }}</span>
      </div>
      <div v-if="result.skipped?.length" class="result-line skipped">
        跳过 {{ result.skipped.length }} 个操作：
        <div v-for="item in result.skipped" :key="item.operation" class="skip-item">
          {{ item.operation }} — {{ item.reason }}
        </div>
      </div>
    </div>
  </a-modal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import yaml from 'js-yaml'
import { toolApi } from '@/apis/tool_api'

const props = defineProps({
  open: { type: Boolean, default: false }
})

const emit = defineEmits(['update:open', 'submitted'])

const visible = computed({
  get: () => props.open,
  set: (val) => emit('update:open', val)
})

const importing = ref(false)
const baseUrl = ref('')
const namePrefix = ref('')
const documentText = ref('')
const result = ref(null)

watch(
  () => props.open,
  (val) => {
    if (val) result.value = null
  }
)

const parseDocument = () => {
  const text = documentText.value.trim()
  if (!text) {
    message.error('请粘贴 OpenAPI 文档')
    return null
  }
  try {
    return JSON.parse(text)
  } catch {
    /* 尝试 YAML */
  }
  try {
    return yaml.load(text)
  } catch (err) {
    message.error(`文档既不是合法 JSON 也不是合法 YAML：${err.message}`)
    return null
  }
}

const handleImport = async () => {
  if (!baseUrl.value.trim()) {
    message.error('目标 Base URL 必填（显式指定比信任文档 servers 更安全）')
    return
  }
  const document = parseDocument()
  if (!document) return
  try {
    importing.value = true
    const response = await toolApi.importOpenApiTools({
      document,
      base_url: baseUrl.value.trim(),
      name_prefix: namePrefix.value.trim() || null
    })
    if (!response.success) {
      message.error(response.message || '导入失败')
      return
    }
    result.value = response.data
    if (response.data?.created?.length) {
      message.success(
        `已生成 ${response.data.created.length} 个草稿工具；需要鉴权的请先配置凭据，再逐个测连（仅 2xx 通过）后启用`
      )
      emit('submitted')
    } else {
      message.warning('没有生成任何工具，请查看跳过原因')
    }
  } catch (err) {
    message.error(err.message || '导入失败')
  } finally {
    importing.value = false
  }
}
</script>

<style lang="less" scoped>
@import '@/assets/css/extensions.less';

.field-hint {
  display: block;
  margin-top: 6px;
  color: var(--gray-500);
  line-height: 1.5;
}

.import-alert {
  margin-bottom: 16px;
}

.doc-textarea {
  font-family: var(--mono-font, monospace);
  font-size: 12px;
}

.import-result {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--gray-50);
  font-size: 13px;
  color: var(--gray-700);

  .result-line {
    line-height: 1.6;
  }

  .skipped {
    margin-top: 4px;
    color: var(--gray-600);
  }

  .skip-item {
    padding-left: 12px;
    color: var(--gray-500);
  }
}
</style>
