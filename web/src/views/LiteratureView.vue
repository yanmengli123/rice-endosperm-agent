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
          :class="{ active: viewMode === 'concepts' }"
          @click="switchWorkspace('concepts')"
        >
          <Network :size="13" aria-hidden="true" /> 概念页
        </button>
        <button
          class="btn"
          :class="{ active: viewMode === 'reports' }"
          @click="switchWorkspace('reports')"
        >
          <FileText :size="13" aria-hidden="true" /> 综述报告
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
      </nav>
    </header>

    <div class="lit-workspace" :style="workspaceStyle">
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
            <option value="compiled">编译笔记（RRF 导航）</option>
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
          <button class="btn" :disabled="importing" @click="$refs.paperAgentInput.click()">
            {{ importing ? '导入中…' : '迁移 PaperAgent' }}
          </button>
          <input ref="bibInput" hidden type="file" accept=".bib,.txt" @change="importBib" />
          <input
            ref="paperAgentInput"
            hidden
            type="file"
            accept=".zip"
            @change="importPaperAgent"
          />
          <input
            v-model="mocTopic"
            class="moc-input"
            placeholder="主题 MOC（如：淀粉合成）"
            @keyup.enter="generateMoc"
          />
          <button class="btn" :disabled="mocLoading" @click="generateMoc">
            {{ mocLoading ? '生成中…' : '生成主题导读' }}
          </button>
        </div>

        <LibraryWorkbench
          v-if="viewMode === 'workbench'"
          :resume-run="workbenchRun"
          @session="openSession"
          @editing-change="setEditing('global-cards', $event)"
        />
        <ImportPanel v-if="viewMode === 'import'" @imported="load" @track="trackTicket" />
        <DiaryView v-if="viewMode === 'diary'" @editing-change="setEditing('diary', $event)" />
        <LiteratureGraphView v-if="viewMode === 'graph'" @open-paper="openPaper" />
        <section v-if="viewMode === 'settings'" class="workspace-settings">
          <h3>设置与治理</h3>
          <p>
            模型与密钥使用 Yuxi 统一配置，不复制 PaperAgent
            明文配置文件。插件使用受治理的工具、Skills 与 MCP。
          </p>
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
        <div
          v-if="viewMode === 'reader' || viewMode === 'sessions'"
          class="reading-workspace"
          :class="{
            split:
              (viewMode === 'sessions' && selectedRid) ||
              (viewMode === 'reader' && comparisonRids.length)
          }"
        >
          <LiteratureSessions
            :session-state="sessionState"
            v-if="viewMode === 'sessions'"
            :rid="selectedRid"
            :thread-id="activeThreadId"
            :can-change-thread="confirmLeave"
            @thread-selected="onSessionSelected"
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
        <div v-if="viewMode === 'reports'" class="concepts-panel">
          <div v-if="!reports.length" class="lit-empty">暂无综述或主题导读</div>
          <ul v-else class="concept-list">
            <li v-for="report in reports" :key="report.id" class="concept-card">
              <div class="concept-head">
                <strong>{{ report.title }}</strong>
                <span class="concept-count">{{ report.report_kind }}</span>
                <button class="btn" @click="openReport(report.id)">阅读全文</button>
              </div>
              <div class="concept-def">{{ report.created_at || '' }}</div>
            </li>
          </ul>
          <article v-if="selectedReport">
            <h3>{{ selectedReport.title }}</h3>
            <p>{{ selectedReport.notice }}</p>
            <MarkdownPreview :content="selectedReport.navigation_markdown" />
          </article>
        </div>

        <div v-if="viewMode === 'tasks'" class="concepts-panel">
          <button class="btn" @click="onRefreshTasks">刷新任务与运行历史</button>
          <p v-if="tasksNotice" class="task-notice" role="status">{{ tasksNotice }}</p>
          <h3>我的持久运行历史</h3>
          <ul class="concept-list">
            <li v-for="run in runs" :key="run.id" class="concept-card">
              {{ run.type }} · {{ run.id }} · {{ run.status }} · {{ run.updated_at }}
              <button v-if="run.rid" class="btn" @click="openPaper({ rid: run.rid })">
                打开文献
              </button>
              <button
                v-if="run.type === 'library' || run.type === 'navigation'"
                class="btn"
                @click="resumeRun(run)"
              >
                查看运行详情
              </button>
              <p v-if="run.error">{{ run.error }}</p>
            </li>
          </ul>
          <pre v-if="runDetail">{{ JSON.stringify(runDetail, null, 2) }}</pre>
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
              <!-- 失败分类给下一步动作：REJECTED 不可重试（说明原因）；FAILED 可重导（sha 复用同一单号） -->
              <p v-if="task.status === 'REJECTED'" class="task-hint">
                质量门禁拒绝：不可重试，请检查文件内容（空白 / 乱码 / 非论文 PDF）。
              </p>
              <p v-else-if="task.status === 'FAILED'" class="task-hint">
                供应商解析失败：可重新导入同一文件（内容指纹复用，不产生重复任务）。
              </p>
              <button
                v-if="task.paper_rid"
                class="btn"
                @click="openPaper({ rid: task.paper_rid })"
              >
                打开文献
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
              <div v-if="task.attempt > 0" class="concept-meta">已尝试 {{ task.attempt + 1 }} 次</div>
              <div v-if="task.error" class="task-error">{{ task.error }}</div>
            </li>
          </ul>
        </div>

        <div v-if="viewMode === 'concepts'" class="concepts-panel">
          <div v-if="!concepts.length" class="lit-empty">
            暂无概念页（≥2 篇文献共享同一概念时聚合；先编译 L1）
          </div>
          <ul v-else class="concept-list">
            <li
              v-for="concept in concepts"
              :key="concept.concept"
              class="concept-card"
              :class="{ mature: concept.mature }"
            >
              <div class="concept-head">
                <strong>{{ concept.concept }}</strong>
                <span class="concept-count"
                  >{{ concept.paper_count }} 篇{{ concept.mature ? ' · 成熟' : '' }}</span
                >
              </div>
              <div class="concept-def">{{ concept.definition }}</div>
              <div class="concept-papers">
                <button
                  v-for="rid in concept.papers"
                  :key="rid"
                  class="btn"
                  @click="openPaper({ rid })"
                >
                  {{ rid }}
                </button>
              </div>
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
          <select v-model="filters.compiled_level" aria-label="编译状态">
            <option value="">所有编译状态</option>
            <option value="missing">尚未编译</option>
            <option value="any">已编译</option>
            <option v-for="level in ['L1', 'L2', 'L3']" :key="level" :value="level">
              {{ level }} 完成
            </option>
          </select>
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
            <option value="value">价值分</option>
            <option value="title">标题</option>
          </select>
          <button class="btn" @click="applyFilters">应用目录筛选</button>
          <label
            ><input
              type="checkbox"
              :checked="allSelected"
              @change="selectPage($event.target.checked)"
            />选择本页</label
          >
          <span>已选 {{ selectedPapers.length }} 篇（最多 50）</span>
          <button
            v-for="level in ['L1', 'L2', 'L3']"
            :key="level"
            class="btn"
            :disabled="batchBusy || !selectedPapers.length"
            @click="compileBatch(level)"
          >
            批量 {{ level }}
          </button>
          <p v-if="batchNotice" role="status">{{ batchNotice }}</p>
        </div>

        <div v-if="viewMode === 'list' && loading" class="lit-loading">加载中…</div>
        <div v-else-if="viewMode === 'list' && !papers.length" class="lit-empty">
          暂无可访问文献。可在知识库中上传 PDF，完成证据处理后自动进入文献库；也可导入 Bib 或迁移
          PaperAgent。
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
            <input
              type="checkbox"
              :aria-label="`选择 ${paper.title || paper.rid}`"
              :checked="selectedPapers.includes(paper.rid)"
              @click.stop
              @change="togglePaper(paper.rid, $event.target.checked)"
            />
            <div class="paper-title">
              <span class="kind-badge" :data-kind="paper.kind">{{ kindLabel(paper.kind) }}</span>
              {{ paper.title || paper.rid }}
              <!-- 快速预览（对标 PaperAgent §5.3 悬停行预览）：只展示列表已返回的字段，
               不在悬停时取原文，避免触发大体积产物读取与额外鉴权请求。 -->
              <div v-if="previewRid === paper.rid" class="paper-preview">
                <span v-if="paper.journal">期刊：{{ paper.journal }}</span>
                <span v-if="paper.pub_year">年份：{{ paper.pub_year }}</span>
                <span v-if="paper.value_score"
                  >价值分：{{ Number(paper.value_score).toFixed(1) }}</span
                >
                <span v-if="paper.note_excerpt">{{ paper.note_excerpt }}</span>
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
              <span v-if="paper.value_score">价值分 {{ paper.value_score.toFixed(1) }}</span>
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
    <p v-if="sessionState.notice.value" class="workspace-notice" role="status">
      {{ sessionState.notice.value }}
    </p>
    <LiteratureStatusBar />
  </div>
</template>

<script>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { BookOpen, List, Network, FileText, Settings, Expand, PanelLeft, Eye } from '@lucide/vue'
import { useRoute, onBeforeRouteLeave } from 'vue-router'
import { apiGet, apiPost } from '@/apis/base'
import { listLiteraturePapers, literatureWorkspaceApi } from '@/apis/literature_api'
import PaperDetail from '@/components/literature/PaperDetail.vue'
import LibraryWorkbench from '@/components/literature/LibraryWorkbench.vue'
import LiteratureStatusBar from '@/components/literature/LiteratureStatusBar.vue'
import ImportPanel from '@/components/literature/ImportPanel.vue'
import LiteratureSessions from '@/components/literature/LiteratureSessions.vue'
import { useColumnResize } from '@/composables/useColumnResize'
import { useLiteratureSessions } from '@/composables/useLiteratureSessions'
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
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
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
    FileText,
    Settings,
    Expand,
    PanelLeft,
    PaperDetail,
    LibraryWorkbench,
    MarkdownPreview,
    ImportPanel,
    LiteratureSessions,
    DiaryView,
    LiteratureGraphView,
    SettingsModal,
    LiteratureSidebar,
    Eye,
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
    const newSession = (kind) => {
      if (!confirmLeave()) return
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
    const selectSessionFromSidebar = (id) => {
      openSession(id)
    }
    const selectedRid = ref('')
    const comparisonRids = ref([])
    const comparisonChoice = ref('')
    const editingKeys = ref([])
    const setEditing = (key, value) => {
      editingKeys.value = editingKeys.value.filter((item) => item !== key)
      if (value) editingKeys.value.push(key)
    }
    const confirmLeave = () =>
      !editingKeys.value.length || window.confirm('离开当前工作区会放弃未保存编辑，继续？')
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
      if (askLoading.value || !confirmLeave()) return
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
    let linkGeneration = 0
    const route = useRoute()
    const viewMode = ref('list')
    const workspaceTabs = [
      { key: 'import', label: '导入' },
      { key: 'sessions', label: '会话 / AI 检索' },
      { key: 'list', label: '文献目录' },
      { key: 'reader', label: '阅读工作区' },
      { key: 'workbench', label: '知识库管理' },
      { key: 'graph', label: '2D / 3D 图谱' },
      { key: 'diary', label: '阅读日记' },
      { key: 'tasks', label: '任务与历史' },
      { key: 'settings', label: '设置' }
    ]
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
      compiled_level: '',
      source: '',
      sort: 'updated'
    })
    const selectedPapers = ref([])
    const batchBusy = ref(false)
    const batchNotice = ref('')
    const allSelected = computed(
      () =>
        papers.value.length > 0 &&
        papers.value.every((paper) => selectedPapers.value.includes(paper.rid))
    )
    const togglePaper = (rid, checked) => {
      if (checked && !selectedPapers.value.includes(rid) && selectedPapers.value.length < 50)
        selectedPapers.value.push(rid)
      if (!checked) selectedPapers.value = selectedPapers.value.filter((value) => value !== rid)
    }
    const selectPage = (checked) => papers.value.forEach((paper) => togglePaper(paper.rid, checked))
    const compileBatch = async (level) => {
      if (
        !window.confirm(
          `提交 ${selectedPapers.value.length} 篇 ${level} 编译？执行会调用模型并按实际用量计费；冻结内容不覆盖。`
        )
      )
        return
      batchBusy.value = true
      try {
        const result = await literatureWorkspaceApi.compileBatch({
          rids: selectedPapers.value,
          level
        })
        const rejected = result.items.filter((item) => !item.accepted)
        batchNotice.value =
          `已接收 ${result.accepted} 个任务（不表示完成）。` +
          rejected.map((item) => `${item.rid}: ${item.error}`).join('；')
      } catch (error) {
        batchNotice.value = error.message || '批量入队失败'
      } finally {
        batchBusy.value = false
      }
    }
    const applyFilters = () => {
      searchMode.value = 'list'
      runSearch()
    }
    const openSession = (id, alreadyConfirmed = false) => {
      if (!alreadyConfirmed && !confirmLeave()) return
      const binding = sessionState.sessions.value.find((item) => item.id === id)?.session
      if (binding?.kind === 'paper' && binding.rid) selectedRid.value = binding.rid
      sessionState.selectedId.value = id
      activeThreadId.value = id
      viewMode.value = 'sessions'
    }
    const onSessionSelected = ({ id, rid }) => {
      activeThreadId.value = id
      if (rid && rid !== selectedRid.value && confirmLeave()) selectedRid.value = rid
    }
    const closeReader = () => {
      setEditing(selectedRid.value, false)
      selectedRid.value = comparisonRids.value.shift() || ''
      if (viewMode.value === 'reader' && !selectedRid.value) viewMode.value = 'list'
    }
    const switchWorkspace = (key) => {
      if (key === 'tasks') loadTasks()
      else if (key === 'concepts') loadConcepts()
      else if (key === 'reports') loadReports()
      else {
        if (key !== viewMode.value && !confirmLeave()) return
        viewMode.value = key
      }
    }
    const resumeRun = async (run) => {
      if (!confirmLeave()) return
      workbenchRun.value = run
      viewMode.value = 'workbench'
    }
    const concepts = ref([])
    const mocTopic = ref('')
    const mocLoading = ref(false)
    const reports = ref([])
    const selectedReport = ref(null)
    const openReport = async (id) => {
      selectedReport.value = null
      try {
        selectedReport.value = await apiGet('/api/literature/reports/' + id)
      } catch (error) {
        alert(error?.message || '报告读取失败')
      }
    }
    // 任务数据来自共享轮询源（状态栏/导入面板同源），任务视图常驻实时
    const taskFeed = useLiteratureTaskFeed()
    const literatureTasks = computed(() => taskFeed.tasks.value)
    const tasksNotice = ref('')
    // 入库段（type=ingest，来自知识域摄入投影）单列一节；其余为文献域任务
    const ingestTasks = computed(() => literatureTasks.value.filter((t) => t?.type === 'ingest'))
    const domainTasks = computed(() => literatureTasks.value.filter((t) => t?.type !== 'ingest'))
    const importing = ref(false)

    const loadConcepts = async () => {
      if (viewMode.value !== 'concepts' && !confirmLeave()) return
      viewMode.value = 'concepts'
      try {
        const data = await apiGet('/api/literature/concepts')
        concepts.value = data?.concepts || []
      } catch (error) {
        searchNotice.value = error?.message || '概念页读取失败'
      }
    }
    const generateMoc = async () => {
      if (!mocTopic.value.trim()) return
      mocLoading.value = true
      try {
        const result = await apiPost('/api/literature/reports/topic-moc', {
          topic: mocTopic.value.trim()
        })
        mocTopic.value = ''
        alert(`主题导读已生成（${result.sources} 篇来源），见「综述报告」列表`)
      } catch (err) {
        alert(`生成失败：${err?.message || err}`)
      } finally {
        mocLoading.value = false
      }
    }
    const loadReports = async () => {
      if (viewMode.value !== 'reports' && !confirmLeave()) return
      viewMode.value = 'reports'
      try {
        const data = await apiGet('/api/literature/reports')
        reports.value = data?.reports || []
      } catch (error) {
        searchNotice.value = error?.message || '报告列表读取失败'
      }
    }
    const loadTasks = async () => {
      if (viewMode.value !== 'tasks' && !confirmLeave()) return
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
        alert(`Bib 导入完成：新增 ${written}，已存在 ${skipped}，引用边 ${citations}`)
        await load()
      } catch (error) {
        alert(error?.message || 'Bib 导入失败')
      }
    }
    const importPaperAgent = async (event) => {
      const file = event.target.files?.[0]
      event.target.value = ''
      if (!file) return
      importing.value = true
      try {
        const dryForm = new FormData()
        dryForm.append('file', file)
        const report = await apiPost(
          '/api/literature/papers/import/paperagent?dry_run=true',
          dryForm
        )
        const summary = `将迁移 ${report.papers?.source || 0} 篇文献、${report.variants?.source || 0} 个变体；冲突 ${report.conflicts?.length || 0} 项。历史资产：${JSON.stringify(report.history?.source_counts || {})}`
        if (
          !window.confirm(
            `${summary}\n${report.warning || ''}\n尚不迁移：${(report.unsupported || []).join('、')}\n确认执行正式导入？`
          )
        )
          return
        const importForm = new FormData()
        importForm.append('file', file)
        const completed = await apiPost(
          '/api/literature/papers/import/paperagent?dry_run=false&archive_sha256=' +
            encodeURIComponent(report.archive_sha256),
          importForm
        )
        alert(
          `迁移完成：历史映射 ${completed.history?.applied || 0}，只读保留 ${completed.history?.preserved || 0}，冲突 ${completed.history?.conflicts?.length || 0}。在导入页查看逐项回执；请保留原始归档。`
        )
        await load()
      } catch (error) {
        alert(error?.message || 'PaperAgent 数据迁移失败')
      } finally {
        importing.value = false
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
            limit: searchMode.value === 'compiled' ? '50' : '100',
            kind: parsed.kind,
            use_cache: String(searchCache.value),
            rerank_model: rerankModel.value.trim()
          })
          const data = await apiGet('/api/literature/search/' + searchMode.value + '?' + params)
          if (ticket !== loadGeneration) return
          const hits = searchMode.value === 'compiled' ? data.hits : data.results
          papers.value = hits || []
          total.value = 0
          searchNotice.value =
            data.note || hits?.find((hit) => hit.navigation_note)?.navigation_note || ''
          if (searchMode.value === 'compiled')
            searchNotice.value = '笔记是派生导航，事实需核验原知识源。' + searchNotice.value
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

    const runSearch = () => {
      if (viewMode.value !== 'list' && !confirmLeave()) return
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
    const openPaper = (paper) => {
      if ((selectedRid.value !== paper.rid || viewMode.value !== 'reader') && !confirmLeave())
        return false
      comparisonRids.value = comparisonRids.value.filter((rid) => rid !== paper.rid)
      selectedRid.value = paper.rid
      viewMode.value = 'reader'
      return true
    }
    const popoutReader = (rid) => {
      window.open(`/literature?rid=${encodeURIComponent(rid)}`, '_blank', 'noopener,noreferrer')
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
          if (!openPaper({ rid: data.rid })) return
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
      window.removeEventListener('beforeunload', beforeUnload)
      sessionState.dispose()
    })

    return {
      userStore,
      workspaceTabs,
      switchWorkspace,
      confirmLeave,
      activeThreadId,
      settingsOpen,
      openSession,
      onSessionSelected,
      closeReader,
      runs,
      workbenchRun,
      runDetail,
      resumeRun,
      filters,
      applyFilters,
      selectedPapers,
      allSelected,
      togglePaper,
      selectPage,
      compileBatch,
      batchBusy,
      batchNotice,
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
      kindChips: KIND_CHIPS,
      runSearch,
      selectKind,
      gotoPage,
      openPaper,
      kindLabel,
      viewMode,
      concepts,
      mocTopic,
      mocLoading,
      reports,
      selectedReport,
      openReport,
      literatureTasks,
      ingestTasks,
      domainTasks,
      tasksNotice,
      highlightRef,
      trackTicket,
      onRefreshTasks,
      importing,
      loadConcepts,
      generateMoc,
      loadReports,
      loadTasks,
      importBib,
      importPaperAgent,
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
  margin: 0;
  padding: 6px 16px;
  font-size: 12px;
  color: var(--color-text-secondary);
  border-top: 1px solid var(--gray-150);
  flex-shrink: 0;
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
@media (max-width: 1100px) {
  .reading-workspace.split {
    grid-auto-flow: row;
    grid-auto-columns: auto;
    grid-template-columns: 1fr;
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
