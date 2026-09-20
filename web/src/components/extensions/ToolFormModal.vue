<template>
  <a-modal
    v-model:open="visible"
    :title="editMode ? '编辑自定义工具' : '新建自定义工具'"
    @ok="handleFormSubmit"
    :confirmLoading="formLoading"
    @cancel="visible = false"
    :maskClosable="false"
    width="680px"
    class="tool-form-modal"
  >
    <a-form layout="vertical" class="extension-form">
      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="工具标识" required class="form-item">
            <a-input
              v-model:value="form.slug"
              placeholder="小写字母开头，如 fetch_gene_info"
              :disabled="editMode"
            />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="工具名称" required class="form-item">
            <a-input v-model:value="form.name" placeholder="请输入展示名称" />
          </a-form-item>
        </a-col>
      </a-row>
      <a-form-item label="描述（给模型看）" required class="form-item">
        <a-textarea
          v-model:value="form.description"
          placeholder="说明何时应调用该工具、输入参数的含义与返回内容；这是模型决定是否调用的唯一依据"
          :rows="2"
        />
      </a-form-item>
      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="请求方式" required class="form-item">
            <a-select v-model:value="form.method">
              <a-select-option v-for="m in methods" :key="m" :value="m">{{ m }}</a-select-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="超时（秒）" class="form-item">
            <a-input-number v-model:value="form.timeout" :min="1" :max="60" style="width: 100%" />
          </a-form-item>
        </a-col>
      </a-row>
      <a-form-item label="Base URL" required class="form-item">
        <a-input v-model:value="form.base_url" placeholder="https://api.example.com" />
        <small class="field-hint">必须通过 SSRF 校验：HTTPS、公网地址、拒绝内网/元数据端点。</small>
      </a-form-item>
      <a-form-item label="请求路径" required class="form-item">
        <a-input v-model:value="form.path" :placeholder="PATH_PLACEHOLDER" />
        <small class="field-hint">{{ PATH_HINT }}</small>
      </a-form-item>

      <a-form-item label="参数契约（模型输入）" class="form-item">
        <div class="param-rows">
          <div v-for="(row, index) in paramRows" :key="index" class="param-row">
            <a-input v-model:value="row.name" placeholder="参数名" class="param-name" />
            <a-select v-model:value="row.type" class="param-type">
              <a-select-option v-for="t in argTypes" :key="t" :value="t">{{ t }}</a-select-option>
            </a-select>
            <a-select v-if="row.type === 'array'" v-model:value="row.itemsType" class="param-items">
              <a-select-option v-for="t in itemTypes" :key="t" :value="t"
                >items: {{ t }}</a-select-option
              >
            </a-select>
            <a-checkbox v-model:checked="row.required">必填</a-checkbox>
            <a-button type="text" danger size="small" @click="paramRows.splice(index, 1)">
              <X :size="14" />
            </a-button>
          </div>
          <div v-for="(row, index) in paramRows" :key="'desc-' + index" class="param-desc-row">
            <a-input
              v-model:value="row.description"
              :placeholder="`参数 ${row.name || '?'} 的含义说明`"
            />
          </div>
        </div>
        <a-button type="dashed" block size="small" @click="addParamRow">
          <Plus :size="14" /> 添加参数
        </a-button>
        <small class="field-hint">
          {{ PARAM_HINT }}
        </small>
      </a-form-item>

      <a-form-item label="查询参数" class="form-item">
        <div class="kv-rows">
          <div v-for="(row, index) in queryRows" :key="index" class="kv-row">
            <a-input v-model:value="row.key" placeholder="参数名，如 q" class="kv-key" />
            <a-input
              v-model:value="row.value"
              :placeholder="QUERY_VALUE_PLACEHOLDER"
              class="kv-value"
            />
            <a-button type="text" danger size="small" @click="queryRows.splice(index, 1)">
              <X :size="14" />
            </a-button>
          </div>
          <div v-if="!queryRows.length" class="text-muted kv-empty">无查询参数</div>
        </div>
        <a-button type="dashed" block size="small" @click="queryRows.push({ key: '', value: '' })">
          <Plus :size="14" /> 添加查询参数
        </a-button>
      </a-form-item>

      <a-form-item v-if="hasBody" label="请求体字段（JSON）" class="form-item">
        <a-select
          v-model:value="form.body_params"
          mode="multiple"
          placeholder="从已定义参数中选择要放入 JSON body 的字段"
          :options="paramOptions"
          style="width: 100%"
        />
      </a-form-item>

      <a-form-item label="静态请求头" class="form-item">
        <div class="kv-rows">
          <div v-for="(row, index) in headerRows" :key="index" class="kv-row">
            <a-input v-model:value="row.key" placeholder="Header 名，如 X-Client" class="kv-key" />
            <a-input
              v-model:value="row.value"
              placeholder="值；支持 ${ENV_VAR} 引用"
              class="kv-value"
            />
            <a-button type="text" danger size="small" @click="headerRows.splice(index, 1)">
              <X :size="14" />
            </a-button>
          </div>
          <div v-if="!headerRows.length" class="text-muted kv-empty">无静态请求头</div>
        </div>
        <a-button type="dashed" block size="small" @click="headerRows.push({ key: '', value: '' })">
          <Plus :size="14" /> 添加请求头
        </a-button>
        <small class="field-hint"
          >含 authorization/token/secret 等关键字的明文值会被拒绝，请使用凭据。</small
        >
      </a-form-item>

      <a-form-item label="认证凭据" class="form-item">
        <a-select
          v-model:value="form.credential_id"
          allow-clear
          placeholder="无认证，或选择已加密凭据"
          :options="credentialOptions"
        />
        <small class="field-hint">密钥只保存在服务端密文仓库，工具配置仅保存 credential_id。</small>
      </a-form-item>

      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="数据级别" class="form-item">
            <a-select v-model:value="form.data_access_level">
              <a-select-option value="PUBLIC">PUBLIC</a-select-option>
              <a-select-option value="INTERNAL">INTERNAL</a-select-option>
              <a-select-option value="CONTROLLED">CONTROLLED</a-select-option>
              <a-select-option value="HUMAN_SENSITIVE">HUMAN_SENSITIVE</a-select-option>
            </a-select>
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="依赖语义" class="form-item">
            <a-select v-model:value="form.dependency_mode">
              <a-select-option value="OPTIONAL">OPTIONAL</a-select-option>
              <a-select-option value="REQUIRED">REQUIRED</a-select-option>
              <a-select-option value="AUTHORITATIVE">AUTHORITATIVE</a-select-option>
            </a-select>
          </a-form-item>
        </a-col>
      </a-row>
      <a-row :gutter="16">
        <a-col :span="12">
          <a-form-item label="标签" class="form-item">
            <a-select
              v-model:value="form.tags"
              mode="tags"
              placeholder="输入标签后回车添加"
              style="width: 100%"
            />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="图标" class="form-item">
            <a-input v-model:value="form.icon" placeholder="输入 emoji，如 🔌" :maxlength="2" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-collapse :bordered="false" class="schema-preview">
        <a-collapse-panel key="schema" header="args_schema 预览（提交给后端的参数契约）">
          <pre class="schema-json">{{ argsSchemaPreview }}</pre>
        </a-collapse-panel>
      </a-collapse>
    </a-form>
  </a-modal>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { Plus, X } from '@lucide/vue'
import { toolApi } from '@/apis/tool_api'
import { mcpApi } from '@/apis/mcp_api'

const props = defineProps({
  open: { type: Boolean, default: false },
  editMode: { type: Boolean, default: false },
  editData: { type: Object, default: null }
})

const emit = defineEmits(['update:open', 'submitted'])

const visible = computed({
  get: () => props.open,
  set: (val) => emit('update:open', val)
})

const methods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const argTypes = ['string', 'integer', 'number', 'boolean', 'array']
const itemTypes = ['string', 'integer', 'number', 'boolean']

// 占位符语法含双花括号，必须放在脚本常量里——模板中的字面量 {{ }} 会被 Vue 解析为插值
const PATH_PLACEHOLDER = '/v1/genes/{{gene_id}}'
const PATH_HINT = '以 / 开头；用 {{参数名}} 引用下方定义的参数，路径参数会自动 URL 编码。'
const PARAM_HINT = '每个参数必须被路径 {{参数名}}、查询参数值或请求体字段引用，否则无法保存。'
const QUERY_VALUE_PLACEHOLDER = '值或 {{参数名}} 模板'

const formLoading = ref(false)
const credentials = ref([])
const credentialOptions = computed(() =>
  credentials.value
    .filter((item) => item.status === 'active')
    .map((item) => ({
      label: `${item.name}（${item.auth_type} · ${item.masked_hint || '已加密'}）`,
      value: item.credential_id
    }))
)

const form = reactive({
  slug: '',
  name: '',
  description: '',
  method: 'GET',
  timeout: 15,
  base_url: '',
  path: '/',
  body_params: [],
  credential_id: null,
  data_access_level: 'PUBLIC',
  dependency_mode: 'OPTIONAL',
  tags: [],
  icon: ''
})

const paramRows = ref([])
const queryRows = ref([])
const headerRows = ref([])

const hasBody = computed(() => ['POST', 'PUT', 'PATCH'].includes(form.method))
const paramOptions = computed(() =>
  paramRows.value.filter((row) => row.name).map((row) => ({ label: row.name, value: row.name }))
)

const addParamRow = () => {
  paramRows.value.push({
    name: '',
    type: 'string',
    required: false,
    description: '',
    itemsType: 'string'
  })
}

const buildArgsSchema = () => {
  const properties = {}
  const required = []
  for (const row of paramRows.value) {
    if (!row.name) continue
    const prop = { type: row.type, description: row.description || `参数 ${row.name}` }
    if (row.type === 'array') prop.items = { type: row.itemsType || 'string' }
    properties[row.name] = prop
    if (row.required) required.push(row.name)
  }
  const schema = { type: 'object', properties }
  if (required.length) schema.required = required
  return schema
}

const argsSchemaPreview = computed(() => JSON.stringify(buildArgsSchema(), null, 2))

const buildKvObject = (rows) => {
  const result = {}
  for (const row of rows) {
    if (row.key && row.key.trim()) result[row.key.trim()] = row.value
  }
  return result
}

const loadCredentials = async () => {
  try {
    const result = await mcpApi.getMcpCredentials()
    credentials.value = result.success ? result.data || [] : []
  } catch {
    credentials.value = []
  }
}

const resetForm = () => {
  Object.assign(form, {
    slug: '',
    name: '',
    description: '',
    method: 'GET',
    timeout: 15,
    base_url: '',
    path: '/',
    body_params: [],
    credential_id: null,
    data_access_level: 'PUBLIC',
    dependency_mode: 'OPTIONAL',
    tags: [],
    icon: ''
  })
  paramRows.value = []
  queryRows.value = []
  headerRows.value = []
}

const populateFromEdit = (detail) => {
  const spec = detail.spec || {}
  Object.assign(form, {
    slug: detail.slug || '',
    name: detail.name || '',
    description: detail.description || '',
    method: spec.method || 'GET',
    timeout: spec.timeout_s || 15,
    base_url: spec.base_url || '',
    path: spec.path || '/',
    body_params: spec.body_params || [],
    credential_id: detail.credential_id || null,
    data_access_level: detail.data_access_level || 'PUBLIC',
    dependency_mode: detail.dependency_mode || 'OPTIONAL',
    tags: detail.tags || [],
    icon: detail.icon || ''
  })
  const schema = detail.args_schema || {}
  paramRows.value = Object.entries(schema.properties || {}).map(([name, prop]) => ({
    name,
    type: prop.type || 'string',
    required: (schema.required || []).includes(name),
    description: prop.description || '',
    itemsType: prop.items?.type || 'string'
  }))
  queryRows.value = Object.entries(spec.query || {}).map(([key, value]) => ({ key, value }))
  headerRows.value = Object.entries(spec.headers || {}).map(([key, value]) => ({ key, value }))
}

watch(
  () => props.open,
  async (val) => {
    if (!val) return
    loadCredentials()
    if (props.editMode && props.editData?.slug) {
      // 目录条目不含 spec/args_schema，编辑时拉取详情
      try {
        const result = await toolApi.getCustomTool(props.editData.slug)
        if (result.success) {
          populateFromEdit(result.data)
          return
        }
        message.error(result.message || '加载工具详情失败')
      } catch (err) {
        message.error(err.message || '加载工具详情失败')
      }
      populateFromEdit(props.editData)
    } else {
      resetForm()
      addParamRow()
    }
  },
  { immediate: true }
)

const validateForm = (data) => {
  if (!data.slug?.trim()) {
    message.error('工具标识不能为空')
    return false
  }
  if (!/^[a-z][a-z0-9_]{1,63}$/.test(data.slug.trim())) {
    message.error('工具标识必须以小写字母开头，仅含小写字母/数字/下划线')
    return false
  }
  if (!data.name?.trim()) {
    message.error('工具名称不能为空')
    return false
  }
  if (!data.description?.trim()) {
    message.error('描述不能为空（模型依据它决定是否调用）')
    return false
  }
  if (!data.spec.base_url?.trim()) {
    message.error('Base URL 不能为空')
    return false
  }
  if (!data.spec.path?.startsWith('/')) {
    message.error('请求路径必须以 / 开头')
    return false
  }
  return true
}

const handleFormSubmit = async () => {
  const data = {
    slug: form.slug.trim(),
    name: form.name.trim(),
    description: form.description.trim(),
    tool_type: 'http',
    spec: {
      base_url: form.base_url.trim(),
      path: form.path.trim(),
      method: form.method,
      headers: buildKvObject(headerRows.value),
      query: buildKvObject(queryRows.value),
      body_params: form.body_params || [],
      timeout_s: form.timeout || 15
    },
    args_schema: buildArgsSchema(),
    credential_id: form.credential_id || null,
    data_access_level: form.data_access_level,
    dependency_mode: form.dependency_mode,
    tags: form.tags?.length ? form.tags : null,
    icon: form.icon || null
  }
  if (!validateForm(data)) return

  try {
    formLoading.value = true
    if (props.editMode) {
      const { slug, ...updateData } = data
      const result = await toolApi.updateTool(props.editData?.slug || slug, updateData)
      if (!result.success) {
        message.error(result.message || '更新失败')
        return
      }
      message.success('自定义工具更新成功（连接配置已变更时会回到草稿，需重新测连）')
    } else {
      const result = await toolApi.createTool(data)
      if (!result.success) {
        message.error(result.message || '创建失败')
        return
      }
      message.success('自定义工具已创建（草稿），测连通过后即可启用')
    }
    visible.value = false
    emit('submitted')
  } catch (err) {
    message.error(err.message || '操作失败')
  } finally {
    formLoading.value = false
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

.param-rows,
.kv-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  margin-bottom: 8px;
}

.param-row {
  display: flex;
  align-items: center;
  gap: 6px;

  .param-name {
    flex: 1.2;
  }

  .param-type {
    width: 110px;
    flex-shrink: 0;
  }

  .param-items {
    width: 130px;
    flex-shrink: 0;
  }
}

.param-desc-row {
  margin-bottom: 6px;
}

.kv-row {
  display: flex;
  align-items: center;
  gap: 6px;

  .kv-key {
    flex: 1;
  }

  .kv-value {
    flex: 2;
  }
}

.kv-empty {
  font-size: 12px;
}

.schema-preview {
  margin-top: 4px;
  background: transparent;

  .schema-json {
    margin: 0;
    padding: 8px;
    border-radius: 6px;
    background: var(--gray-50);
    color: var(--gray-700);
    font-size: 12px;
    font-family: var(--mono-font, monospace);
    max-height: 220px;
    overflow: auto;
    white-space: pre-wrap;
  }
}
</style>
