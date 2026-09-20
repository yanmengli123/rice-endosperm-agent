<template>
  <div class="tools-cards-page extension-page-root">
    <PageShoulder search-placeholder="搜索工具..." v-model:search="searchQuery">
      <template #filters>
        <a-select
          v-model:value="selectedCategory"
          style="width: 120px"
          placeholder="全部分类"
          allow-clear
        >
          <a-select-option value="">全部分类</a-select-option>
          <a-select-option v-for="cat in categories" :key="cat" :value="cat">
            {{ categoryLabels[cat] || cat }}
          </a-select-option>
        </a-select>
      </template>
      <template #actions>
        <a-button type="primary" @click="openCreate"> <Plus :size="14" /> 新建工具 </a-button>
        <a-tooltip title="导入 OpenAPI" placement="bottom">
          <a-button class="lucide-icon-btn" @click="importOpen = true">
            <Upload :size="14" />
          </a-button>
        </a-tooltip>
        <a-tooltip title="刷新工具" placement="bottom">
          <a-button class="lucide-icon-btn" :disabled="loading" @click="fetchTools">
            <RefreshCw :size="14" />
          </a-button>
        </a-tooltip>
      </template>
    </PageShoulder>

    <div v-if="filteredTools.length === 0" class="extension-card-grid-empty-state">
      <a-empty :image="false" :description="searchQuery ? '无匹配工具' : '暂无工具'" />
    </div>

    <ExtensionCardGrid v-else>
      <InfoCard
        v-for="tool in filteredTools"
        :key="getToolSlug(tool)"
        :title="formatExtensionCardTitle(tool.name)"
        :subtitle="getToolSlug(tool)"
        :description="tool.description || '无描述'"
        :default-icon="getToolIcon(getToolSlug(tool)) || WrenchIcon"
        :tags="toolTags(tool)"
        :status="isCustom(tool) ? customCardStatus(tool) : null"
        @click="selectTool(tool)"
      >
        <template v-if="isCustom(tool)" #card-more-action-corner>
          <a-menu @click="({ key }) => handleCardAction(key, tool)">
            <a-menu-item key="test" :disabled="actionPending">{{
              testActionLabel(tool)
            }}</a-menu-item>
            <a-menu-item key="toggle" :disabled="actionPending">
              {{ tool.enabled ? '停用' : '启用' }}
            </a-menu-item>
            <a-menu-item key="edit" :disabled="actionPending">编辑</a-menu-item>
            <a-menu-divider />
            <a-menu-item key="delete" danger :disabled="actionPending">删除</a-menu-item>
          </a-menu>
        </template>
        <template v-if="!isCustom(tool)" #footer>
          <span class="buildin-hint">代码注册 · 只读（贡献请提 PR）</span>
        </template>
      </InfoCard>
    </ExtensionCardGrid>

    <a-modal
      v-model:open="detailVisible"
      :title="currentTool?.name || '工具详情'"
      :footer="null"
      width="640px"
    >
      <template v-if="currentTool">
        <div class="tool-detail-content detail-section-container">
          <div class="detail-section">
            <div class="section-content description">
              {{ currentTool.description || '无描述' }}
            </div>
          </div>

          <div class="detail-section" v-if="currentTool.config_guide">
            <div class="section-header">
              <FileText :size="14" />
              <span>配置说明</span>
            </div>
            <div class="section-content description config-guide">
              {{ currentTool.config_guide }}
            </div>
          </div>

          <template v-if="isCustom(currentTool)">
            <div class="detail-section">
              <div class="section-header">
                <Globe :size="14" />
                <span>连接</span>
              </div>
              <div class="section-content mono">
                {{ currentDetail?.spec?.method || 'GET' }}
                {{ (currentDetail?.spec?.base_url || '') + (currentDetail?.spec?.path || '/') }}
              </div>
            </div>
            <div class="detail-section">
              <div class="section-header">
                <Activity :size="14" />
                <span>状态</span>
              </div>
              <div class="section-content status-line">
                <a-tag :color="lifecycleColor(currentTool.lifecycle_status)">
                  {{ lifecycleLabel(currentTool.lifecycle_status) }}
                </a-tag>
                <a-tag :color="currentTool.enabled ? 'green' : 'default'">
                  {{ currentTool.enabled ? '已启用' : '已停用' }}
                </a-tag>
                <a-tag v-if="currentTool.tool_type">{{ currentTool.tool_type }}</a-tag>
                <a-tag v-if="currentTool.data_access_level">{{
                  currentTool.data_access_level
                }}</a-tag>
                <a-button
                  size="small"
                  :loading="actionPending"
                  @click="handleTestTool(currentTool)"
                >
                  测连
                </a-button>
              </div>
            </div>
            <div class="detail-section" v-if="lastHealthText">
              <div class="section-header">
                <Activity :size="14" />
                <span>最近测连</span>
              </div>
              <div class="section-content mono health">
                {{ lastHealthText }}
              </div>
            </div>
          </template>

          <div class="detail-section">
            <div class="section-header">
              <Tag :size="14" />
              <span>分类</span>
            </div>
            <div class="section-content">
              <a-tag :color="categoryColors[currentTool.category] || 'default'">
                {{ categoryLabels[currentTool.category] || currentTool.category }}
              </a-tag>
            </div>
          </div>

          <div class="detail-section">
            <div class="section-header">
              <Tags :size="14" />
              <span>标签</span>
            </div>
            <div class="section-content">
              <a-tag v-for="tag in currentTool.tags" :key="tag">{{ tag }}</a-tag>
              <span v-if="!currentTool.tags?.length" class="text-muted">无</span>
            </div>
          </div>

          <div class="detail-section" v-if="currentTool.args?.length">
            <div class="section-header">
              <List :size="14" />
              <span>参数</span>
            </div>
            <div class="section-content">
              <a-table
                :dataSource="currentTool.args"
                :columns="argColumns"
                size="small"
                :pagination="false"
                bordered
                class="args-table"
              />
            </div>
          </div>
        </div>
      </template>
    </a-modal>

    <ToolFormModal
      v-model:open="formOpen"
      :editMode="formEditMode"
      :editData="formEditData"
      @submitted="fetchTools"
    />
    <ToolImportModal v-model:open="importOpen" @submitted="fetchTools" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  Wrench,
  RefreshCw,
  FileText,
  Tag,
  Tags,
  List,
  Plus,
  Upload,
  Globe,
  Activity
} from '@lucide/vue'
import { toolApi } from '@/apis/tool_api'
import { getToolIcon } from '@/components/ToolCallingResult/toolRegistry'
import ExtensionCardGrid from './ExtensionCardGrid.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import ToolFormModal from './ToolFormModal.vue'
import ToolImportModal from './ToolImportModal.vue'
import { formatExtensionCardTitle } from '@/utils/extensionDisplayName'

const WrenchIcon = Wrench

const loading = ref(false)
const actionPending = ref(false)
const searchQuery = ref('')
const selectedCategory = ref('')
const tools = ref([])
const currentTool = ref(null)
const currentDetail = ref(null)
const detailVisible = ref(false)

const formOpen = ref(false)
const formEditMode = ref(false)
const formEditData = ref(null)
const importOpen = ref(false)

const categories = ['custom', 'buildin', 'knowledge', 'mysql', 'debug']
const categoryLabels = {
  custom: '自定义',
  buildin: '内置工具',
  knowledge: '知识库',
  mysql: 'MySQL',
  debug: '调试'
}
const categoryColors = {
  custom: 'cyan',
  buildin: 'blue',
  knowledge: 'purple',
  mysql: 'green',
  debug: 'orange'
}

const getToolSlug = (tool) => tool?.slug || tool?.id || ''
const isCustom = (tool) => tool?.source === 'custom'

const lifecycleLabels = { DRAFT: '草稿', READY: '测连通过', FAILED: '测连失败' }
const lifecycleLabel = (status) => lifecycleLabels[status] || status || '未知'
const lifecycleColor = (status) =>
  ({ DRAFT: 'orange', READY: 'green', FAILED: 'red' })[status] || 'default'

const customCardStatus = (tool) => {
  if (tool.lifecycle_status === 'READY' && tool.enabled)
    return { label: '已启用', level: 'success' }
  if (tool.lifecycle_status === 'READY') return { label: '可启用', level: 'info' }
  if (tool.lifecycle_status === 'FAILED') return { label: '测连失败', level: 'error' }
  return { label: '草稿', level: 'warning' }
}

const lastHealthText = computed(() => {
  const health = currentDetail.value?.last_health
  if (!health || !Object.keys(health).length) return ''
  if (health.ok)
    return `HTTP ${health.status_code} · ${health.duration_ms}ms · ${health.checked_at}`
  return `${health.error || '失败'} · ${health.checked_at}`
})

const toolTags = (tool) => {
  const tags = []
  if (tool.category) {
    tags.push({
      name: categoryLabels[tool.category] || tool.category,
      color: categoryColors[tool.category] || 'blue'
    })
  }
  ;(tool.tags || []).slice(0, 2).forEach((t) => tags.push(t))
  return tags
}

const argColumns = [
  { title: '参数名', dataIndex: 'name', key: 'name' },
  { title: '类型', dataIndex: 'type', key: 'type', width: 80 },
  { title: '描述', dataIndex: 'description', key: 'description' }
]

const filteredTools = computed(() => {
  let result = [...tools.value]
  // 自定义工具置顶，方便管理员直接管理
  result.sort((a, b) => Number(isCustom(b)) - Number(isCustom(a)))
  if (selectedCategory.value) {
    result = result.filter((t) => t.category === selectedCategory.value)
  }
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase()
    result = result.filter(
      (t) =>
        t.name.toLowerCase().includes(q) ||
        getToolSlug(t).toLowerCase().includes(q) ||
        t.description?.toLowerCase().includes(q) ||
        t.config_guide?.toLowerCase().includes(q)
    )
  }
  return result
})

const openCreate = () => {
  formEditMode.value = false
  formEditData.value = null
  formOpen.value = true
}

const selectTool = async (tool) => {
  currentTool.value = tool
  currentDetail.value = null
  detailVisible.value = true
  if (isCustom(tool)) {
    try {
      const result = await toolApi.getCustomTool(tool.slug)
      if (result.success) currentDetail.value = result.data
    } catch {
      /* 详情拉取失败不阻塞展示目录条目 */
    }
  }
}

const fetchTools = async () => {
  loading.value = true
  try {
    const result = await toolApi.getTools()
    tools.value = result?.data || []
  } catch {
    message.error('加载工具失败')
  } finally {
    loading.value = false
  }
}

const testActionLabel = (tool) =>
  tool.lifecycle_status === 'READY' ? '重新测连' : '测连（通过后可启用）'

const handleTestTool = async (tool) => {
  if (actionPending.value) return
  actionPending.value = true
  try {
    const result = await toolApi.testTool(tool.slug)
    if (!result.success) {
      message.error(result.message || '测连请求失败')
      return null
    }
    const data = result.data
    if (data.lifecycle_status === 'READY') {
      message.success(
        `测连通过（HTTP ${data.health?.status_code} · ${data.health?.duration_ms}ms），工具已就绪`,
        4
      )
    } else {
      message.warning(`测连失败：${data.health?.error || `HTTP ${data.health?.status_code}`}`, 6)
    }
    await fetchTools()
    const updated = tools.value.find((t) => t.slug === tool.slug)
    if (updated && currentTool.value?.slug === tool.slug) {
      currentTool.value = updated
      currentDetail.value = { ...(currentDetail.value || {}), ...updated, last_health: data.health }
    }
    return data
  } catch (err) {
    message.error(err.message || '测连请求失败')
    return null
  } finally {
    actionPending.value = false
  }
}

const handleToggleEnabled = async (tool) => {
  if (actionPending.value) return
  actionPending.value = true
  try {
    if (!tool.enabled && tool.lifecycle_status !== 'READY') {
      // 与 MCP 一致：未就绪工具先测连，通过才允许启用
      const testResult = await toolApi.testTool(tool.slug)
      if (!testResult.success || testResult.data?.lifecycle_status !== 'READY') {
        message.warning(
          testResult.data?.health?.error
            ? `测连未通过：${testResult.data.health.error}`
            : '测连未通过，无法启用',
          6
        )
        await fetchTools()
        return
      }
    }
    const result = await toolApi.setToolStatus(tool.slug, !tool.enabled)
    if (!result.success) {
      message.error(result.message || '状态更新失败')
      return
    }
    message.success(!tool.enabled ? '工具已启用' : '工具已停用')
    await fetchTools()
  } catch (err) {
    message.error(err.message || '状态更新失败')
  } finally {
    actionPending.value = false
  }
}

const handleDeleteTool = (tool) => {
  Modal.confirm({
    title: '确认删除自定义工具',
    content: `确定要删除 "${tool.name}"（${tool.slug}）吗？此操作不可撤销。仍被智能体引用时删除会被拒绝。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        const result = await toolApi.deleteTool(tool.slug)
        if (!result.success) {
          message.error(result.message || '删除失败')
          return
        }
        message.success('自定义工具已删除')
        await fetchTools()
      } catch (err) {
        message.error(err.message || '删除失败')
      }
    }
  })
}

const handleCardAction = (key, tool) => {
  if (key === 'test') handleTestTool(tool)
  else if (key === 'toggle') handleToggleEnabled(tool)
  else if (key === 'delete') handleDeleteTool(tool)
  else if (key === 'edit') {
    formEditMode.value = true
    formEditData.value = tool
    formOpen.value = true
  }
}

onMounted(fetchTools)

defineExpose({ fetchTools, loading })
</script>

<style lang="less" scoped>
@import '@/assets/css/extensions.less';

.tool-detail-content {
  max-height: 70vh;
  overflow-y: auto;
  padding: 0;
}

.args-table {
  :deep(.ant-table) {
    font-size: 12px;
  }
}

.config-guide {
  white-space: pre-line;
}

.buildin-hint {
  font-size: 12px;
  color: var(--gray-500);
}

.mono {
  font-family: var(--mono-font, monospace);
  font-size: 12px;
  word-break: break-all;
}

.status-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.health {
  color: var(--gray-600);
}
</style>
