<template>
  <div class="mcp-cards-page extension-page-root">
    <PageShoulder search-placeholder="搜索 MCP..." v-model:search="searchQuery">
      <template #actions>
        <a-button type="primary" @click="handleMcpAdd" class="lucide-icon-btn">
          <Plus :size="14" />
          <span>添加 MCP</span>
        </a-button>
        <a-button class="lucide-icon-btn" @click="importModalVisible = true">
          <FileDown :size="14" />
          <span>导入配置</span>
        </a-button>
        <a-tooltip title="刷新 MCP" placement="bottom">
          <a-button class="lucide-icon-btn" :disabled="loading" @click="fetchServers">
            <RefreshCw :size="14" />
          </a-button>
        </a-tooltip>
      </template>
    </PageShoulder>

    <div
      v-if="filteredEnabledServers.length === 0 && filteredDisabledServers.length === 0"
      class="extension-card-grid-empty-state"
    >
      <a-empty
        :image="false"
        :description="searchQuery ? '无匹配 MCP' : '暂无 MCP，点击上方按钮添加'"
      />
    </div>

    <template v-else>
      <div v-if="filteredEnabledServers.length" class="extension-section-header">已添加</div>
      <ExtensionCardGrid v-if="filteredEnabledServers.length" :min-width="360">
        <InfoCard
          v-for="server in filteredEnabledServers"
          :key="server.slug"
          variant="mini"
          :title="cardTitle(server)"
          :description="server.description || '暂无描述'"
          @click="handleCardClick(server)"
        >
          <template #icon>
            <span class="info-card-emoji-icon">{{ server.icon || '🔌' }}</span>
          </template>
          <template #action>
            <button
              type="button"
              class="mcp-card-action mcp-card-action-danger"
              :disabled="isActionLoading(server)"
              :aria-label="server.created_by === 'system' ? '移除 MCP' : '删除 MCP'"
              @click.stop="handleRemoveServer(server)"
            >
              <Check :size="15" class="action-icon action-icon-check" />
              <Trash2 :size="15" class="action-icon action-icon-trash" />
            </button>
          </template>
        </InfoCard>
      </ExtensionCardGrid>

      <div v-if="filteredDisabledServers.length" class="extension-section-header">可添加</div>
      <ExtensionCardGrid v-if="filteredDisabledServers.length" :min-width="360">
        <InfoCard
          v-for="server in filteredDisabledServers"
          :key="server.slug"
          variant="mini"
          :title="cardTitle(server)"
          :description="server.description || '暂无描述'"
          @click="openBasicInfo(server)"
        >
          <template #icon>
            <span class="info-card-emoji-icon">{{ server.icon || '🔌' }}</span>
          </template>
          <template #action>
            <button
              type="button"
              class="mcp-card-action"
              :disabled="isActionLoading(server)"
              aria-label="添加 MCP"
              @click.stop="handleSetServerEnabled(server, true)"
            >
              <Plus :size="15" class="action-icon" />
            </button>
          </template>
        </InfoCard>
      </ExtensionCardGrid>
    </template>

    <a-modal
      v-model:open="basicInfoVisible"
      class="mcp-basic-info-modal"
      :footer="null"
      width="560px"
      :destroy-on-close="true"
      @cancel="closeBasicInfo"
    >
      <div v-if="previewServer" class="mcp-basic-info-panel">
        <div class="mcp-basic-info-header">
          <div class="mcp-basic-info-icon">
            <span>{{ previewServer.icon || '🔌' }}</span>
          </div>
          <div class="mcp-basic-info-title-area">
            <div class="mcp-basic-info-title">
              {{ formatExtensionCardTitle(previewServer.name) }}
            </div>
            <div class="mcp-basic-info-meta">
              <span>{{ previewServer.transport || '未知传输类型' }}</span>
              <span v-if="previewServer.created_by === 'system'" class="mcp-basic-info-tag">
                内置
              </span>
            </div>
          </div>
        </div>

        <div class="mcp-basic-info-body">
          <div class="mcp-basic-info-row">
            <label>描述</label>
            <span>{{ previewServer.description || '暂无描述' }}</span>
          </div>
          <div class="mcp-basic-info-row">
            <label>传输类型</label>
            <span>{{ previewServer.transport || '-' }}</span>
          </div>
          <div class="mcp-basic-info-row">
            <label>生命周期</label>
            <a-tag :color="lifecycleColor(previewServer.lifecycle_status)">
              {{ previewServer.lifecycle_status || 'UNKNOWN' }}
            </a-tag>
          </div>
          <div class="mcp-basic-info-row">
            <label>生产运行形态</label>
            <span>{{ previewServer.runtime_level || '-' }}</span>
          </div>
          <div class="mcp-basic-info-row">
            <label>数据/依赖策略</label>
            <span>
              {{ previewServer.data_access_level || 'PUBLIC' }} ·
              {{ previewServer.dependency_mode || 'OPTIONAL' }}
            </span>
          </div>
          <div
            v-if="Array.isArray(previewServer.tags) && previewServer.tags.length > 0"
            class="mcp-basic-info-row"
          >
            <label>标签</label>
            <span class="mcp-basic-info-tags">
              <a-tag v-for="tag in previewServer.tags" :key="tag">{{ tag }}</a-tag>
            </span>
          </div>
          <div class="mcp-basic-info-row">
            <label>创建人</label>
            <span>{{ previewServer.created_by || '-' }}</span>
          </div>
        </div>

        <div class="mcp-basic-info-footer">
          <a-button @click="closeBasicInfo">关闭</a-button>
          <a-button
            type="primary"
            class="lucide-icon-btn"
            :loading="isActionLoading(previewServer)"
            @click="handleSetServerEnabled(previewServer, true)"
          >
            <template #icon><Plus :size="14" /></template>
            添加
          </a-button>
        </div>
      </div>
    </a-modal>

    <a-modal
      v-model:open="importModalVisible"
      title="导入 MCP 配置"
      :confirm-loading="importLoading"
      width="640px"
      ok-text="解析并导入"
      cancel-text="取消"
      @ok="handleImportSubmit"
    >
      <div class="mcp-import-panel">
        <p class="mcp-import-hint">
          支持官方 Registry server.json、Claude/Cursor 配置和远程 URL。系统会保留全部候选；
          PyPI/npm/Cargo/NuGet/MCPB/Bioconda 只作为 OCI 构建源，不会在生产服务器直接执行。
          远程端点完成 SSRF/TLS/能力验证后才可启用。
        </p>
        <a-textarea
          v-model:value="importPayloadText"
          :rows="8"
          placeholder='{
  "mcpServers": {
    "my-mcp": { "command": "npx", "args": ["-y", "@scope/pkg@1.2.3"] }
  }
}'
        />
        <div v-if="importResults.length" class="mcp-import-results">
          <div v-for="item in importResults" :key="item.slug" class="mcp-import-result-item">
            <a-tag :color="resultTagColor(item.status)">{{ resultStatusText(item.status) }}</a-tag>
            <span class="mcp-import-result-name">{{ item.name || item.slug }}</span>
            <a-tag v-if="item.lifecycle_status" :color="lifecycleColor(item.lifecycle_status)">
              {{ item.lifecycle_status }}
            </a-tag>
            <span v-if="item.reason" class="mcp-import-result-reason">{{ item.reason }}</span>
            <ul v-if="item.warnings && item.warnings.length" class="mcp-import-result-warnings">
              <li v-for="(w, i) in item.warnings" :key="i">{{ w }}</li>
            </ul>
          </div>
        </div>
      </div>
    </a-modal>

    <McpFormModal
      v-model:open="formModalVisible"
      :edit-mode="false"
      @submitted="handleFormSubmitted"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { Check, FileDown, Plus, RefreshCw, Trash2 } from '@lucide/vue'
import { mcpApi } from '@/apis/mcp_api'
import ExtensionCardGrid from './ExtensionCardGrid.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import McpFormModal from './McpFormModal.vue'
import { formatExtensionCardTitle } from '@/utils/extensionDisplayName'

const router = useRouter()

const lifecycleColor = (status) =>
  ({
    READY: 'green',
    RESOLVED: 'blue',
    VERIFIED: 'cyan',
    BUILD_REQUIRED: 'orange',
    BLOCKED: 'red',
    FAILED: 'red'
  })[status] || 'default'

const cardTitle = (server) => {
  const name = formatExtensionCardTitle(server.name)
  return server.lifecycle_status && server.lifecycle_status !== 'READY'
    ? `${name} · ${server.lifecycle_status}`
    : name
}

const loading = ref(false)
const servers = ref([])
const searchQuery = ref('')
const formModalVisible = ref(false)
const basicInfoVisible = ref(false)
const previewServer = ref(null)
const actionLoadingSlug = ref('')

const filteredServers = computed(() => {
  const sorted = [...servers.value].sort((a, b) =>
    String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN', {
      sensitivity: 'base',
      numeric: true
    })
  )
  if (!searchQuery.value) return sorted
  const q = searchQuery.value.toLowerCase()
  return sorted.filter(
    (s) => s.name.toLowerCase().includes(q) || (s.description || '').toLowerCase().includes(q)
  )
})

const filteredEnabledServers = computed(() =>
  filteredServers.value.filter((item) => !!item.enabled)
)
const filteredDisabledServers = computed(() =>
  filteredServers.value.filter((item) => !item.enabled)
)

const navigateToDetail = (server) => {
  router.push({ path: `/extensions/mcp/${encodeURIComponent(server.slug)}` })
}

const handleCardClick = (server) => {
  if (server.enabled) {
    navigateToDetail(server)
    return
  }
  openBasicInfo(server)
}

const openBasicInfo = (server) => {
  previewServer.value = server
  basicInfoVisible.value = true
}

const closeBasicInfo = () => {
  basicInfoVisible.value = false
  previewServer.value = null
}

const isActionLoading = (server) => actionLoadingSlug.value === server?.slug

const handleMcpAdd = () => {
  formModalVisible.value = true
}

// ── 导入配置 ──
const importModalVisible = ref(false)
const importLoading = ref(false)
const importPayloadText = ref('')
const importResults = ref([])

const resultStatusText = (status) =>
  ({ created: '已导入', exists: '已存在', rejected: '被拒绝', failed: '失败' })[status] || status

const resultTagColor = (status) =>
  ({ created: 'green', exists: 'blue', rejected: 'orange', failed: 'red' })[status] || 'default'

const handleImportSubmit = async () => {
  const text = importPayloadText.value.trim()
  if (!text) {
    message.warning('请粘贴要导入的配置内容')
    return
  }
  try {
    importLoading.value = true
    let payload = text
    if (!/^https?:\/\//.test(text)) {
      payload = JSON.parse(text)
    }
    const result = await mcpApi.importMcpConfig(payload)
    if (result.success) {
      message.success(result.message || '导入完成')
      importResults.value = result.data || []
      await fetchServers()
    } else {
      message.error(result.message || '导入失败')
    }
  } catch (err) {
    // JSON 解析错误给出本地提示；接口错误透传后端原因（含策略拒绝说明）
    message.error(err instanceof SyntaxError ? `JSON 解析失败：${err.message}` : err.message || '导入失败')
  } finally {
    importLoading.value = false
  }
}

const handleFormSubmitted = async () => {
  formModalVisible.value = false
  await fetchServers()
}

const handleSetServerEnabled = async (server, enabled) => {
  try {
    actionLoadingSlug.value = server.slug
    if (enabled && server.lifecycle_status !== 'READY') {
      const verification = await mcpApi.testMcpServer(server.slug)
      if (!verification.success) {
        message.error(
          verification.message ||
            `MCP 当前为 ${server.lifecycle_status || 'UNKNOWN'}，尚未生成可启用的运行产物`
        )
        return
      }
    }
    const result = await mcpApi.updateMcpServerStatus(server.slug, enabled)
    if (result.success) {
      message.success(result.message || `MCP 已${enabled ? '添加' : '移除'}`)
      if (enabled) closeBasicInfo()
      await fetchServers()
    } else {
      message.error(result.message || '操作失败')
    }
  } catch (err) {
    message.error(err.message || '操作失败')
  } finally {
    actionLoadingSlug.value = ''
  }
}

const handleRemoveServer = (server) => {
  if (server.created_by === 'system') {
    handleSetServerEnabled(server, false)
    return
  }
  confirmDeleteServer(server)
}

const confirmDeleteServer = (server) => {
  Modal.confirm({
    title: '确认删除 MCP',
    content: `确定要删除 MCP "${server.name}" 吗？此操作不可撤销。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        actionLoadingSlug.value = server.slug
        const result = await mcpApi.deleteMcpServer(server.slug)
        if (result.success) {
          message.success('MCP 删除成功')
          await fetchServers()
        } else {
          message.error(result.message || '删除失败')
        }
      } catch (err) {
        message.error(err.message || '删除失败')
      } finally {
        actionLoadingSlug.value = ''
      }
    }
  })
}

const fetchServers = async () => {
  try {
    loading.value = true
    const result = await mcpApi.getMcpServers()
    if (result.success) {
      servers.value = result.data || []
    }
  } catch (err) {
    message.error(err.message || '获取 MCP 列表失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchServers()
})

defineExpose({ fetchServers, loading })
</script>

<style lang="less" scoped>
@import '@/assets/css/extensions.less';

.info-card-emoji-icon {
  font-size: 18px;
  line-height: 1;
}

.mcp-card-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  color: var(--main-color);
  cursor: pointer;
  transition:
    border-color 0.18s ease,
    background-color 0.18s ease,
    color 0.18s ease;

  &:hover,
  &:focus {
    outline: none;
    border-color: var(--main-200);
    background: var(--main-50);
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  &.mcp-card-action-danger {
    color: var(--color-success-700);

    .action-icon-trash {
      display: none;
    }

    &:hover,
    &:focus {
      border-color: var(--color-error-100);
      background: var(--color-error-50);
      color: var(--color-error-700);

      .action-icon-check {
        display: none;
      }

      .action-icon-trash {
        display: block;
      }
    }
  }
}

.action-icon {
  flex-shrink: 0;
}

.mcp-basic-info-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.mcp-basic-info-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.mcp-basic-info-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  border: 1px solid var(--gray-150);
  background: var(--main-50);
  color: var(--main-color);
  font-size: 18px;
}

.mcp-basic-info-title-area {
  min-width: 0;
}

.mcp-basic-info-title {
  overflow: hidden;
  color: var(--gray-900);
  font-size: 16px;
  font-weight: 700;
  line-height: 22px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mcp-basic-info-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
  color: var(--gray-500);
  font-size: 12px;
  line-height: 18px;
}

.mcp-basic-info-tag {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--gray-100);
  color: var(--gray-600);
  font-size: 11px;
  font-weight: 600;
}

.mcp-basic-info-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-25);
}

.mcp-basic-info-row {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  gap: 12px;
  color: var(--gray-700);
  font-size: 13px;
  line-height: 20px;

  label {
    color: var(--gray-500);
    font-weight: 600;
  }

  span {
    min-width: 0;
    overflow-wrap: anywhere;
  }
}

.mcp-basic-info-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.mcp-basic-info-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.mcp-import-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.mcp-import-hint {
  margin: 0;
  color: var(--text-color-secondary, #888);
  font-size: 12px;
  line-height: 1.6;
}

.mcp-import-results {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 220px;
  overflow-y: auto;
}

.mcp-import-result-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;

  .mcp-import-result-name {
    font-weight: 500;
  }

  .mcp-import-result-reason {
    color: #d46b08;
    font-size: 12px;
  }

  .mcp-import-result-warnings {
    width: 100%;
    margin: 0;
    padding-left: 18px;
    color: #ad6800;
    font-size: 12px;
  }
}
</style>
