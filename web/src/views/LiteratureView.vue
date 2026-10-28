<template>
  <div class="literature-view" :class="{ 'read-mode': readMode, 'eye-care': eyeCare }">
    <header class="lit-topbar">
      <h2 class="lit-brand">文献工作台</h2>
      <nav class="topbar-actions" aria-label="工作区视图">
        <button
          class="btn"
          :class="{ active: viewMode === 'reader' }"
          :disabled="!selectedRid"
          title="阅读工作区（选中文献后可进入）"
          @click="switchWorkspace('reader')"
        >
          <BookOpen :size="13" aria-hidden="true" /> 阅读工作区
        </button>
        <button
          class="btn"
          :class="{ active: viewMode === 'list' }"
          @click="switchWorkspace('list')"
        >
          <List :size="13" aria-hidden="true" /> 目录
        </button>
        <button
          class="btn"
          :class="{ active: viewMode === 'bibmetrics' }"
          title="文献计量：年度趋势/期刊/机构分布与共现网络"
          @click="switchWorkspace('bibmetrics')"
        >
          <BarChart3 :size="13" aria-hidden="true" /> 计量分析
        </button>
        <button
          class="btn"
          :class="{ active: viewMode === 'settings' }"
          @click="switchWorkspace('settings')"
        >
          <Settings :size="13" aria-hidden="true" /> 设置
        </button>
        <span class="toolbar-spacer"></span>
        <button
          class="btn"
          :aria-pressed="readMode"
          title="阅读模式：隐藏侧栏，把桌面让给内容"
          @click="toggleReadMode"
        >
          <Expand :size="13" aria-hidden="true" /> {{ readMode ? '退出阅读' : '进入阅读' }}
        </button>
        <button
          class="btn"
          :class="{ active: eyeCare }"
          :aria-pressed="eyeCare"
          title="护眼模式：米黄底色降低长时间阅读疲劳（仅作用于文献工作台）"
          @click="toggleEyeCare"
        >
          <Eye :size="13" aria-hidden="true" /> {{ eyeCare ? '退出护眼' : '护眼' }}
        </button>
        <button
          class="btn"
          :class="{ active: !sidebarPinned }"
          :aria-expanded="sidebarVisible"
          title="显示/隐藏侧栏"
          @click="toggleSidebar"
        >
          <PanelLeft :size="13" aria-hidden="true" /> {{ sidebarVisible ? '隐藏侧栏' : '显示侧栏' }}
        </button>
        <button
          class="btn"
          :class="{ active: splitSwapped }"
          :aria-pressed="splitSwapped"
          title="一键互换分栏：会话面板与文献页左右对调（偏好随账号记忆）"
          @click="toggleSplitSwap"
        >
          <Network :size="13" aria-hidden="true" /> {{ splitSwapped ? '会话在右' : '会话在左' }}
        </button>
      </nav>
    </header>

    <div class="lit-workspace" :class="{ 'has-sidebar': sidebarVisible }" :style="workspaceStyle">
      <template v-if="sidebarVisible">
        <LiteratureSidebar
          :drawer="drawer"
          :pinned="sidebarPinned"
          :rid="selectedRid"
          :sessions="sessionState.sessions.value"
          :selected-id="sessionState.selectedId.value"
          :selected-ids="sessionState.selectedIds.value"
          :all-selected="sessionState.allSelected.value"
          :archived="sessionState.archived.value"
          :busy="sessionState.busy.value"
          :papers="papers"
          :current-kind="currentKind"
          :search-query="searchQuery"
          :kind-chips="kindChips"
          :selected-rid="selectedRid"
          @select-drawer="selectDrawer"
          @toggle-pin="togglePin"
          @new-session="newSession"
          @toggle-all="sessionState.toggleAll"
          @delete-selected="sessionState.deleteSelected"
          @clear-archived="sessionState.clearArchived"
          @toggle-archived="sessionState.setArchived"
          @toggle-one="sessionState.toggleOne"
          @select-session="selectSessionFromSidebar"
          @restore="sessionState.restore"
          @remove="sessionState.removeOne"
          @select-kind="selectKind"
          @update:search-query="searchQuery = $event"
          @search="runSearch"
          @open-paper="openPaper"
          @open-workbench="switchWorkspace('workbench')"
          @open-import="switchWorkspace('import')"
          @open-tasks="switchWorkspace('tasks')"
          @open-graph="switchWorkspace('graph')"
          @open-diary="switchWorkspace('diary')"
        />
        <div
          class="sidebar-resizer"
          :class="{ resizing: isSidebarResizing }"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整文献侧栏宽度"
          :aria-valuemin="LIT_SIDEBAR_WIDTH_MIN"
          :aria-valuemax="LIT_SIDEBAR_WIDTH_MAX"
          :aria-valuenow="litSidebarWidth"
          tabindex="0"
          title="拖拽调整宽度，双击恢复默认"
          @pointerdown="startSidebarDrag"
          @dblclick="resetSidebarWidth"
          @keydown="onSidebarResizeKeydown"
        ></div>
      </template>
      <div class="lit-main">
        <div class="lit-toolbar">
          <select v-model="searchMode" aria-label="检索通道" @change="runSearch">
            <option value="catalog">目录（关键词 + 语义）</option>
            <option value="list">列表（分页）</option>
          </select>
          <label><input v-model="searchCache" type="checkbox" />检索缓存</label>
          <input
            v-model="rerankModel"
            placeholder="可选重排模型 provider:model（有用量）"
            aria-label="重排模型"
            style="max-width: 220px"
          />
          <span class="toolbar-spacer"></span>
          <button class="btn" @click="$refs.bibInput.click()">导入 Bib</button>
          <input ref="bibInput" hidden type="file" accept=".bib,.txt" @change="importBib" />
        </div>

        <LibraryWorkbench
          v-if="viewMode === 'workbench'"
          :resume-run="workbenchRun"
          @session="openSession"
          @editing-change="setEditing('global-cards', $event)"
        />
        <ImportPanel v-if="viewMode === 'import'" @imported="load" @track="trackTicket" />
        <BibliometricsPanel
          v-if="viewMode === 'bibmetrics'"
          @imported="load"
          @open-paper="openPaper"
        />
        <DiaryView v-if="viewMode === 'diary'" @editing-change="setEditing('diary', $event)" />
        <LiteratureGraphView v-if="viewMode === 'graph'" @open-paper="openPaper" />
        <section v-if="viewMode === 'settings'" class="workspace-settings">
          <h3>设置与治理</h3>
          <p>
            模型与密钥使用 Yuxi 统一配置，不复制 PaperAgent
            明文配置文件。插件使用受治理的工具、Skills 与 MCP。
          </p>
          <LiteratureSettingsPanel />
          <div class="settings-links">
            <button class="btn" @click="settingsOpen = true">个人模型、凭据与外观设置</button>
            <router-link to="/model-manage">智能体与模型管理</router-link>
            <router-link v-if="userStore.isAdmin" to="/extensions?tab=knowledge"
              >知识源 / PDF 解析契约</router-link
            >
            <router-link v-if="userStore.isAdmin" to="/extensions?tab=tools"
              >工具与扩展管理</router-link
            >
          </div>
          <p>
            桌面托盘、PyInstaller 与任意 Python ZIP
            插件不是服务端安全接口；这些平台机制不以伪造开关展示。
          </p>
        </section>
        <SettingsModal v-model:visible="settingsOpen" />
        <FloatingReaderWindow
          v-for="(win, index) in floatingReaders"
          :key="win.rid"
          :title="`阅读 · ${win.title}`"
          :initial="win.initial"
          :z-index="300 + index"
          @close="closeFloatingReader(win.rid)"
          @dock="dockFloatingReader(win.rid)"
        >
          <ReaderTabs
            :rid="win.rid"
            :paper="win.paper"
            @assistant-action="handleReaderAction"
            @editing-change="setEditing(`floating:${win.rid}`, $event)"
          />
        </FloatingReaderWindow>
        <div
          v-if="viewMode === 'reader' || viewMode === 'sessions'"
          class="reading-workspace"
          :class="{
            split:
              (viewMode === 'sessions' && selectedRid) ||
              (viewMode === 'reader' && comparisonRids.length),
            swapped: splitSwapped
          }"
        >
          <LiteratureSessions
            :session-state="sessionState"
            v-if="viewMode === 'sessions'"
            :rid="selectedRid"
            :thread-id="activeThreadId"
            :can-change-thread="confirmLeave"
            :reader-request="pendingReaderRequest"
            @thread-selected="onSessionSelected"
            @reader-evidence="openReaderEvidence"
            @reader-staged="readerStaged"
            @close="closeSessionsPanel"
          />
          <PaperDetail
            v-for="rid in (viewMode === 'reader'
              ? [selectedRid, ...comparisonRids]
              : [selectedRid]
            ).filter(Boolean)"
            :key="rid"
            :rid="rid"
            embedded
            @close="rid === selectedRid ? closeReader() : removeComparison(rid)"
            @session="openSession"
            @open-link="openReaderLink($event, rid)"
            :reader-target="readerTargets[rid] || null"
            :evidence-target="evidenceTargets[rid] || null"
            @assistant-action="handleReaderAction"
            @editing-change="setEditing(rid, $event)"
          />
          <p v-if="viewMode === 'reader' && !selectedRid">从文献列表选择一篇文献开始阅读。</p>
        </div>
        <div v-if="viewMode === 'reader' && selectedRid" class="reader-actions">
          <label
            >并排阅读（最多 5 篇）<select
              v-model="comparisonChoice"
              :disabled="comparisonRids.length >= 4"
              aria-label="添加并排文献"
            >
              <option value="">选择文献</option>
              <option
                v-for="paper in papers.filter(
                  (item) => item.rid !== selectedRid && !comparisonRids.includes(item.rid)
                )"
                :key="paper.rid"
                :value="paper.rid"
              >
                {{ paper.title || paper.rid }}
              </option>
            </select></label
          >
          <button
            class="btn"
            :disabled="!comparisonChoice || comparisonRids.length >= 4"
            @click="addComparison"
          >
            添加阅读器
          </button>
          <button class="btn" @click="popoutReader(selectedRid)">独立窗口阅读</button>
          <p v-if="readerLinkNotice" role="status">{{ readerLinkNotice }}</p>
        </div>
        <p v-if="searchNotice" role="status">{{ searchNotice }}</p>

        <div v-if="viewMode === 'tasks'" class="concepts-panel">
          <button class="btn" @click="onRefreshTasks">刷新任务与运行历史</button>
          <p v-if="tasksNotice" class="task-notice" role="status">{{ tasksNotice }}</p>
          <div v-if="qualityMetrics" class="task-hint" role="status" aria-label="质量指标">
            质量指标（{{ qualityMetrics.window_days }} 天）· 答案门禁通过
            {{ qualityMetrics.source_guard?.passed ?? 0 }}/{{
              qualityMetrics.source_guard?.total ?? 0
            }}
            · 拒绝率 {{ formatRate(qualityMetrics.source_guard?.refusal_rate) }} · 降级率
            {{ formatRate(qualityMetrics.source_guard?.degradation_rate) }} · 修复成功率
            {{ formatRate(qualityMetrics.source_guard?.repair_success_rate) }}
            <template v-if="qualityMetrics.ingest?.attempt_budget_exhausted">
              · 重试预算耗尽 {{ qualityMetrics.ingest.attempt_budget_exhausted }} 篇</template
            >
          </div>
          <h3>我的持久运行历史</h3>
          <ul class="concept-list">
            <li v-for="run in runs" :key="run.id" class="concept-card">
              {{ run.type }} · {{ run.id }} · {{ run.status }} · {{ run.updated_at }}
              <button v-if="run.rid" class="btn" @click="openPaper({ rid: run.rid })">
                打开文献
              </button>
              <button v-if="run.type === 'navigation'" class="btn" @click="resumeRun(run)">
                查看运行详情
              </button>
              <p v-if="run.error" :title="run.error" class="run-error">
                {{ describeRunError(run.error) }}
              </p>
            </li>
          </ul>
          <!-- 诊断视图默认折叠：原始 JSON 只在排查时展开，不直接糊在用户界面上 -->
          <details v-if="runDetail" class="run-detail">
            <summary>运行详情（诊断视图，展开查看原始数据）</summary>
            <pre>{{ JSON.stringify(runDetail, null, 2) }}</pre>
          </details>
          <h3>入库中（PDF 契约处理）</h3>
          <div v-if="!ingestTasks.length" class="lit-empty">暂无入库任务</div>
          <ul v-else class="concept-list">
            <li
              v-for="task in ingestTasks"
              :key="`ingest-${task.ref}`"
              class="concept-card"
              :class="{
                'task-highlight': highlightRef && task.ref === highlightRef,
                'task-failure': task.is_failure
              }"
            >
              <div class="concept-head">
                <strong>{{ task.detail || task.ref }}</strong>
                <span class="concept-count">{{ task.stage || task.status }}</span>
              </div>
              <div class="concept-meta">
                单号 {{ task.ref }} · 提交于 {{ task.created_at || '—' }}
                <template v-if="task.attempt > 0">· 已尝试 {{ task.attempt + 1 }} 次</template>
                <template v-if="task.processing_mode === 'parse_only'"
                  >· 仅解析（终点为草稿）</template
                >
                <template v-if="task.paper_rid">· 已登记文献</template>
              </div>
              <div v-if="task.error" class="task-error">{{ task.error }}</div>
              <!-- 失败分类给下一步动作：REJECTED 提示内容检查（管理员可改配置后重试）；FAILED 瞬时故障可直接重试 -->
              <p v-if="task.status === 'REJECTED'" class="task-hint">
                质量门禁拒绝：请检查文件内容（空白 / 乱码 / 非论文 PDF）；调整解析配置后可重试。
              </p>
              <p v-else-if="task.status === 'FAILED'" class="task-hint">
                供应商解析失败：可直接重试（瞬时故障自愈），或重新导入同一文件（内容指纹复用）。
              </p>
              <button v-if="task.paper_rid" class="btn" @click="openPaper({ rid: task.paper_rid })">
                打开文献
              </button>
              <button
                v-if="task.is_failure"
                class="btn"
                :disabled="retryingIngest === task.ref"
                @click="retryIngest(task)"
              >
                {{ retryingIngest === task.ref ? '重试中…' : '重试入库' }}
              </button>
            </li>
          </ul>
          <h3>处理任务</h3>
          <div v-if="!domainTasks.length" class="lit-empty">暂无文献任务</div>
          <ul v-else class="concept-list">
            <li
              v-for="task in domainTasks"
              :key="`${task.type}-${task.ref}`"
              class="concept-card"
              :class="{ 'task-failure': task.is_failure }"
            >
              <div class="concept-head">
                <strong>{{ task.type }} · {{ task.ref }}</strong>
                <span class="concept-count">{{ task.stage || task.status }}</span>
              </div>
              <div v-if="task.attempt > 0" class="concept-meta">
                已尝试 {{ task.attempt + 1 }} 次
              </div>
              <div v-if="task.error" class="task-error">{{ task.error }}</div>
            </li>
          </ul>
        </div>

        <div v-if="viewMode === 'list'" class="directory-filters">
          <label
            >期刊<input v-model="filters.journal" maxlength="512" @keyup.enter="runSearch"
          /></label>
          <label
            >年份从<input v-model="filters.year_min" type="number" min="1000" max="9999"
          /></label>
          <label>至<input v-model="filters.year_max" type="number" min="1000" max="9999" /></label>
          <select v-model="filters.source" aria-label="文献来源">
            <option value="">所有来源</option>
            <option value="pdf">关联 PDF 知识源</option>
            <option value="catalog">独立目录 / Markdown</option>
          </select>
          <select v-model="filters.quartile" aria-label="期刊分区">
            <option value="">所有分区</option>
            <option v-for="q in ['Q1', 'Q2', 'Q3', 'Q4']" :key="q" :value="q">{{ q }}</option>
            <option value="none">未知分区</option>
          </select>
          <label>最低 IF<input v-model="filters.min_if" type="number" min="0" step="0.1" /></label>
          <select v-model="filters.sort" aria-label="目录排序">
            <option value="updated">最近更新</option>
            <option value="year">年份</option>
            <option value="cited">外部被引</option>
            <option value="rank">PaperRank</option>
            <option value="title">标题</option>
          </select>
          <button class="btn" @click="applyFilters">应用目录筛选</button>
        </div>

        <div
          v-if="viewMode === 'list' && loading"
          class="lit-loading"
          role="status"
          aria-live="polite"
        >
          加载中…
        </div>
        <div v-else-if="viewMode === 'list' && !papers.length" class="lit-empty">
          暂无可访问文献。可在知识库中上传 PDF，完成证据处理后自动进入文献库；也可导入 Bib。
          <router-link v-if="userStore.isAdmin" to="/extensions?tab=knowledge"
            >前往知识库</router-link
          >
        </div>

        <ul v-else-if="viewMode === 'list'" class="paper-list">
          <li
            v-for="paper in papers"
            :key="paper.rid"
            class="paper-card"
            @click="openPaper(paper)"
            @mouseenter="previewRid = paper.rid"
            @mouseleave="previewRid = ''"
          >
            <div
              class="paper-title"
              role="button"
              tabindex="0"
              :aria-label="`打开文献 ${paper.title || paper.rid}`"
              @click="openPaper(paper)"
              @keydown.enter.prevent="openPaper(paper)"
              @keydown.space.prevent="openPaper(paper)"
            >
              <span class="kind-badge" :data-kind="paper.kind">{{ kindLabel(paper.kind) }}</span>
              {{ paper.title || paper.rid }}
              <!-- 快速预览（对标 PaperAgent §5.3 悬停行预览）：只展示列表已返回的字段，
               不在悬停时取原文，避免触发大体积产物读取与额外鉴权请求。 -->
              <div v-if="previewRid === paper.rid" class="paper-preview">
                <span v-if="paper.journal">期刊：{{ paper.journal }}</span>
                <span v-if="paper.pub_year">年份：{{ paper.pub_year }}</span>
              </div>
            </div>
            <div class="paper-actions" @click.stop>
              <button class="btn" :disabled="askLoading === paper.rid" @click="askPaper(paper)">
                {{ askLoading === paper.rid ? '建立中…' : '问答' }}
              </button>
            </div>
            <div class="paper-meta">
              <span v-if="paper.doi">DOI: {{ paper.doi }}</span>
              <span v-if="paper.pub_year">{{ paper.pub_year }}</span>
              <span v-if="paper.journal">{{ paper.journal }}</span>
              <span v-if="paper.external_cited != null">被引 {{ paper.external_cited }}</span>
              <span v-if="paper.quartile">JCR {{ paper.quartile }}</span>
              <span v-if="paper.impact_factor">IF {{ paper.impact_factor }}</span>
            </div>
          </li>
        </ul>

        <div class="lit-pagination" v-if="viewMode === 'list' && total > pageSize">
          <button class="btn" :disabled="page <= 1" @click="gotoPage(page - 1)">上一页</button>
          <span>{{ page }} / {{ totalPages }}</span>
          <button class="btn" :disabled="page >= totalPages" @click="gotoPage(page + 1)">
            下一页
          </button>
        </div>
      </div>
    </div>
    <!-- 会话面板挂载时其头部已显示同一条提示；此处仅在面板不可见（其他视图残留
         的会话提示）时兜底渲染，避免同一条提示双渲染且全宽条压住聊天输入区。 -->
    <div
      v-if="sessionState.notice.value && viewMode !== 'sessions'"
      class="workspace-notice"
      role="status"
    >
      <span class="notice-text">{{ sessionState.notice.value }}</span>
      <button class="notice-close" aria-label="关闭提示" @click="sessionState.notice.value = ''">
        ×
      </button>
    </div>
    <LiteratureStatusBar />
  </div>
</template>

<script>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { matchesReaderVersion, readerCapture } from '@/utils/literature_assistant'
import { message } from 'ant-design-vue'
import { BookOpen, List, Network, Settings, Expand, PanelLeft, Eye, BarChart3 } from '@lucide/vue'
import { useRoute, onBeforeRouteLeave } from 'vue-router'
import { apiGet, apiPost } from '@/apis/base'
import {
  listLiteraturePapers,
  getLiteraturePaper,
  literatureWorkspaceApi
} from '@/apis/literature_api'
import PaperDetail from '@/components/literature/PaperDetail.vue'
import LibraryWorkbench from '@/components/literature/LibraryWorkbench.vue'
import LiteratureStatusBar from '@/components/literature/LiteratureStatusBar.vue'
import ImportPanel from '@/components/literature/ImportPanel.vue'
import BibliometricsPanel from '@/components/literature/BibliometricsPanel.vue'
import LiteratureSessions from '@/components/literature/LiteratureSessions.vue'
import LiteratureSettingsPanel from '@/components/literature/LiteratureSettingsPanel.vue'
import FloatingReaderWindow from '@/components/literature/FloatingReaderWindow.vue'
import ReaderTabs from '@/components/literature/ReaderTabs.vue'
import { useColumnResize } from '@/composables/useColumnResize'
import { confirmAction, useLiteratureSessions } from '@/composables/useLiteratureSessions'
import { useLiteratureTaskFeed } from '@/composables/useLiteratureTaskFeed'
import {
  LIT_SIDEBAR_WIDTH_DEFAULT,
  LIT_SIDEBAR_WIDTH_MAX,
  LIT_SIDEBAR_WIDTH_MIN,
  clampLitSidebarWidth,
  readLitChrome,
  writeLitChrome
} from '@/utils/literature_chrome'
import LiteratureSidebar from '@/components/literature/LiteratureSidebar.vue'
import SettingsModal from '@/components/SettingsModal.vue'
import DiaryView from '@/views/DiaryView.vue'
import LiteratureGraphView from '@/views/LiteratureGraphView.vue'
import { useUserStore } from '@/stores/user'

const KIND_CHIPS = [
  { value: '', label: '全部' },
  { value: 'paper', label: '论文' },
  { value: 'thesis', label: '学位论文' },
  { value: 'book', label: '书籍' },
  { value: 'chapter', label: '章节' },
  { value: 'patent', label: '专利' },
  { value: 'standard', label: '标准' }
]

const KIND_LABELS = Object.fromEntries(KIND_CHIPS.map((c) => [c.value, c.label]))

export default {
  name: 'LiteratureView',
  components: {
    BookOpen,
    List,
    Network,
    Settings,
    Expand,
    PanelLeft,
    PaperDetail,
    LibraryWorkbench,
    ImportPanel,
    LiteratureSessions,
    DiaryView,
    LiteratureGraphView,
    SettingsModal,
    LiteratureSettingsPanel,
    FloatingReaderWindow,
    ReaderTabs,
    LiteratureSidebar,
    Eye,
    BarChart3,
    BibliometricsPanel,
    LiteratureStatusBar
  },
  setup() {
    const userStore = useUserStore()
    const papers = ref([])
    const total = ref(0)
    const page = ref(1)
    const pageSize = 20
    const loading = ref(false)
    const searchQuery = ref('')
    const searchMode = ref('catalog')
    const searchCache = ref(true)
    const rerankModel = ref('')
    const searchNotice = ref('')
    const currentKind = ref('')
    const previewRid = ref('')
    const askLoading = ref('')

    // ── Workspace chrome (PaperAgent reading-desk parity) ──
    // The domain deck owns the full desktop width; the outer chat sidebar is
    // collapsed declaratively via route meta (compactShell), not by writing
    // the persisted preference from this component.
    const chrome = ref(
      readLitChrome(
        {
          getItem: (key) => window.localStorage.getItem(key),
          setItem: (key, value) => window.localStorage.setItem(key, value)
        },
        userStore.uid
      )
    )
    const drawer = computed(() => chrome.value.drawer)
    const readMode = computed(() => chrome.value.readMode)
    const sidebarPinned = computed(() => chrome.value.pinned)
    const litSidebarWidth = computed(() => chrome.value.sidebarWidth)
    // Reading mode hides the deck entirely; otherwise it follows the pin.
    const sidebarVisible = computed(() => !readMode.value && sidebarPinned.value)
    const workspaceStyle = computed(() => ({
      gridTemplateColumns: sidebarVisible.value
        ? `${chrome.value.sidebarWidth}px 4px minmax(0, 1fr)`
        : 'minmax(0, 1fr)'
    }))
    const sessionState = useLiteratureSessions()
    const persistChrome = () =>
      writeLitChrome(
        {
          getItem: (key) => window.localStorage.getItem(key),
          setItem: (key, value) => window.localStorage.setItem(key, value)
        },
        userStore.uid,
        chrome.value
      )
    const DRAWER_KEYS = ['sessions', 'papers', 'workbench', 'diary']
    // Reject unknown keys here as well as in normalizeLitChrome: an unknown drawer
    // would render an empty deck, so the boundary check must not rely on storage alone.
    const selectDrawer = (key) => {
      if (!DRAWER_KEYS.includes(key)) return
      chrome.value = { ...chrome.value, drawer: key, pinned: true }
      persistChrome()
    }
    const togglePin = () => {
      chrome.value = { ...chrome.value, pinned: !chrome.value.pinned }
      persistChrome()
    }
    const toggleSidebar = () => {
      chrome.value = { ...chrome.value, pinned: !sidebarVisible.value }
      if (readMode.value) chrome.value.readMode = false
      persistChrome()
    }
    // 分栏互换：会话面板与文献页左右对调（chrome 持久化，跨会话记忆）
    const splitSwapped = computed(() => chrome.value.splitSwapped === true)
    const toggleSplitSwap = () => {
      chrome.value = { ...chrome.value, splitSwapped: !splitSwapped.value }
      persistChrome()
    }
    const toggleReadMode = () => {
      chrome.value = { ...chrome.value, readMode: !readMode.value }
      persistChrome()
    }
    // 护眼模式：仅作用于文献工作台的暖色调层（平台品牌主题不受影响），偏好随 chrome 持久化
    const eyeCare = computed(() => chrome.value.eyeCare === true)
    const toggleEyeCare = () => {
      chrome.value = { ...chrome.value, eyeCare: !eyeCare.value }
      persistChrome()
    }
    const setSidebarWidth = (value) => {
      chrome.value = { ...chrome.value, sidebarWidth: clampLitSidebarWidth(value) }
      persistChrome()
    }
    const resetSidebarWidth = () => setSidebarWidth(LIT_SIDEBAR_WIDTH_DEFAULT)
    let resizeStartWidth = chrome.value.sidebarWidth
    const { isDragging: isSidebarResizing, startDrag: startSidebarDrag } = useColumnResize({
      onStart: () => {
        resizeStartWidth = chrome.value.sidebarWidth
      },
      onResize: (clientX, startClientX) =>
        setSidebarWidth(resizeStartWidth + (clientX - startClientX))
    })
    // Keyboard parity with AppLayout: arrows nudge, Shift for coarse, Home/End to bounds, Enter resets.
    const onSidebarResizeKeydown = (event) => {
      const step = 8 * (event.shiftKey ? 4 : 1)
      const actions = {
        ArrowLeft: () => setSidebarWidth(chrome.value.sidebarWidth - step),
        ArrowRight: () => setSidebarWidth(chrome.value.sidebarWidth + step),
        Home: () => setSidebarWidth(LIT_SIDEBAR_WIDTH_MIN),
        End: () => setSidebarWidth(LIT_SIDEBAR_WIDTH_MAX),
        Enter: resetSidebarWidth
      }
      const action = actions[event.key]
      if (!action) return
      event.preventDefault()
      action()
    }
    const newSession = async (kind) => {
      if (!(await confirmLeave())) return
      return sessionState.perform(async () => {
        const data = await literatureWorkspaceApi.createSession(
          kind,
          kind === 'paper' ? selectedRid.value : ''
        )
        sessionState.archived.value = false
        await sessionState.refresh()
        openSession(data.thread.id, true)
      })
    }
    const selectSessionFromSidebar = (id) => openSession(id)
    const selectedRid = ref('')
    const comparisonRids = ref([])
    const comparisonChoice = ref('')
    const editingKeys = ref([])
    const setEditing = (key, value) => {
      editingKeys.value = editingKeys.value.filter((item) => item !== key)
      if (value) editingKeys.value.push(key)
    }
    const confirmLeave = async () =>
      !editingKeys.value.length || (await confirmAction('离开当前工作区会放弃未保存编辑，继续？'))
    const beforeUnload = (event) => {
      if (!editingKeys.value.length) return
      event.preventDefault()
      event.returnValue = ''
    }
    onBeforeRouteLeave(() => confirmLeave())
    /**
     * 卡片「问答」快捷入口（对标 PaperAgent §5.1 文献卡 `💬 问答`）：
     * 建立本文专属会话并切到会话区。已存在本文会话时直接复用，避免重复创建。
     */
    const askPaper = async (paper) => {
      if (askLoading.value || !(await confirmLeave())) return
      askLoading.value = paper.rid
      try {
        const existing = await literatureWorkspaceApi.sessions(false)
        const reuse = (existing.items || []).find(
          (item) => item.session?.kind === 'paper' && item.session?.rid === paper.rid
        )
        if (reuse) {
          // 顺序要紧：openPaper 会切到阅读区，必须先定位文献再开会话，
          // 否则视图会被 openPaper 覆盖，用户点了「问答」却停在阅读器。
          selectedRid.value = paper.rid
          openSession(reuse.id, true)
          return
        }
        const created = await literatureWorkspaceApi.createSession('paper', paper.rid)
        if (!created?.thread?.id) throw new Error('会话建立未返回线程标识')
        selectedRid.value = paper.rid
        openSession(created.thread.id, true)
      } catch (error) {
        searchNotice.value = error?.message || '建立本文会话失败'
      } finally {
        askLoading.value = ''
      }
    }

    const addComparison = () => {
      const rid = comparisonChoice.value
      if (
        !rid ||
        rid === selectedRid.value ||
        comparisonRids.value.includes(rid) ||
        comparisonRids.value.length >= 4
      )
        return
      comparisonRids.value.push(rid)
      comparisonChoice.value = ''
    }
    const removeComparison = (rid) => {
      // PaperDetail confirms unsaved edits before emitting close.
      comparisonRids.value = comparisonRids.value.filter((item) => item !== rid)
      setEditing(rid, false)
    }
    const readerLinkNotice = ref('')
    const readerTargets = ref({})
    const evidenceTargets = ref({}),
      pendingReaderRequest = ref(null)
    let readerActionGeneration = 0
    const handleReaderAction = async (input) => {
      if (askLoading.value || !(await confirmLeave())) return
      // 阅读动作进入分栏问答：默认收起文献侧栏，把桌面宽度让给问答与原文
      //（用户可随时点「显示侧栏」恢复，阅读模式本就无侧栏不受影响）
      if (sidebarPinned.value && !readMode.value) {
        chrome.value = { ...chrome.value, pinned: false }
        persistChrome()
      }
      const ticket = ++readerActionGeneration
      const origin = { rid: selectedRid.value, thread: activeThreadId.value, mode: viewMode.value }
      const stillHere = () =>
        ticket === readerActionGeneration &&
        origin.rid === selectedRid.value &&
        origin.thread === activeThreadId.value &&
        origin.mode === viewMode.value
      askLoading.value = input.rid
      try {
        const capture = readerCapture(input)
        const created = await literatureWorkspaceApi.createSession('paper', capture.rid)
        if (!stillHere()) return
        if (
          !created?.thread?.id ||
          created.session?.kind !== 'paper' ||
          created.session.rid !== capture.rid
        )
          throw new Error('服务端未返回匹配的本文会话')
        if (!matchesReaderVersion(capture, created.context))
          throw new Error('文献版本已更新，请刷新阅读器后重新选择')
        sessionState.archived.value = false
        sessionState.searchText.value = ''
        await sessionState.refresh()
        if (!stillHere()) return
        const threadRecord = { ...created.thread, session: created.session }
        if (!sessionState.sessions.value.some((item) => item.id === threadRecord.id))
          sessionState.sessions.value = [threadRecord, ...sessionState.sessions.value]
        selectedRid.value = capture.rid
        await openSession(created.thread.id, true)
        pendingReaderRequest.value = {
          ...capture,
          imageFile: input.imageFile || null,
          thread_id: created.thread.id,
          threadRecord,
          id: `reader-${ticket}-${Date.now()}`
        }
      } catch (error) {
        if (ticket === readerActionGeneration)
          searchNotice.value = error.message || '阅读动作交接失败'
      } finally {
        if (ticket === readerActionGeneration) askLoading.value = ''
      }
    }
    const readerStaged = (result) => {
      if (pendingReaderRequest.value?.id !== result.id) return
      pendingReaderRequest.value = null
      if (!result.success) searchNotice.value = result.error || '阅读动作未写入，请重试'
    }
    const openReaderEvidence = async (target) => {
      if (!target?.rid || target.rid !== selectedRid.value || !(await confirmLeave())) return
      evidenceTargets.value = {
        ...evidenceTargets.value,
        [target.rid]: { ...target, requestId: `evidence-${Date.now()}` }
      }
      readerTargets.value = {
        ...readerTargets.value,
        [target.rid]: { rid: target.rid, tab: 'pdf', requestId: `evidence-${Date.now()}` }
      }
    }
    let linkGeneration = 0
    const route = useRoute()
    const viewMode = ref('list')
    const activeThreadId = ref('')
    const settingsOpen = ref(false)
    const runs = ref([])
    const workbenchRun = ref(null)
    const runDetail = ref(null)
    const filters = ref({
      journal: '',
      quartile: '',
      min_if: '',
      year_min: '',
      year_max: '',
      source: '',
      sort: 'updated'
    })
    const applyFilters = () => {
      searchMode.value = 'list'
      runSearch()
    }
    const openSession = async (id, alreadyConfirmed = false) => {
      if (!alreadyConfirmed && !(await confirmLeave())) return
      const binding = sessionState.sessions.value.find((item) => item.id === id)?.session
      if (binding?.kind === 'paper' && binding.rid) selectedRid.value = binding.rid
      sessionState.selectedId.value = id
      activeThreadId.value = id
      viewMode.value = 'sessions'
    }
    const onSessionSelected = async ({ id, rid }) => {
      activeThreadId.value = id
      if (rid && rid !== selectedRid.value && (await confirmLeave())) selectedRid.value = rid
    }
    // 面板级 ×：收起会话分栏。有选中文献时切回全宽阅读工作区（会话记录保留，
    // 顶栏「会话 / AI 检索」或文献页「文献专属会话」随时唤回）；无文献则回目录。
    const closeSessionsPanel = async () => {
      if (!(await confirmLeave())) return
      viewMode.value = selectedRid.value ? 'reader' : 'list'
    }
    const closeReader = () => {
      setEditing(selectedRid.value, false)
      selectedRid.value = comparisonRids.value.shift() || ''
      if (viewMode.value === 'reader' && !selectedRid.value) viewMode.value = 'list'
    }
    const switchWorkspace = async (key) => {
      if (key === 'tasks') await loadTasks()
      else {
        if (key !== viewMode.value && !(await confirmLeave())) return
        viewMode.value = key
      }
    }
    const resumeRun = async (run) => {
      if (!(await confirmLeave())) return
      workbenchRun.value = run
      viewMode.value = 'workbench'
    }
    // 任务数据来自共享轮询源（状态栏/导入面板同源），任务视图常驻实时
    const taskFeed = useLiteratureTaskFeed()
    const literatureTasks = computed(() => taskFeed.tasks.value)
    const tasksNotice = ref('')
    // 入库段（type=ingest，来自知识域摄入投影）单列一节；其余为文献域任务
    const ingestTasks = computed(() => literatureTasks.value.filter((t) => t?.type === 'ingest'))
    const domainTasks = computed(() => literatureTasks.value.filter((t) => t?.type !== 'ingest'))

    const loadTasks = async () => {
      if (viewMode.value !== 'tasks' && !(await confirmLeave())) return
      viewMode.value = 'tasks'
      // 任务列表由共享轮询源（useLiteratureTaskFeed）常驻供给；这里只补运行历史。
      // runs 与任务进度互不相干，失败降级为行内提示，绝不连坐清空任务列表。
      tasksNotice.value = ''
      try {
        const history = await literatureWorkspaceApi.runs()
        runs.value = history?.items || []
      } catch (error) {
        runs.value = []
        tasksNotice.value = `运行历史读取失败：${error?.message || error}（任务进度不受影响）`
      }
      loadQualityMetrics()
    }
    const qualityMetrics = ref(null)
    const loadQualityMetrics = async () => {
      try {
        qualityMetrics.value = await literatureWorkspaceApi.qualityMetrics()
      } catch {
        qualityMetrics.value = null // 指标是辅助面板，失败不打断任务页
      }
    }
    const retryingIngest = ref('')
    const retryIngest = async (task) => {
      if (retryingIngest.value) return
      retryingIngest.value = task.ref
      try {
        await literatureWorkspaceApi.retryIngest(task.ref)
        tasksNotice.value = `已提交重试（单号 ${String(task.ref).slice(0, 16)}…），入库任务回到队列`
        taskFeed.refresh()
      } catch (error) {
        tasksNotice.value = error?.message || '重试提交失败'
      } finally {
        retryingIngest.value = ''
      }
    }
    const formatRate = (value) => (value == null ? '—' : `${Math.round(value * 100)}%`)
    // 运行错误人话化：只展示首行与有限长度（完整原文保留在 title 悬停）。
    // 原始错误串常带堆栈与内部字段名，直接渲染会泄露调试视图给终端用户。
    const describeRunError = (error) => {
      const text = String(error || '').trim()
      if (!text) return ''
      const firstLine = text.split(/\r?\n/)[0].trim()
      const cleaned = firstLine.replace(/^(Error|Exception|HTTPError)\s*[:：]\s*/i, '')
      return cleaned.length > 160
        ? `${cleaned.slice(0, 160)}…`
        : cleaned || '运行失败；悬停查看完整原因'
    }
    const onRefreshTasks = () => {
      taskFeed.refresh()
      return loadTasks()
    }
    // 导入面板「跟踪」→ 切到任务视图并高亮该处理单号（8 秒后自动取消）
    const highlightRef = ref('')
    const trackTicket = (ticket) => {
      highlightRef.value = String(ticket || '')
      loadTasks()
      window.setTimeout(() => {
        if (highlightRef.value === String(ticket || '')) highlightRef.value = ''
      }, 8000)
    }
    const importBib = async (event) => {
      const file = event.target.files?.[0]
      event.target.value = ''
      if (!file) return
      const form = new FormData()
      form.append('file', file)
      try {
        const result = await apiPost('/api/literature/papers/import/bib', form)
        // 后端幂等口径：papers_written（新插入）/ papers_skipped（已存在）；
        // citations_written 为实插引用边（RETURNING 判真，冲突跳过不计）
        const written = result.papers_written ?? result.inserted ?? 0
        const skipped = result.papers_skipped ?? result.skipped ?? 0
        const citations = result.citations_written ?? 0
        message.success(`Bib 导入完成：新增 ${written}，已存在 ${skipped}，引用边 ${citations}`)
        await load()
      } catch (error) {
        message.error(error?.message || 'Bib 导入失败')
      }
    }

    const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

    /** 搜索语法糖（API 层解析，对标 PaperAgent：kind:thesis 磁性 / 书 电化学） */
    const parseSearchSyntax = (raw) => {
      const text = String(raw || '').trim()
      let kind = currentKind.value
      let query = text
      const kindMatch = text.match(/kind:(\S+)/i)
      if (kindMatch) {
        kind = kindMatch[1]
        query = text.replace(kindMatch[0], '').trim()
      } else {
        for (const chip of KIND_CHIPS) {
          if (!chip.value) continue
          if (query.startsWith(`${chip.label} `) || query === chip.label) {
            kind = chip.value
            query = query.replace(chip.label, '').trim()
            break
          }
        }
      }
      return { kind, query }
    }

    let loadGeneration = 0
    const load = async () => {
      const ticket = ++loadGeneration
      loading.value = true
      try {
        const parsed = parseSearchSyntax(searchQuery.value)
        searchNotice.value = ''
        if (parsed.query && searchMode.value !== 'list') {
          const params = new URLSearchParams({
            query: parsed.query,
            limit: '100',
            kind: parsed.kind,
            use_cache: String(searchCache.value),
            rerank_model: rerankModel.value.trim()
          })
          const data = await apiGet('/api/literature/search/' + searchMode.value + '?' + params)
          if (ticket !== loadGeneration) return
          const hits = data.results
          papers.value = hits || []
          total.value = 0
          searchNotice.value =
            data.note || hits?.find((hit) => hit.navigation_note)?.navigation_note || ''
          return
        }
        const data = await listLiteraturePapers({
          query: parsed.query,
          kind: parsed.kind,
          page: page.value,
          page_size: pageSize,
          ...filters.value
        })
        if (ticket !== loadGeneration) return
        papers.value = data.items || []
        total.value = data.total || 0
      } catch (error) {
        if (ticket !== loadGeneration) return
        papers.value = []
        total.value = 0
        searchNotice.value = error?.message || '检索失败'
      } finally {
        if (ticket === loadGeneration) loading.value = false
      }
    }

    const runSearch = async () => {
      if (viewMode.value !== 'list' && !(await confirmLeave())) return
      viewMode.value = 'list'
      page.value = 1
      load()
    }
    const selectKind = (value) => {
      currentKind.value = value
      runSearch()
    }
    const gotoPage = (target) => {
      page.value = target
      load()
    }
    const openPaper = async (paper, stillCurrent = () => true) => {
      if (
        (selectedRid.value !== paper.rid || viewMode.value !== 'reader') &&
        !(await confirmLeave())
      )
        return false
      if (!stillCurrent()) return false
      comparisonRids.value = comparisonRids.value.filter((rid) => rid !== paper.rid)
      selectedRid.value = paper.rid
      viewMode.value = 'reader'
      return true
    }
    // 浮动阅读窗：替代 window.open 新页签（同页可拖拽/停靠/缩放的多窗并读）
    const floatingReaders = ref([])
    const popoutReader = async (rid) => {
      if (!rid || floatingReaders.value.some((win) => win.rid === rid)) return
      try {
        const paper = await getLiteraturePaper(rid)
        const offset = floatingReaders.value.length
        floatingReaders.value.push({
          rid,
          title: paper?.title || rid,
          paper: paper || {},
          initial: { x: 90 + offset * 28, y: 80 + offset * 28, w: 760, h: 640 }
        })
      } catch {
        // 数据不可得时保留旧行为兜底，不静默吞错
        window.open(`/literature?rid=${encodeURIComponent(rid)}`, '_blank', 'noopener,noreferrer')
      }
    }
    const closeFloatingReader = async (rid) => {
      const win = floatingReaders.value.find((item) => item.rid === rid)
      if (!win) return
      if (editingKeys.value.includes(`floating:${rid}`) && !(await confirmLeave())) return
      if (!floatingReaders.value.includes(win)) return
      floatingReaders.value = floatingReaders.value.filter((win) => win.rid !== rid)
      setEditing(`floating:${rid}`, false)
    }
    const dockFloatingReader = async (rid) => {
      const win = floatingReaders.value.find((item) => item.rid === rid)
      if (!win) return
      if (
        (editingKeys.value.includes(`floating:${rid}`) || viewMode.value !== 'reader') &&
        !(await confirmLeave())
      )
        return
      if (!floatingReaders.value.includes(win)) return
      if (selectedRid.value && selectedRid.value !== rid && !comparisonRids.value.includes(rid)) {
        if (comparisonRids.value.length >= 4) {
          searchNotice.value = '并排阅读最多 5 篇，请先关闭一个阅读器再停靠。'
          return
        }
        comparisonRids.value.push(rid)
      } else if (!selectedRid.value) selectedRid.value = rid
      viewMode.value = 'reader'
      floatingReaders.value = floatingReaders.value.filter((item) => item !== win)
      setEditing(`floating:${rid}`, false)
    }
    const openReaderLink = async (target, sourceRid) => {
      const ticket = ++linkGeneration
      if (typeof target !== 'string' || !target.trim() || target.length > 500) {
        readerLinkNotice.value = '链接不是受支持的文献双链'
        return
      }
      try {
        const data = await apiGet(
          `/api/literature/resolve-link?target=${encodeURIComponent(target)}&source_rid=${encodeURIComponent(sourceRid)}`
        )
        if (ticket !== linkGeneration) return
        if (selectedRid.value !== sourceRid && !comparisonRids.value.includes(sourceRid)) return
        if (data.rid) {
          if (!(await openPaper({ rid: data.rid }, () => ticket === linkGeneration))) return
          readerTargets.value = {
            ...readerTargets.value,
            [data.rid]: { ...data, requestId: ticket }
          }
          readerLinkNotice.value = ''
        } else
          readerLinkNotice.value = '未找到唯一可访问文献，请在目录中按标题或 DOI 检索；未自动跳转。'
      } catch (error) {
        if (ticket === linkGeneration) readerLinkNotice.value = error.message || '文献链接解析失败'
      }
    }
    const kindLabel = (kind) => KIND_LABELS[kind] || kind

    // 外层聊天侧栏的收起由路由 meta compactShell 驱动（见 AppLayout 的
    // sidebarEffectivelyCollapsed）。这里不再“进入时改 store、离开时恢复”：
    // 本路由 keepAlive，离开只触发 onDeactivated 而非 unmount，那套恢复是死代码；
    // 且写持久化偏好会在刷新后把用户的展开偏好永久改成折叠。
    onMounted(() => {
      window.addEventListener('beforeunload', beforeUnload)
      if (typeof route.query.rid === 'string' && route.query.rid.length <= 255)
        openPaper({ rid: route.query.rid })
      load()
      sessionState.refresh()
    })
    onBeforeUnmount(() => {
      loadGeneration++
      readerActionGeneration++
      window.removeEventListener('beforeunload', beforeUnload)
      sessionState.dispose()
    })

    return {
      evidenceTargets,
      pendingReaderRequest,
      handleReaderAction,
      openReaderEvidence,
      readerStaged,
      userStore,
      switchWorkspace,
      confirmLeave,
      activeThreadId,
      settingsOpen,
      openSession,
      onSessionSelected,
      closeSessionsPanel,
      closeReader,
      runs,
      workbenchRun,
      runDetail,
      resumeRun,
      filters,
      applyFilters,
      papers,
      total,
      page,
      pageSize,
      totalPages,
      loading,
      searchQuery,
      searchMode,
      searchCache,
      rerankModel,
      searchNotice,
      currentKind,
      selectedRid,
      previewRid,
      askLoading,
      askPaper,
      comparisonRids,
      comparisonChoice,
      addComparison,
      removeComparison,
      setEditing,
      readerLinkNotice,
      readerTargets,
      openReaderLink,
      popoutReader,
      floatingReaders,
      closeFloatingReader,
      dockFloatingReader,
      kindChips: KIND_CHIPS,
      runSearch,
      selectKind,
      gotoPage,
      openPaper,
      kindLabel,
      viewMode,
      literatureTasks,
      ingestTasks,
      domainTasks,
      tasksNotice,
      highlightRef,
      trackTicket,
      onRefreshTasks,
      qualityMetrics,
      retryingIngest,
      retryIngest,
      formatRate,
      describeRunError,
      loadTasks,
      load,
      importBib,
      // workspace chrome (PaperAgent reading-desk parity)
      drawer,
      readMode,
      eyeCare,
      toggleEyeCare,
      sidebarPinned,
      sidebarVisible,
      litSidebarWidth,
      LIT_SIDEBAR_WIDTH_MIN,
      LIT_SIDEBAR_WIDTH_MAX,
      workspaceStyle,
      sessionState,
      selectDrawer,
      togglePin,
      toggleSidebar,
      splitSwapped,
      toggleSplitSwap,
      toggleReadMode,
      startSidebarDrag,
      isSidebarResizing,
      onSidebarResizeKeydown,
      resetSidebarWidth,
      newSession,
      selectSessionFromSidebar
    }
  }
}
</script>

<style lang="less" scoped>
@import '@/assets/css/base.css';

/* Literature workbench chrome: fixed topbar + CSS Grid deck (sidebar | resizer | main),
   mirroring PaperAgent's reading desk. Column widths transition via inline style so
   collapse/read-mode stay cheap instead of re-laying out the whole page. */
.literature-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  padding: 0;
}
.lit-topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 16px;
  border-bottom: 1px solid var(--gray-150);
  background: var(--color-bg-container);
  flex-shrink: 0;
}
.lit-brand {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
}
.topbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 1;
  min-width: 0;
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
}
.lit-workspace {
  display: grid;
  grid-template-columns: var(--lit-side-w, 320px) 4px minmax(0, 1fr);
  flex: 1;
  min-height: 0;
  transition: grid-template-columns 0.18s ease;
}
/* Reading mode collapses the deck to the content only (PaperAgent .read-mode). */
.literature-view.read-mode .lit-workspace {
  grid-template-columns: minmax(0, 1fr);
}

// 护眼模式（PaperAgent §3.2 👁）：作用域限定在文献工作台内覆盖平台令牌，
// 不触碰品牌主题层（brands/**）的变量定义。暖色取值对正文对比度 ≥ 4.5:1。
.literature-view.eye-care {
  --color-bg-container: #f7f1e3;
  --color-bg-elevated: #fdf9ee;
  --gray-100: #efe7d4;
  --color-field-bg: #fdf9ee;
  --color-field-border: #ddcfb2;
  background: #f4ecd9;
}
.sidebar-resizer {
  cursor: col-resize;
  background: transparent;
  &:hover {
    background: var(--main-color);
    opacity: 0.35;
  }
}
.lit-main {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  padding: 12px 16px;
}
.workspace-notice {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 6px 16px;
  font-size: 12px;
  color: var(--color-text-secondary);
  border-top: 1px solid var(--gray-150);
  flex-shrink: 0;
}
.notice-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notice-close {
  flex-shrink: 0;
  border: 0;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 4px;
  line-height: 1;
  &:hover {
    background: var(--color-bg-elevated);
    color: var(--color-text-primary);
  }
}
.directory-filters,
.settings-links {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin: 12px 0;
}
.directory-filters input,
.directory-filters select {
  padding: 5px;
  background: var(--color-bg-container);
  color: var(--color-text-primary);
  border: 1px solid var(--gray-150);
  border-radius: 6px;
}
.directory-filters input[type='number'] {
  width: 80px;
}
.directory-filters input[type='text'] {
  max-width: 180px;
}
.reading-workspace {
  min-width: 0;
}
.reading-workspace.split {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(360px, 1fr);
  gap: 16px;
}
/* 一键互换：会话面板排到文献页之后（右侧），文献页（含并排对比）保持原相对顺序 */
.reading-workspace.split.swapped :deep(.sessions-workspace) {
  order: 2;
}
@media (max-width: 1100px) {
  .reading-workspace.split {
    grid-auto-flow: row;
    grid-auto-columns: auto;
    grid-template-columns: 1fr;
  }
}
@media (max-width: 760px) {
  // Keep both navigation and actions reachable in the embedded browser.
  .lit-workspace.has-sidebar {
    grid-template-columns: minmax(0, 1fr) !important;
    grid-template-rows: 180px minmax(0, 1fr);
  }
  .sidebar-resizer {
    display: none;
  }
  .lit-workspace.has-sidebar > :deep(.lit-sidebar) {
    border-right: 0;
    border-bottom: 1px solid var(--gray-150);
  }
}
.lit-header {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.kind-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.chip {
  border: 1px solid var(--gray-300);
  border-radius: 12px;
  background: transparent;
  color: var(--color-text-secondary);
  padding: 2px 12px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s ease;
  &:hover {
    border-color: var(--main-color);
  }
  &.active {
    background: var(--main-color);
    border-color: var(--main-color);
    color: var(--gray-0);
    font-weight: 500;
  }
}
.lit-toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.search-input {
  background: var(--gray-100);
  color: var(--color-text);
  border: 1px solid var(--gray-300);
  border-radius: 6px;
  flex: 1;
  max-width: 480px;
  padding: 6px 12px;
}
.paper-list {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.paper-card {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 10px 14px;
  cursor: pointer;
  &:hover {
    border-color: var(--main-color);
  }
}
.paper-title {
  font-weight: 600;
  cursor: pointer;
  /* 键盘可达的整卡打开入口（role=button）：焦点圈清晰可见 */
  &:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: 2px;
    border-radius: 4px;
  }
}
.kind-badge {
  font-size: 12px;
  border-radius: 4px;
  padding: 1px 6px;
  margin-right: 8px;
  background: color-mix(in srgb, var(--main-color) 12%, transparent);
}
.paper-preview {
  margin-top: 6px;
  padding: 8px 10px;
  border-left: 3px solid var(--main-color);
  border-radius: 4px;
  background: var(--color-bg-elevated);
  font-size: 12px;
  color: var(--color-text-secondary);
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 140px;
  overflow: auto;
  text-align: left;
}
.paper-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.paper-meta {
  margin-top: 4px;
  font-size: 12px;
  color: var(--color-text-secondary);
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.lit-pagination {
  margin-top: 12px;
  display: flex;
  gap: 12px;
  align-items: center;
}
.toolbar-spacer {
  flex: 1;
}
.moc-input {
  background: var(--gray-100);
  color: var(--color-text);
  border: 1px solid var(--gray-300);
  border-radius: 6px;
  padding: 6px 12px;
  width: 200px;
}
.concept-list {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.concept-card {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 10px 14px;
  &.mature {
    border-color: color-mix(in srgb, #52c41a 50%, transparent);
  }
}
.concept-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.concept-count {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.concept-def {
  margin: 6px 0;
  font-size: 13px;
}
.concept-papers {
  font-size: 11px;
  font-family: monospace;
  color: var(--color-text-secondary);
  word-break: break-all;
}
.lit-empty,
.lit-loading {
  padding: 32px 0;
  color: var(--color-text-tertiary);
}
.task-error {
  margin-top: 6px;
  color: var(--color-error-700);
  font-size: 12px;
}
.task-notice {
  margin: 6px 0;
  color: var(--color-text-secondary, #888);
  font-size: 12px;
}
.run-error {
  margin: 4px 0 0;
  color: var(--color-error-700);
  font-size: 12px;
  cursor: help;
}
.run-detail {
  margin: 8px 0;
  font-size: 12px;
  summary {
    cursor: pointer;
    color: var(--color-text-secondary);
    user-select: none;
  }
  pre {
    margin: 6px 0 0;
    padding: 8px;
    background: var(--color-bg-container);
    border: 1px solid var(--gray-150);
    border-radius: 6px;
    overflow: auto;
    max-height: 40vh;
  }
}
.task-hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}
/* 失败类条目整卡描红；跟踪定位条目短暂高亮 */
.task-failure {
  border-color: var(--color-error-700, #d03029);
}
.task-highlight {
  outline: 2px solid var(--main-color, #2f6b4f);
  outline-offset: 2px;
}
.settings-links a {
  color: var(--main-color);
  text-decoration: none;
  &:hover {
    text-decoration: underline;
  }
}
</style>
