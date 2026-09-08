<template>
  <div class="wiki-detail layout-container">
    <div class="wiki-header">
      <a-button type="text" class="back-button" @click="router.push('/extensions?tab=knowledge')">
        <ArrowLeft :size="18" /> 返回知识库
      </a-button>
      <div class="wiki-heading">
        <div class="wiki-mark"><BookOpenCheck :size="25" /></div>
        <div>
          <div class="wiki-title-row">
            <h1>{{ detail.name || '动态 LLM-Wiki' }}</h1>
            <a-tag :color="statusMeta.color">{{ statusMeta.label }}</a-tag>
            <a-tag color="orange">DERIVED · NAVIGATION ONLY</a-tag>
          </div>
          <p>{{ detail.description || '由权威知识源编译的动态科研导航产品' }}</p>
          <code>{{ detail.wiki_id }}</code>
        </div>
      </div>
      <div class="header-actions">
        <a-button :loading="loading" @click="loadAll"><RefreshCw :size="15" /> 刷新</a-button>
        <a-button type="primary" :loading="building" @click="runBuild">
          <Hammer :size="15" /> 创建不可变构建
        </a-button>
      </div>
    </div>

    <a-alert
      class="authority-notice"
      type="info"
      show-icon
      message="权威边界已启用"
      description="Wiki 页面和导航词只参与召回扩展；最终回答必须重新检索绑定知识源，并引用原 PDF 锚点、CSV 行或规范图谱 Evidence。"
    />

    <a-spin :spinning="loading">
      <div class="metric-grid">
        <div class="metric-card">
          <span>权威知识源</span
          ><strong>{{ detail.sources?.filter((item) => item.enabled).length || 0 }}</strong>
        </div>
        <div class="metric-card">
          <span>发布页面</span><strong>{{ pages.length }}</strong>
        </div>
        <div class="metric-card">
          <span>已验证 Claim</span><strong>{{ verifiedClaimCount }}</strong>
        </div>
        <div class="metric-card">
          <span>当前发布</span
          ><strong class="metric-code">{{ shortId(detail.current_publication_id) }}</strong>
        </div>
      </div>

      <a-tabs v-model:active-key="activeTab" class="wiki-tabs">
        <a-tab-pane key="overview" tab="概览与来源">
          <div class="content-grid">
            <section class="panel">
              <div class="panel-title"><ShieldCheck :size="17" /> 权威来源绑定</div>
              <div v-if="detail.sources?.length" class="source-list">
                <div v-for="source in detail.sources" :key="source.binding_id" class="source-row">
                  <div>
                    <strong>{{ source.name }}</strong>
                    <div>
                      <code>{{ source.kb_id }}</code> · {{ source.kb_type }}
                    </div>
                  </div>
                  <a-tag :color="source.enabled ? 'green' : 'default'">
                    {{ source.enabled ? '已冻结到下一快照' : '已停用' }}
                  </a-tag>
                </div>
              </div>
              <a-empty v-else description="尚未绑定权威知识源" />
              <a-divider />
              <div class="update-settings">
                <div class="panel-title"><RefreshCw :size="16" /> 动态更新策略</div>
                <label>
                  <span>触发方式</span>
                  <a-select v-model:value="settings.update_mode">
                    <a-select-option value="MANUAL">仅手动构建</a-select-option>
                    <a-select-option value="ON_SOURCE_CHANGE">知识源变化后自动构建</a-select-option>
                    <a-select-option value="SCHEDULED">按周期自动构建</a-select-option>
                  </a-select>
                </label>
                <label>
                  <span>{{
                    settings.update_mode === 'SCHEDULED' ? '更新周期（秒）' : '变化去抖（秒）'
                  }}</span>
                  <a-input-number
                    v-model:value="settings.debounce_seconds"
                    :min="30"
                    :max="86400"
                    :disabled="settings.update_mode === 'MANUAL'"
                  />
                </label>
                <label class="switch-row">
                  <span>门禁通过后自动发布</span>
                  <a-switch v-model:checked="settings.auto_publish" />
                </label>
                <div class="settings-footer">
                  <span v-if="detail.pending_update_events">
                    {{ detail.pending_update_events }} 个待处理更新事件
                  </span>
                  <a-button type="primary" :loading="savingSettings" @click="saveSettings">
                    保存更新策略
                  </a-button>
                </div>
              </div>
            </section>
            <section class="panel">
              <div class="panel-title"><SearchCheck :size="17" /> 发布态导航冒烟测试</div>
              <a-input-search
                v-model:value="navigationQuestion"
                enter-button="测试导航"
                :loading="navigating"
                placeholder="例如：Wx 基因的等位变异"
                @search="testNavigation"
              />
              <div v-if="navigationHits.length" class="navigation-results">
                <div
                  v-for="hit in navigationHits"
                  :key="`${hit.publication_id}-${hit.entity_id}-${hit.entity_name}`"
                  class="nav-hit"
                >
                  <div>
                    <strong>{{ hit.entity_name || hit.page_path?.[0] }}</strong
                    ><span>{{ Math.round(hit.score * 100) }}%</span>
                  </div>
                  <p>{{ hit.expansion_terms?.join(' · ') }}</p>
                </div>
              </div>
              <a-empty v-else-if="navigationTested" description="当前发布版本没有导航命中" />
            </section>
          </div>
        </a-tab-pane>

        <a-tab-pane key="builds" tab="构建与发布">
          <a-table
            :data-source="detail.builds || []"
            :pagination="false"
            row-key="build_id"
            size="middle"
          >
            <a-table-column title="构建" key="build">
              <template #default="{ record }">
                <code>{{ shortId(record.build_id) }}</code>
                <div class="table-subtitle">{{ formatTime(record.created_at) }}</div>
              </template>
            </a-table-column>
            <a-table-column title="状态" key="status">
              <template #default="{ record }"
                ><a-tag :color="buildStatus(record.status).color">{{
                  buildStatus(record.status).label
                }}</a-tag></template
              >
            </a-table-column>
            <a-table-column title="产物" key="metrics">
              <template #default="{ record }">
                {{ record.metrics?.page_count || 0 }} 页 ·
                {{ record.metrics?.verified_claim_count || 0 }} Claim ·
                {{ record.metrics?.authority_evidence_count || 0 }} Evidence
              </template>
            </a-table-column>
            <a-table-column title="操作" key="actions" align="right">
              <template #default="{ record }">
                <a-space>
                  <a-button size="small" @click="inspectBuild(record.build_id)">核验</a-button>
                  <a-button
                    v-if="record.status === 'COMPLETED'"
                    size="small"
                    type="primary"
                    :loading="publishingId === record.build_id"
                    @click="publishBuild(record.build_id)"
                    >发布</a-button
                  >
                </a-space>
              </template>
            </a-table-column>
          </a-table>
          <a-empty v-if="!detail.builds?.length" description="尚无构建；先创建不可变快照和构建" />

          <div class="publication-title">发布历史（蓝绿指针可回滚）</div>
          <a-table
            :data-source="detail.publications || []"
            :pagination="false"
            row-key="publication_id"
            size="small"
          >
            <a-table-column title="发布 ID" data-index="publication_id">
              <template #default="{ text }"
                ><code>{{ shortId(text) }}</code></template
              >
            </a-table-column>
            <a-table-column title="状态" data-index="status">
              <template #default="{ text }"
                ><a-tag :color="text === 'ACTIVE' ? 'green' : 'default'">{{
                  text
                }}</a-tag></template
              >
            </a-table-column>
            <a-table-column title="验证 Claim" key="claims">
              <template #default="{ record }">{{
                record.manifest?.verified_claim_count || 0
              }}</template>
            </a-table-column>
            <a-table-column title="发布时间" data-index="published_at">
              <template #default="{ text }">{{ formatTime(text) }}</template>
            </a-table-column>
            <a-table-column title="操作" key="actions" align="right">
              <template #default="{ record }">
                <a-button
                  v-if="record.status !== 'ACTIVE'"
                  size="small"
                  :loading="rollbackId === record.publication_id"
                  @click="rollbackTo(record.publication_id)"
                  >回滚至此版本</a-button
                >
              </template>
            </a-table-column>
          </a-table>
        </a-tab-pane>

        <a-tab-pane key="pages" tab="页面与 Claim">
          <div v-if="inspectedBuildId" class="inspection-banner">
            正在核验构建 <code>{{ shortId(inspectedBuildId) }}</code>
            <a-button type="link" @click="inspectBuild('')">返回当前发布</a-button>
          </div>
          <div class="content-grid">
            <section class="panel">
              <div class="panel-title">导航页面（{{ pages.length }}）</div>
              <div class="page-list">
                <button
                  v-for="page in pages"
                  :key="page.page_revision_id"
                  type="button"
                  class="page-row"
                  @click="selectedPage = page"
                >
                  <span>{{ page.title }}</span
                  ><a-tag>{{ page.status }}</a-tag>
                </button>
              </div>
            </section>
            <section class="panel">
              <div class="panel-title">Claim 发布门禁（{{ claims.length }}）</div>
              <div class="claim-list">
                <div v-for="claim in claims" :key="claim.claim_revision_id" class="claim-row">
                  <strong>{{ claim.claim_text }}</strong>
                  <div>
                    <a-tag color="green">{{ claim.verification_status }}</a-tag>
                    <a-tag>{{ claim.evidence?.length || 0 }} Evidence</a-tag>
                  </div>
                </div>
              </div>
            </section>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-spin>

    <a-drawer
      :open="Boolean(selectedPage)"
      width="720"
      title="Wiki 导航页预览"
      @close="selectedPage = null"
    >
      <MarkdownPreview v-if="selectedPage" :content="selectedPage.content_markdown" />
    </a-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { ArrowLeft, BookOpenCheck, Hammer, RefreshCw, SearchCheck, ShieldCheck } from '@lucide/vue'
import { wikiApi } from '@/apis/knowledge_api'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import dayjs from '@/utils/time'

const route = useRoute()
const router = useRouter()
const wikiId = computed(() => String(route.params.wikiId || ''))
const detail = reactive({})
const pages = ref([])
const claims = ref([])
const loading = ref(false)
const building = ref(false)
const publishingId = ref('')
const rollbackId = ref('')
const activeTab = ref('overview')
const inspectedBuildId = ref('')
const selectedPage = ref(null)
const navigationQuestion = ref('')
const navigationHits = ref([])
const navigationTested = ref(false)
const navigating = ref(false)
const savingSettings = ref(false)
const settings = reactive({
  update_mode: 'MANUAL',
  debounce_seconds: 300,
  auto_publish: false
})

const statusMeta = computed(() => {
  const map = {
    DRAFT: { label: '草稿', color: 'blue' },
    BUILDING: { label: '构建中', color: 'processing' },
    READY_TO_PUBLISH: { label: '待发布', color: 'orange' },
    PUBLISHED: { label: '已发布', color: 'green' },
    BUILD_FAILED: { label: '构建失败', color: 'red' },
    SOURCE_CHANGED: { label: '来源已变化', color: 'gold' },
    UPDATE_QUEUED: { label: '更新已排队', color: 'processing' }
  }
  return map[detail.status] || { label: detail.status || '未知', color: 'default' }
})
const verifiedClaimCount = computed(
  () => claims.value.filter((item) => item.verification_status === 'VERIFIED').length
)

const shortId = (value) => (value ? `${String(value).slice(0, 12)}…` : '—')
const formatTime = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—')
const buildStatus = (status) => {
  const map = {
    QUEUED: { label: '排队中', color: 'blue' },
    RUNNING: { label: '构建中', color: 'processing' },
    COMPLETED: { label: '已完成', color: 'green' },
    FAILED: { label: '失败', color: 'red' }
  }
  return map[status] || { label: status, color: 'default' }
}

const loadArtifacts = async (buildId = '') => {
  const [pageData, claimData] = await Promise.all([
    wikiApi.pages(wikiId.value, buildId),
    wikiApi.claims(wikiId.value, buildId)
  ])
  pages.value = pageData.pages || []
  claims.value = claimData.claims || []
}

const loadAll = async () => {
  loading.value = true
  try {
    const data = await wikiApi.get(wikiId.value)
    Object.assign(detail, data.wiki || {})
    settings.update_mode = detail.update_mode || 'MANUAL'
    settings.debounce_seconds = detail.debounce_seconds || 300
    settings.auto_publish = Boolean(detail.policy?.auto_publish)
    await loadArtifacts(inspectedBuildId.value)
  } catch (error) {
    message.error(error.message || '动态 Wiki 加载失败')
  } finally {
    loading.value = false
  }
}

const saveSettings = async () => {
  savingSettings.value = true
  try {
    const data = await wikiApi.updateSettings(wikiId.value, { ...settings })
    Object.assign(detail, data.wiki || {})
    message.success('动态更新策略已保存')
  } catch (error) {
    message.error(error.message || '动态更新策略保存失败')
  } finally {
    savingSettings.value = false
  }
}

const runBuild = async () => {
  building.value = true
  try {
    const data = await wikiApi.build(wikiId.value)
    message.success(
      data.build?.reused ? '输入快照未变化，已复用既有构建' : '构建完成，请核验后发布'
    )
    inspectedBuildId.value = data.build?.build_id || ''
    activeTab.value = 'pages'
    await loadAll()
  } catch (error) {
    message.error(error.message || 'Wiki 构建失败')
  } finally {
    building.value = false
  }
}

const inspectBuild = async (buildId) => {
  inspectedBuildId.value = buildId
  activeTab.value = 'pages'
  await loadArtifacts(buildId)
}

const publishBuild = (buildId) => {
  Modal.confirm({
    title: '发布该不可变构建？',
    content: '发布会以原子方式切换导航指针；原版本保留，可随时回滚。Wiki 内容仍不会成为回答证据。',
    okText: '通过门禁并发布',
    onOk: async () => {
      publishingId.value = buildId
      try {
        await wikiApi.publish(wikiId.value, buildId)
        message.success('发布成功，新的导航版本已生效')
        inspectedBuildId.value = ''
        await loadAll()
      } finally {
        publishingId.value = ''
      }
    }
  })
}

const rollbackTo = (publicationId) => {
  Modal.confirm({
    title: '回滚导航发布指针？',
    content: '该操作不会重写或删除任何构建，仅把当前指针切换到所选不可变版本。',
    okText: '确认回滚',
    onOk: async () => {
      rollbackId.value = publicationId
      try {
        await wikiApi.rollback(wikiId.value, publicationId)
        message.success('导航版本已回滚')
        await loadAll()
      } finally {
        rollbackId.value = ''
      }
    }
  })
}

const testNavigation = async () => {
  if (!navigationQuestion.value.trim()) return
  navigating.value = true
  navigationTested.value = true
  try {
    const data = await wikiApi.navigate(wikiId.value, navigationQuestion.value.trim())
    navigationHits.value = data.navigation_hits || []
  } catch (error) {
    message.error(error.message || '导航测试失败')
  } finally {
    navigating.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped lang="less">
.wiki-detail {
  padding-bottom: 40px;
}

.wiki-header {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 18px;
  padding: 22px 0;
}

.back-button,
.header-actions,
.wiki-heading,
.wiki-title-row,
.panel-title {
  display: flex;
  align-items: center;
}

.wiki-heading {
  gap: 14px;
}

.wiki-mark {
  display: grid;
  width: 48px;
  height: 48px;
  place-items: center;
  border-radius: 12px;
  background: var(--color-primary-50);
  color: var(--color-primary-700);
}

.wiki-title-row {
  gap: 8px;

  h1 {
    margin: 0;
    color: var(--gray-950);
    font-size: 24px;
  }
}

.wiki-heading p {
  margin: 4px 0;
  color: var(--gray-600);
}

.header-actions {
  gap: 8px;
}

.authority-notice {
  margin-bottom: 18px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.metric-card {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 16px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-0);

  span {
    color: var(--gray-500);
    font-size: 12px;
  }

  strong {
    color: var(--gray-900);
    font-size: 24px;
  }

  .metric-code {
    font-size: 15px;
  }
}

.wiki-tabs {
  margin-top: 18px;
}

.content-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.panel {
  min-height: 240px;
  padding: 18px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-0);
}

.panel-title {
  gap: 7px;
  margin-bottom: 14px;
  color: var(--gray-900);
  font-weight: 600;
}

.source-list,
.navigation-results,
.page-list,
.claim-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 560px;
  overflow: auto;
}

.update-settings {
  display: grid;
  gap: 12px;
}

.update-settings label {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) minmax(190px, 1.4fr);
  align-items: center;
  gap: 12px;
  color: var(--gray-700);
  font-size: 13px;
}

.update-settings .ant-select,
.update-settings .ant-input-number {
  width: 100%;
}

.update-settings .switch-row {
  grid-template-columns: 1fr auto;
}

.settings-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  color: var(--color-warning-700);
  font-size: 12px;
}

.source-row,
.nav-hit,
.page-row,
.claim-row {
  padding: 11px 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-10);
}

.source-row,
.nav-hit > div,
.page-row,
.claim-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.source-row div div,
.table-subtitle,
.nav-hit p {
  margin: 3px 0 0;
  color: var(--gray-500);
  font-size: 12px;
}

.publication-title {
  margin: 28px 0 12px;
  color: var(--gray-900);
  font-size: 16px;
  font-weight: 600;
}

.inspection-banner {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--color-primary-50);
}

.page-row {
  width: 100%;
  color: var(--gray-800);
  text-align: left;
  cursor: pointer;
}

.claim-row {
  align-items: flex-start;
  flex-direction: column;
}

@media (max-width: 900px) {
  .wiki-header {
    grid-template-columns: 1fr;
  }

  .metric-grid,
  .content-grid {
    grid-template-columns: 1fr;
  }
}
</style>
