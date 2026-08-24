<template>
  <div class="database-container layout-container">
    <PageHeader
      v-if="!props.embedded"
      title="知识库"
      :active-key="knowledgeActiveView"
      :tabs="knowledgeViewItems"
      :loading="dbState.listLoading"
      :show-border="true"
      aria-label="知识库视图切换"
    />

    <PageShoulder v-model:search="searchQuery" search-placeholder="搜索知识库...">
      <template #filters>
        <a-select
          v-model:value="typeFilter"
          style="width: 120px"
          placeholder="全部类型"
          allow-clear
        >
          <a-select-option :value="null">全部类型</a-select-option>
          <a-select-option v-for="t in kbTypes" :key="t" :value="t">
            {{ getKbTypeLabel(t) }}
          </a-select-option>
        </a-select>
        <a-select v-model:value="scopeFilter" style="width: 140px" placeholder="默认问答范围">
          <a-select-option value="all">全部范围状态</a-select-option>
          <a-select-option value="included">已纳入问答</a-select-option>
          <a-select-option value="excluded">未纳入问答</a-select-option>
        </a-select>
      </template>
      <template #actions>
        <a-button
          type="primary"
          class="lucide-icon-btn"
          :disabled="!kbTypes.length"
          @click="state.openNewDatabaseModel = true"
        >
          <Plus :size="16" /> 新建知识库
        </a-button>
      </template>
    </PageShoulder>

    <a-modal
      :open="state.openNewDatabaseModel"
      title="新建知识库"
      :confirm-loading="dbState.creating"
      @ok="handleCreateDatabase"
      @cancel="cancelCreateDatabase"
      class="new-database-modal"
      width="800px"
      destroyOnClose
    >
      <div class="new-database-form">
        <!-- 按文件格式快速创建（可选模板） -->
        <div class="form-section">
          <h3 class="section-title">
            按文件格式快速创建<span class="template-optional-mark">（选填，自动配置分块与解析）</span>
          </h3>
          <div class="format-template-cards">
            <div
              v-for="template in formatTemplates"
              :key="template.key"
              class="format-template-card"
              :class="{ active: state.formatTemplate === template.key }"
              @click="applyFormatTemplate(template.key)"
            >
              <div class="card-header">
                <span class="type-title">{{ template.label }}</span>
              </div>
              <div class="card-description">{{ template.description }}</div>
              <div
                v-if="state.formatTemplate === template.key && template.key === 'csv_dataset'"
                class="template-sub-option"
                @click.stop
              >
                <a-radio-group
                  :value="state.formatCsvMode"
                  size="small"
                  @change="handleFormatCsvModeChange"
                >
                  <a-radio-button value="record">记录型（一行一块）</a-radio-button>
                  <a-radio-button value="qa">问答型（Q/A 两列）</a-radio-button>
                </a-radio-group>
              </div>
            </div>
          </div>
        </div>

        <!-- 知识库类型选择 -->
        <div class="form-section">
          <h3 class="section-title">知识库类型<span class="required-mark">*</span></h3>
          <div class="kb-type-cards">
            <div
              v-for="(typeInfo, typeKey) in orderedKbTypes"
              :key="typeKey"
              class="kb-type-card"
              :class="{ active: newDatabase.kb_type === typeKey }"
              :data-type="typeKey"
              @click="handleKbTypeChange(typeKey)"
            >
              <div class="card-header">
                <component :is="getKbTypeIcon(typeKey)" class="type-icon" />
                <span class="type-title">{{ getKbTypeLabel(typeKey) }}</span>
              </div>
              <div class="card-description">{{ getKbTypeDescription(typeInfo) }}</div>
            </div>
          </div>
        </div>

        <div class="form-section">
          <h3 class="section-title">知识库名称<span class="required-mark">*</span></h3>
          <a-input v-model:value="newDatabase.name" placeholder="新建知识库名称" />
        </div>

        <div v-if="selectedKbTypeInfo?.requires_embedding_model" class="form-grid two-columns">
          <div class="form-section compact-section">
            <h3 class="section-title">嵌入模型</h3>
            <EmbeddingModelSelector
              v-model:value="newDatabase.embedding_model_spec"
              class="full-width"
              placeholder="请选择嵌入模型"
            />
          </div>

          <div class="form-section compact-section">
            <div class="chunk-preset-title-row">
              <h3 class="section-title">分块策略</h3>
              <a-tooltip :title="selectedPresetDescription">
                <QuestionCircleOutlined class="chunk-preset-help-icon" />
              </a-tooltip>
            </div>
            <a-select
              v-model:value="newDatabase.chunk_preset_id"
              :options="chunkPresetOptions"
              :loading="chunkPresetLoading"
              class="full-width"
            />
          </div>
        </div>

        <div v-if="createParamOptions.length" class="form-grid three-columns">
          <div
            v-for="field in createParamOptions"
            :key="field.key"
            class="form-section compact-section"
          >
            <h3 class="section-title">
              {{ field.label || field.key
              }}<span v-if="field.required" class="required-mark">*</span>
            </h3>
            <a-input-password
              v-if="field.type === 'password'"
              v-model:value="newDatabase.additional_params[field.key]"
              :placeholder="field.placeholder"
            />
            <a-input-number
              v-else-if="field.type === 'number'"
              v-model:value="newDatabase.additional_params[field.key]"
              :min="field.min"
              :max="field.max"
              :step="field.step"
              class="full-width"
            />
            <a-switch
              v-else-if="field.type === 'boolean'"
              v-model:checked="newDatabase.additional_params[field.key]"
            />
            <a-select
              v-else-if="field.type === 'select'"
              v-model:value="newDatabase.additional_params[field.key]"
              :options="field.options || []"
              class="full-width"
            />
            <a-input
              v-else
              v-model:value="newDatabase.additional_params[field.key]"
              :placeholder="field.placeholder"
            />
            <p v-if="field.description" class="field-hint">{{ field.description }}</p>
          </div>
        </div>

        <div class="form-section">
          <h3 class="section-title">知识库描述</h3>
          <p class="field-hint description-hint">
            在智能体流程中，这里的描述会作为工具的描述。智能体会根据知识库的标题和描述来选择合适的工具。所以这里描述的越详细，智能体越容易选择到合适的工具。
          </p>
          <AiTextarea
            v-model="newDatabase.description"
            :name="newDatabase.name"
            placeholder="新建知识库描述"
            :auto-size="{ minRows: 3, maxRows: 10 }"
          />
        </div>

        <!-- 共享配置 -->
        <div class="form-section compact-section">
          <h3 class="section-title">共享设置</h3>
          <ShareConfigForm
            ref="shareConfigFormRef"
            v-model="shareConfig"
            :auto-select-user-dept="true"
          />
        </div>
      </div>
      <template #footer>
        <a-button key="back" @click="cancelCreateDatabase">取消</a-button>
        <a-button
          key="submit"
          type="primary"
          :loading="dbState.creating"
          :disabled="!selectedKbTypeInfo"
          @click="handleCreateDatabase"
          >创建</a-button
        >
      </template>
    </a-modal>

    <a-modal
      :open="scopeModal.open"
      title="默认问答范围"
      width="680px"
      :confirm-loading="scopeModal.saving"
      destroyOnClose
      @ok="saveScopeMember"
      @cancel="closeScopeModal"
    >
      <div v-if="scopeModal.database" class="scope-config">
        <div class="scope-summary">
          <div>
            <div class="scope-kb-name">{{ scopeModal.database.name }}</div>
            <div class="scope-kb-id">{{ scopeModal.database.kb_id }}</div>
          </div>
          <a-tag :color="healthTag(scopeForm.health_status).color">
            {{ healthTag(scopeForm.health_status).label }}
          </a-tag>
        </div>

        <a-alert
          type="info"
          show-icon
          message="纳入问答只改变检索策略，不会重新索引、删除文件或修改图谱。最终范围仍会与用户权限取交集。"
        />

        <div class="scope-section scope-enabled-row">
          <div>
            <div class="scope-section-title">纳入默认问答范围</div>
            <div class="scope-section-hint">启用后，继承默认范围的智能体可检索此知识库。</div>
          </div>
          <a-switch v-model:checked="scopeForm.enabled" />
        </div>

        <div class="scope-section">
          <div class="scope-section-title">检索通道</div>
          <div class="scope-option-grid">
            <label class="scope-option">
              <span><FileText :size="16" /> 文档 Chunk</span>
              <a-switch v-model:checked="scopeForm.document_enabled" size="small" />
            </label>
            <label class="scope-option">
              <span><Network :size="16" /> 知识图谱</span>
              <a-switch v-model:checked="scopeForm.graph_enabled" size="small" />
            </label>
            <label class="scope-option">
              <span><TableProperties :size="16" /> 结构化证据</span>
              <a-switch v-model:checked="scopeForm.structured_enabled" size="small" />
            </label>
          </div>
        </div>

        <div class="scope-section">
          <div class="scope-section-title">科研证据策略</div>
          <div class="evidence-options">
            <a-checkbox v-model:checked="scopeForm.evidence_strict">STRICT 严格证据</a-checkbox>
            <a-checkbox v-model:checked="scopeForm.evidence_supporting"
              >SUPPORTING 支持证据</a-checkbox
            >
            <a-checkbox v-model:checked="scopeForm.evidence_candidate"
              >CANDIDATE 候选证据</a-checkbox
            >
            <a-checkbox v-model:checked="scopeForm.evidence_rejected">REJECTED 否定证据</a-checkbox>
          </div>
          <a-alert
            v-if="scopeForm.evidence_candidate || scopeForm.evidence_rejected"
            type="warning"
            show-icon
            message="候选或否定证据只用于展示不确定性与冲突，不能自动升级为已证实结论。"
          />
        </div>

        <div class="scope-section priority-row">
          <div>
            <div class="scope-section-title">检索优先级</div>
            <div class="scope-section-hint">数值越小越优先；全局重排仍会综合相关度和证据等级。</div>
          </div>
          <a-input-number v-model:value="scopeForm.priority" :min="0" :max="1000" />
        </div>

        <div class="scope-health-grid">
          <div v-for="item in healthMetrics" :key="item.label" class="scope-health-item">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
      </div>
    </a-modal>

    <!-- 加载状态 -->
    <div v-if="dbState.listLoading" class="loading-container">
      <a-spin size="large" />
      <p>正在加载知识库...</p>
    </div>

    <!-- 空状态显示 -->
    <ResourceEmptyState
      v-else-if="!databases || databases.length === 0"
      title="暂无知识库"
      description="创建知识库后，可以上传文件并配置检索、图谱和评估能力。"
      :icon="getKbTypeIcon('milvus')"
    >
      <template #actions>
        <a-button
          type="primary"
          size="large"
          class="lucide-icon-btn"
          :disabled="!kbTypes.length"
          @click="state.openNewDatabaseModel = true"
        >
          <template #icon>
            <Plus :size="16" />
          </template>
          创建知识库
        </a-button>
      </template>
    </ResourceEmptyState>

    <!-- 数据库列表 -->
    <ExtensionCardGrid v-else>
      <InfoCard
        v-for="database in filteredDatabases"
        :key="database.kb_id"
        :title="database.name"
        :subtitle="cardSubtitle(database)"
        :description="database.description || '暂无描述'"
        :tags="cardTags(database)"
        @click="navigateToDatabase(database)"
      >
        <template #icon>
          <component :is="getKbTypeIcon(database.kb_type || 'milvus')" :size="20" />
        </template>
        <template #card-more-action-corner>
          <a-menu @click="({ key }) => handleDatabaseAction(key, database)">
            <a-menu-item key="copy">
              <span class="lucide-menu-item">
                <Copy :size="15" />
                <span>复制 ID</span>
              </span>
            </a-menu-item>
            <a-menu-item key="edit">
              <span class="lucide-menu-item">
                <Pencil :size="15" />
                <span>编辑知识库</span>
              </span>
            </a-menu-item>
            <a-menu-divider />
            <a-menu-item key="delete" danger>
              <span class="lucide-menu-item">
                <Trash2 :size="15" />
                <span>删除知识库</span>
              </span>
            </a-menu-item>
          </a-menu>
        </template>
        <template #footer>
          <div class="scope-card-state">
            <span class="scope-state-dot" :class="scopeStateClass(database)"></span>
            <span>{{ scopeStateLabel(database) }}</span>
          </div>
          <a-button size="small" @click.stop="openScopeModal(database)">
            <Settings2 :size="14" /> 配置问答范围
          </a-button>
        </template>
      </InfoCard>
    </ExtensionCardGrid>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive, watch, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useConfigStore } from '@/stores/config'
import { useDatabaseStore } from '@/stores/database'
import { QuestionCircleOutlined } from '@ant-design/icons-vue'
import {
  Copy,
  FileText,
  Network,
  Pencil,
  Plus,
  Settings2,
  TableProperties,
  Trash2
} from '@lucide/vue'
import { message, Modal } from 'ant-design-vue'
import { databaseApi, knowledgeScopeApi, typeApi } from '@/apis/knowledge_api'
import PageHeader from '@/components/shared/PageHeader.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import EmbeddingModelSelector from '@/components/EmbeddingModelSelector.vue'
import ShareConfigForm from '@/components/ShareConfigForm.vue'
import ExtensionCardGrid from '@/components/extensions/ExtensionCardGrid.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import dayjs, { parseToShanghai } from '@/utils/time'
import AiTextarea from '@/components/AiTextarea.vue'
import { useChunkPresetOptions } from '@/composables/useChunkPresetOptions'
import { getKbTypeLabel, getKbTypeIcon, getKbTypeColor, kbUtils } from '@/utils/kb_utils'
import { DEFAULT_CHUNK_PRESET_ID } from '@/utils/chunkUtils'

const route = useRoute()
const router = useRouter()
const configStore = useConfigStore()
const databaseStore = useDatabaseStore()
const {
  chunkPresetSelectOptions: chunkPresetOptions,
  chunkPresetLoading,
  loadChunkPresetOptions,
  getChunkPresetDescription
} = useChunkPresetOptions()

const props = defineProps({
  embedded: { type: Boolean, default: false }
})

// 使用 store 的状态
const { databases, state: dbState } = storeToRefs(databaseStore)

const knowledgeActiveView = 'documents'
const knowledgeViewItems = [
  { key: 'documents', label: '文档知识库', path: '/extensions?tab=knowledge' }
]

const kbTypes = computed(() => Object.keys(supportedKbTypes.value))
const searchQuery = ref('')
const typeFilter = ref(null)
const scopeFilter = ref('all')
const scopeState = reactive({ scope: null, members: new Map(), loading: false })

const emptyScopeForm = () => ({
  enabled: false,
  document_enabled: true,
  graph_enabled: true,
  structured_enabled: true,
  evidence_strict: true,
  evidence_supporting: true,
  evidence_candidate: false,
  evidence_rejected: false,
  priority: 100,
  health_status: 'VALIDATING',
  health_details: {}
})

const scopeForm = reactive(emptyScopeForm())
const scopeModal = reactive({ open: false, saving: false, database: null })

const filteredDatabases = computed(() => {
  let list = databases.value
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase()
    list = list.filter(
      (db) =>
        db.name.toLowerCase().includes(q) ||
        (db.description && db.description.toLowerCase().includes(q))
    )
  }
  if (typeFilter.value) {
    list = list.filter((db) => (db.kb_type || 'milvus') === typeFilter.value)
  }
  if (scopeFilter.value !== 'all') {
    list = list.filter((db) => {
      const included = Boolean(scopeState.members.get(db.kb_id)?.enabled)
      return scopeFilter.value === 'included' ? included : !included
    })
  }
  return list
})

const loadDefaultScope = async () => {
  scopeState.loading = true
  try {
    const data = await knowledgeScopeApi.getDefaultScope()
    scopeState.scope = data.scope || null
    scopeState.members = new Map((data.members || []).map((item) => [item.kb_id, item]))
  } catch (error) {
    message.error(error.message || '默认问答范围加载失败')
  } finally {
    scopeState.loading = false
  }
}

const healthTag = (status) => {
  const map = {
    HEALTHY: { label: '健康', color: 'green' },
    DEGRADED: { label: '部分可用', color: 'orange' },
    UNAVAILABLE: { label: '不可用', color: 'red' },
    VALIDATING: { label: '待验证', color: 'blue' }
  }
  return map[status] || map.VALIDATING
}

const scopeStateLabel = (database) => {
  const member = scopeState.members.get(database.kb_id)
  if (!member?.enabled) return '未纳入默认问答'
  return `已纳入 · ${healthTag(member.health_status).label}`
}

const scopeStateClass = (database) => {
  const member = scopeState.members.get(database.kb_id)
  if (!member?.enabled) return 'is-off'
  if (member.health_status === 'HEALTHY') return 'is-healthy'
  if (member.health_status === 'UNAVAILABLE') return 'is-error'
  return 'is-warning'
}

const openScopeModal = (database) => {
  const member = scopeState.members.get(database.kb_id) || emptyScopeForm()
  Object.assign(scopeForm, emptyScopeForm(), member)
  scopeModal.database = database
  scopeModal.open = true
}

const closeScopeModal = () => {
  scopeModal.open = false
  scopeModal.database = null
  Object.assign(scopeForm, emptyScopeForm())
}

const healthMetrics = computed(() => {
  const details = scopeForm.health_details || {}
  return [
    { label: '文件', value: details.files ?? '—' },
    { label: 'Chunks', value: details.chunks ?? '—' },
    { label: '实体', value: details.entities ?? '—' },
    { label: '关系', value: details.triples ?? '—' },
    { label: '证据', value: details.evidence ?? '—' }
  ]
})

const saveScopeMember = async () => {
  if (!scopeModal.database || !scopeState.scope) return
  if (
    scopeForm.enabled &&
    !scopeForm.document_enabled &&
    !scopeForm.graph_enabled &&
    !scopeForm.structured_enabled
  ) {
    message.warning('纳入问答时至少启用一个检索通道')
    return
  }
  scopeModal.saving = true
  try {
    const payload = {
      expected_version: scopeState.scope.version,
      enabled: scopeForm.enabled,
      document_enabled: scopeForm.document_enabled,
      graph_enabled: scopeForm.graph_enabled,
      structured_enabled: scopeForm.structured_enabled,
      evidence_strict: scopeForm.evidence_strict,
      evidence_supporting: scopeForm.evidence_supporting,
      evidence_candidate: scopeForm.evidence_candidate,
      evidence_rejected: scopeForm.evidence_rejected,
      priority: scopeForm.priority
    }
    const data = await knowledgeScopeApi.updateDefaultScopeMember(
      scopeModal.database.kb_id,
      payload
    )
    scopeState.scope = data.scope
    scopeState.members.set(scopeModal.database.kb_id, {
      ...data.member,
      name: scopeModal.database.name,
      kb_type: scopeModal.database.kb_type
    })
    message.success(scopeForm.enabled ? '已纳入默认问答范围' : '已从默认问答范围停用')
    closeScopeModal()
  } catch (error) {
    if (error.response?.status === 409) {
      await loadDefaultScope()
      message.warning('范围配置已被其他管理员更新，已刷新到最新版本，请重新确认')
    } else {
      message.error(error.message || '问答范围保存失败')
    }
  } finally {
    scopeModal.saving = false
  }
}

const state = reactive({
  openNewDatabaseModel: false,
  formatTemplate: '',
  formatCsvMode: 'record'
})

// 按文件格式快速创建模板：只做表单预填与创建后引导，kb_type 恒为 milvus
const FORMAT_TEMPLATES = [
  {
    key: 'pdf_literature',
    label: '📄 PDF 文献证据库',
    description: 'MinerU 解析 + 语义分块；每个 chunk 自动附加文献标题、DOI/PMID、章节路径与证据级别（Results=direct，Discussion=inferred）。上传 PDF 时建议使用 MinerU 官方引擎。',
    nameSuffix: '文献证据库',
    apply: {
      chunk_preset_id: 'semantic',
      chunk_parser_config: { chunk_token_num: 512, literature_enrichment: true },
      format_template: 'pdf_literature'
    }
  },
  {
    key: 'csv_dataset',
    label: '📊 CSV 结构化数据集',
    description: '一行一条记录独立成块，保留行级来源；问答型 CSV（question,answer 两列）自动抽取问答对。',
    nameSuffix: '结构化数据集',
    apply: {
      chunk_preset_id: 'separator',
      chunk_parser_config: { chunk_token_num: 384, delimiter: '\n\n', overlapped_percent: 0 },
      format_template: 'csv_dataset'
    }
  },
  {
    key: 'graph_csv',
    label: '🕸 科研知识图谱',
    description: '节点 CSV + 关系 CSV + 审计 cypher；PostgreSQL 规范源、Neo4j/Milvus 双投影，创建后前往图谱页执行导入。',
    nameSuffix: '科研知识图谱',
    apply: { format_template: 'graph_csv' }
  }
]

const getFormatTemplate = (key) => FORMAT_TEMPLATES.find((item) => item.key === key) || null

const applyFormatTemplate = (key) => {
  if (state.formatTemplate === key) {
    state.formatTemplate = ''
    state.formatCsvMode = 'record'
    return
  }
  const template = getFormatTemplate(key)
  if (!template) return
  state.formatTemplate = key
  state.formatCsvMode = 'record'
  newDatabase.kb_type = 'milvus'
  newDatabase.chunk_preset_id = template.apply.chunk_preset_id || DEFAULT_CHUNK_PRESET_ID
  if (!newDatabase.name.trim()) {
    newDatabase.name = template.nameSuffix
  }
  if (!newDatabase.description.trim()) {
    newDatabase.description = `${template.label.replace(/^\S+\s/, '')}：${template.description}`
  }
}

const handleFormatCsvModeChange = () => {
  if (state.formatTemplate !== 'csv_dataset') return
  newDatabase.chunk_preset_id = state.formatCsvMode === 'qa' ? 'qa' : 'separator'
}

const createDefaultShareConfig = () => ({
  access_level: 'global',
  department_ids: [],
  user_uids: []
})

const shareConfig = ref(createDefaultShareConfig())
const shareConfigFormRef = ref(null)

const formatTemplates = FORMAT_TEMPLATES

const createEmptyDatabaseForm = () => ({
  name: '',
  description: '',
  embedding_model_spec: configStore.config?.embed_model,
  kb_type: '',
  storage: '',
  chunk_preset_id: DEFAULT_CHUNK_PRESET_ID,
  additional_params: {}
})

const newDatabase = reactive(createEmptyDatabaseForm())

const selectedPresetDescription = computed(() =>
  getChunkPresetDescription(newDatabase.chunk_preset_id)
)

// 支持的知识库类型
const supportedKbTypes = ref({})

// 有序的知识库类型
const orderedKbTypes = computed(() => supportedKbTypes.value)

const selectedKbTypeInfo = computed(() => supportedKbTypes.value[newDatabase.kb_type] || null)

const createParamOptions = computed(() => selectedKbTypeInfo.value?.create_params?.options || [])

const getKbTypeDescription = (typeInfo) => typeInfo?.description || ''

const resetCreateParamValues = () => {
  newDatabase.additional_params = {}
  for (const field of createParamOptions.value) {
    if ('default' in field) {
      newDatabase.additional_params[field.key] = field.default
    } else if (field.type === 'boolean') {
      newDatabase.additional_params[field.key] = false
    } else {
      newDatabase.additional_params[field.key] = ''
    }
  }
}

// 加载支持的知识库类型
const loadSupportedKbTypes = async () => {
  try {
    const data = await typeApi.getKnowledgeBaseTypes()
    supportedKbTypes.value = data.kb_types || {}
    newDatabase.kb_type = kbTypes.value[0] || ''
    resetCreateParamValues()
  } catch (error) {
    console.error('加载知识库类型失败:', error)
    supportedKbTypes.value = {}
    newDatabase.kb_type = ''
    resetCreateParamValues()
    message.error('加载知识库类型失败，请稍后重试')
  }
}

const resetNewDatabase = () => {
  Object.assign(newDatabase, createEmptyDatabaseForm())
  newDatabase.kb_type = kbTypes.value[0] || ''
  resetCreateParamValues()
  state.formatTemplate = ''
  state.formatCsvMode = 'record'
  shareConfig.value = createDefaultShareConfig()
}

const cancelCreateDatabase = () => {
  state.openNewDatabaseModel = false
  resetNewDatabase()
}

// 格式化创建时间
const formatCreatedTime = (createdAt) => {
  if (!createdAt) return ''
  const parsed = parseToShanghai(createdAt)
  if (!parsed) return ''

  const today = dayjs().startOf('day')
  const createdDay = parsed.startOf('day')
  const diffInDays = today.diff(createdDay, 'day')

  if (diffInDays === 0) {
    return '今天创建'
  }
  if (diffInDays === 1) {
    return '昨天创建'
  }
  if (diffInDays < 7) {
    return `${diffInDays} 天前创建`
  }
  if (diffInDays < 30) {
    const weeks = Math.floor(diffInDays / 7)
    return `${weeks} 周前创建`
  }
  if (diffInDays < 365) {
    const months = Math.floor(diffInDays / 30)
    return `${months} 个月前创建`
  }
  const years = Math.floor(diffInDays / 365)
  return `${years} 年前创建`
}

// 处理知识库类型改变
const handleKbTypeChange = (type) => {
  console.log('知识库类型改变:', type)
  resetNewDatabase()
  newDatabase.kb_type = type
  resetCreateParamValues()
}

// 构建请求数据（只负责表单数据转换）
const buildRequestData = () => {
  const requestData = {
    database_name: newDatabase.name.trim(),
    description: newDatabase.description?.trim() || '',
    kb_type: newDatabase.kb_type,
    additional_params: {}
  }

  if (selectedKbTypeInfo.value?.requires_embedding_model) {
    requestData.embedding_model_spec =
      newDatabase.embedding_model_spec || configStore.config.embed_model
    requestData.additional_params.chunk_preset_id =
      newDatabase.chunk_preset_id || DEFAULT_CHUNK_PRESET_ID
  }

  requestData.share_config = {
    access_level: shareConfig.value.access_level,
    department_ids:
      shareConfig.value.access_level === 'department' ? shareConfig.value.department_ids || [] : [],
    user_uids: shareConfig.value.access_level === 'user' ? shareConfig.value.user_uids || [] : []
  }

  // 根据类型添加特定配置
  if (['milvus'].includes(newDatabase.kb_type)) {
    if (newDatabase.storage) {
      requestData.additional_params.storage = newDatabase.storage
    }
  }

  // 按格式模板合并分块参数与模板标记
  const template = getFormatTemplate(state.formatTemplate)
  if (template && newDatabase.kb_type === 'milvus') {
    requestData.additional_params.format_template = template.apply.format_template
    if (template.key === 'csv_dataset') {
      requestData.additional_params.chunk_preset_id =
        state.formatCsvMode === 'qa' ? 'qa' : 'separator'
      requestData.additional_params.chunk_parser_config = {
        ...(template.apply.chunk_parser_config || {}),
        ...(state.formatCsvMode === 'qa'
          ? { chunk_token_num: 512, delimiter: '\n', overlapped_percent: 0 }
          : {})
      }
    } else if (template.apply.chunk_parser_config) {
      requestData.additional_params.chunk_parser_config = { ...template.apply.chunk_parser_config }
    }
  }

  for (const field of createParamOptions.value) {
    const value = newDatabase.additional_params[field.key]
    requestData.additional_params[field.key] = typeof value === 'string' ? value.trim() : value
  }

  return requestData
}

// 创建按钮处理
const handleCreateDatabase = async () => {
  if (!selectedKbTypeInfo.value) {
    message.error('知识库类型加载失败，无法创建知识库')
    return
  }

  for (const field of createParamOptions.value) {
    if (!field.required) continue
    const value = newDatabase.additional_params[field.key]
    if (value === undefined || value === null || (typeof value === 'string' && !value.trim())) {
      message.error(`请填写${field.label || field.key}`)
      return
    }
  }

  if (shareConfigFormRef.value) {
    const validation = shareConfigFormRef.value.validate()
    if (!validation.valid) {
      message.warning(validation.message)
      return
    }
  }

  const requestData = buildRequestData()
  try {
    const templateKey = state.formatTemplate
    const data = await databaseStore.createDatabase(requestData)
    resetNewDatabase()
    state.openNewDatabaseModel = false
    // 图谱模板创建成功后直接引导到图谱页执行 CSV 导入
    if (templateKey === 'graph_csv') {
      const createdKbId =
        data?.kb_id || data?.database?.kb_id || databaseStore.databases?.[0]?.kb_id || ''
      if (createdKbId) {
        router.push(`/extensions/knowledgebase/${createdKbId}?tab=graph`)
        message.info('知识库已创建，请在图谱页导入节点 CSV 与关系 CSV')
      }
    }
  } catch {
    // 错误已在 store 中处理
  }
}

const cardSubtitle = (database) => {
  const parts = []
  if (database.created_at) {
    parts.push(formatCreatedTime(database.created_at))
  }
  if (!kbUtils.isReadOnlyDatabase(database)) {
    parts.push(`${database.row_count || 0} 文件`)
  }
  return parts.join(' · ')
}

const cardTags = (database) => {
  const tags = [
    {
      name: getKbTypeLabel(database.kb_type || 'milvus'),
      color: getKbTypeColor(database.kb_type || 'milvus')
    }
  ]
  if (database.embedding_model_spec) {
    tags.push({
      name: database.embedding_model_spec.split('/').slice(-1)[0],
      color: 'blue'
    })
  }
  return tags
}

const navigateToDatabase = (database) => {
  router.push({ path: `/extensions/knowledgebase/${database.kb_id}` })
}

const copyDatabaseId = async (database) => {
  try {
    await navigator.clipboard.writeText(database.kb_id)
  } catch {
    const textArea = document.createElement('textarea')
    textArea.value = database.kb_id
    document.body.appendChild(textArea)
    textArea.select()
    document.execCommand('copy')
    document.body.removeChild(textArea)
  }
  message.success('知识库 ID 已复制')
}

const deleteDatabase = (database) => {
  Modal.confirm({
    title: '删除知识库',
    content: `确定要删除知识库“${database.name}”吗？此操作不可撤销。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        await databaseApi.deleteDatabase(database.kb_id)
        message.success('知识库已删除')
        await databaseStore.loadDatabases()
      } catch (error) {
        message.error(error.message || '删除失败')
        throw error
      }
    }
  })
}

const handleDatabaseAction = (key, database) => {
  if (key === 'copy') {
    copyDatabaseId(database)
    return
  }
  if (key === 'edit') {
    router.push({
      path: `/extensions/knowledgebase/${database.kb_id}`,
      query: { action: 'edit' }
    })
    return
  }
  if (key === 'delete') {
    deleteDatabase(database)
  }
}

watch(
  () => route.path,
  (newPath) => {
    if (newPath === '/extensions' && route.query.tab === 'knowledge') {
      databaseStore.loadDatabases()
    }
  }
)

onMounted(() => {
  loadChunkPresetOptions()
  loadSupportedKbTypes()
  databaseStore.loadDatabases()
  loadDefaultScope()
})

defineExpose({
  loading: computed(() => dbState.value.listLoading)
})
</script>

<style lang="less" scoped>
.database-container {
  :deep(.info-card-icon) {
    background: var(--gray-0);
  }
}

.scope-config {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.scope-summary,
.scope-enabled-row,
.priority-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.scope-kb-name,
.scope-section-title {
  color: var(--gray-900);
  font-size: 14px;
  font-weight: 600;
}

.scope-kb-id,
.scope-section-hint {
  margin-top: 3px;
  color: var(--gray-500);
  font-size: 12px;
}

.scope-kb-id {
  font-family: monospace;
}

.scope-section {
  padding: 14px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-10);
}

.scope-option-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
}

.scope-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);

  > span {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--gray-700);
    font-size: 12px;
  }
}

.evidence-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 12px 0;
}

.scope-health-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
}

.scope-health-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 9px;
  border-radius: 6px;
  background: var(--gray-50);
  color: var(--gray-500);
  font-size: 11px;

  strong {
    color: var(--gray-900);
    font-size: 16px;
  }
}

.scope-card-state {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-600);
  font-size: 12px;
}

.scope-state-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--gray-300);

  &.is-healthy {
    background: var(--color-success-500);
  }

  &.is-warning {
    background: var(--color-warning-500);
  }

  &.is-error {
    background: var(--color-error-500);
  }
}

@media (max-width: 768px) {
  .scope-option-grid,
  .evidence-options {
    grid-template-columns: 1fr;
  }

  .scope-health-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.new-database-modal {
  .new-database-form {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .form-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .form-section.compact-section {
    gap: 6px;
  }

  .form-grid {
    display: grid;
    gap: 16px;

    &.two-columns {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    &.three-columns {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    @media (max-width: 768px) {
      &.two-columns,
      &.three-columns {
        grid-template-columns: 1fr;
      }
    }
  }

  .full-width {
    width: 100%;
  }

  .compact-model-selector {
    height: 40px;
  }

  .section-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--gray-800);
  }

  .required-mark {
    margin-left: 2px;
    color: var(--color-error-500);
  }

  .field-hint {
    margin: 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--gray-600);
  }

  .description-hint {
    margin-top: -2px;
  }

  .chunk-preset-title-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .chunk-preset-help-icon {
    color: var(--gray-500);
    cursor: help;
    font-size: 14px;
  }

  .kb-type-guide {
    margin: 12px 0;
  }

  .privacy-config {
    display: flex;
    align-items: center;
    margin-bottom: 12px;
  }

  .kb-type-cards {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin: 4px 0 0;

    @media (max-width: 768px) {
      grid-template-columns: 1fr;
      gap: 10px;
    }

    .format-template-cards {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;

      .format-template-card {
        border: 1px solid var(--gray-150);
        border-radius: 12px;
        padding: 12px;
        cursor: pointer;
        transition: all 0.2s ease;
        background: var(--gray-0);

        &:hover {
          border-color: var(--main-color);
        }

        &.active {
          border-color: var(--main-color);
          background: color-mix(in srgb, var(--main-color) 6%, transparent);
        }

        .card-header {
          margin-bottom: 6px;

          .type-title {
            font-weight: 600;
            font-size: 13px;
          }
        }

        .card-description {
          font-size: 12px;
          color: var(--gray-500);
          line-height: 1.5;
        }

        .template-sub-option {
          margin-top: 8px;
        }
      }
    }

    .template-optional-mark {
      font-size: 12px;
      font-weight: 400;
      color: var(--gray-400);
      margin-left: 6px;
    }

    .kb-type-card {
      border: 1px solid var(--gray-150);
      border-radius: 12px;
      padding: 14px;
      cursor: pointer;
      transition: all 0.2s ease;
      background: var(--gray-0);
      position: relative;
      overflow: hidden;

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

      .card-header {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 10px;

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
      }

      .card-description {
        font-size: 13px;
        color: var(--gray-600);
        line-height: 1.5;
        margin-bottom: 0;
      }

      .deprecated-badge {
        background: var(--color-error-100);
        color: var(--color-error-600);
        font-size: 10px;
        font-weight: 600;
        padding: 2px 6px;
        border-radius: 4px;
        margin-left: auto;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        cursor: help;
        transition: all 0.2s ease;

        &:hover {
          background: var(--color-error-200);
          color: var(--color-error-700);
        }
      }
    }
  }

  .chunk-config {
    margin-top: 16px;
    padding: 12px 16px;
    background-color: var(--gray-25);
    border-radius: 6px;
    border: 1px solid var(--gray-150);

    h3 {
      margin-top: 0;
      margin-bottom: 12px;
      color: var(--gray-800);
    }

    .chunk-params {
      display: flex;
      flex-direction: column;
      gap: 12px;

      .param-row {
        display: flex;
        align-items: center;
        gap: 12px;

        label {
          min-width: 80px;
          font-weight: 500;
          color: var(--gray-700);
        }

        .param-hint {
          font-size: 12px;
          color: var(--gray-500);
          margin-left: 8px;
        }
      }
    }
  }
}

.database-container {
  padding: 0;
}

.loading-container {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  height: 300px;
  gap: 16px;
}

.new-database-modal {
  h3 {
    margin-top: 10px;
  }
}
</style>
