<template>
  <div class="literature-view">
    <div class="lit-header">
      <h2>文献库</h2>
      <div class="kind-chips">
        <button
          v-for="chip in kindChips"
          :key="chip.value"
          class="chip"
          :class="{ active: currentKind === chip.value }"
          @click="selectKind(chip.value)"
        >
          {{ chip.label }}
        </button>
      </div>
    </div>

    <nav class="workspace-nav" aria-label="文献工作区导航">
      <button
        v-for="item in workspaceTabs"
        :key="item.key"
        class="btn"
        :class="{ active: viewMode === item.key }"
        @click="switchWorkspace(item.key)"
      >
        {{ item.label }}
      </button>
    </nav>

    <div class="lit-toolbar">
      <input
        v-model="searchQuery"
        class="search-input"
        placeholder="检索标题 / DOI（支持 kind:thesis 语法）"
        @keyup.enter="runSearch"
      />
      <button class="btn" @click="runSearch">搜索</button>
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
        style="max-width: 260px"
      />
      <span class="toolbar-spacer"></span>
      <button class="btn" :class="{ ghost: viewMode !== 'list' }" @click="viewMode = 'list'">
        文献列表
      </button>
      <button class="btn" :class="{ ghost: viewMode !== 'concepts' }" @click="loadConcepts">
        概念页
      </button>
      <button class="btn" :class="{ ghost: viewMode !== 'reports' }" @click="loadReports">
        综述报告
      </button>
      <button class="btn" :class="{ ghost: viewMode !== 'tasks' }" @click="loadTasks">任务</button>
      <button class="btn" @click="viewMode = 'workbench'">建库与导出</button>
      <button class="btn" @click="$refs.bibInput.click()">导入 Bib</button>
      <button class="btn" :disabled="importing" @click="$refs.paperAgentInput.click()">
        {{ importing ? '导入中…' : '迁移 PaperAgent' }}
      </button>
      <input ref="bibInput" hidden type="file" accept=".bib,.txt" @change="importBib" />
      <input ref="paperAgentInput" hidden type="file" accept=".zip" @change="importPaperAgent" />
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
    />
    <ImportPanel v-if="viewMode === 'import'" @imported="load" />
    <DiaryView v-if="viewMode === 'diary'" />
    <LiteratureGraphView v-if="viewMode === 'graph'" />
    <section v-if="viewMode === 'settings'" class="workspace-settings">
      <h3>设置与治理</h3>
      <p>
        模型与密钥使用 Yuxi 统一配置，不复制 PaperAgent 明文配置文件。插件使用受治理的工具、Skills
        与 MCP。
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
        split: (viewMode === 'sessions' && selectedRid) || (viewMode === 'reader' && secondaryRid)
      }"
    >
      <LiteratureSessions
        v-if="viewMode === 'sessions'"
        :rid="selectedRid"
        :thread-id="activeThreadId"
        @thread-selected="onSessionSelected"
      />
      <PaperDetail
        v-if="selectedRid"
        :key="selectedRid"
        :rid="selectedRid"
        embedded
        @close="closeReader"
        @session="openSession"
        @open-link="openReaderLink"
      />
      <PaperDetail
        v-if="viewMode === 'reader' && secondaryRid"
        :key="`secondary-${secondaryRid}`"
        :rid="secondaryRid"
        embedded
        @close="secondaryRid = ''"
        @open-link="openReaderLink"
      />
      <p v-else-if="viewMode === 'reader'">从文献列表选择一篇文献开始阅读。</p>
    </div>
    <div v-if="viewMode === 'reader' && selectedRid" class="reader-actions">
      <label
        >并排阅读<select v-model="secondaryRid">
          <option value="">关闭第二阅读器</option>
          <option
            v-for="paper in papers.filter((item) => item.rid !== selectedRid)"
            :key="paper.rid"
            :value="paper.rid"
          >
            {{ paper.title || paper.rid }}
          </option>
        </select></label
      >
      <button class="btn" @click="popoutReader">独立窗口阅读</button>
      <p v-if="readerLinkNotice" role="status">{{ readerLinkNotice }}</p>
    </div>
    <p v-if="viewMode === 'list' && searchNotice" role="status">{{ searchNotice }}</p>
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
      <button class="btn" @click="loadTasks">刷新任务与运行历史</button>
      <h3>我的持久运行历史</h3>
      <ul class="concept-list">
        <li v-for="run in runs" :key="run.id" class="concept-card">
          {{ run.type }} · {{ run.id }} · {{ run.status }} · {{ run.updated_at }}
          <button v-if="run.rid" class="btn" @click="openPaper({ rid: run.rid })">打开文献</button>
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
      <h3>处理任务</h3>
      <div v-if="!literatureTasks.length" class="lit-empty">暂无文献任务</div>
      <ul v-else class="concept-list">
        <li v-for="task in literatureTasks" :key="`${task.type}-${task.ref}`" class="concept-card">
          <div class="concept-head">
            <strong>{{ task.type }} · {{ task.ref }}</strong>
            <span class="concept-count">{{ task.status }}</span>
          </div>
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
      <label>年份从<input v-model="filters.year_min" type="number" min="1000" max="9999" /></label>
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
      <router-link v-if="userStore.isAdmin" to="/extensions?tab=knowledge">前往知识库</router-link>
    </div>

    <ul v-else-if="viewMode === 'list'" class="paper-list">
      <li v-for="paper in papers" :key="paper.rid" class="paper-card" @click="openPaper(paper)">
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
          <div v-if="paper.note_excerpt" class="concept-def">{{ paper.note_excerpt }}</div>
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
      <button class="btn" :disabled="page >= totalPages" @click="gotoPage(page + 1)">下一页</button>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { parseLiteratureLink } from '@/utils/literature_reader'
import { apiGet, apiPost } from '@/apis/base'
import {
  listLiteraturePapers,
  listLiteratureTasks,
  literatureWorkspaceApi
} from '@/apis/literature_api'
import PaperDetail from '@/components/literature/PaperDetail.vue'
import LibraryWorkbench from '@/components/literature/LibraryWorkbench.vue'
import ImportPanel from '@/components/literature/ImportPanel.vue'
import LiteratureSessions from '@/components/literature/LiteratureSessions.vue'
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
    PaperDetail,
    LibraryWorkbench,
    MarkdownPreview,
    ImportPanel,
    LiteratureSessions,
    DiaryView,
    LiteratureGraphView,
    SettingsModal
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
    const selectedRid = ref('')
    const secondaryRid = ref('')
    const readerLinkNotice = ref('')
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
    const openSession = (id) => {
      activeThreadId.value = id
      viewMode.value = 'sessions'
    }
    const onSessionSelected = ({ id, rid }) => {
      activeThreadId.value = id
      if (rid) selectedRid.value = rid
    }
    const closeReader = () => {
      selectedRid.value = ''
      if (viewMode.value === 'reader') viewMode.value = 'list'
    }
    const switchWorkspace = (key) => {
      if (key === 'tasks') loadTasks()
      else viewMode.value = key
    }
    const resumeRun = async (run) => {
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
    const literatureTasks = ref([])
    const importing = ref(false)

    const loadConcepts = async () => {
      viewMode.value = 'concepts'
      const data = await apiGet('/api/literature/concepts')
      concepts.value = data?.concepts || []
    }
    const generateMoc = async () => {
      if (!mocTopic.value.trim()) return
      mocLoading.value = true
      try {
        const result = await apiPost('/api/literature/reports/topic-moc', {
          topic: mocTopic.value.trim()
        })
        selectedRid.value = ''
        mocTopic.value = ''
        alert(`主题导读已生成（${result.sources} 篇来源），见「综述报告」列表`)
      } catch (err) {
        alert(`生成失败：${err?.message || err}`)
      } finally {
        mocLoading.value = false
      }
    }
    const loadReports = async () => {
      viewMode.value = 'reports'
      const data = await apiGet('/api/literature/reports')
      reports.value = data?.reports || []
    }
    const loadTasks = async () => {
      viewMode.value = 'tasks'
      try {
        const [tasks, history] = await Promise.all([
          listLiteratureTasks(),
          literatureWorkspaceApi.runs()
        ])
        literatureTasks.value = tasks?.tasks || []
        runs.value = history?.items || []
      } catch (error) {
        alert(error.message || '任务读取失败')
      }
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
      selectedRid.value = paper.rid
      viewMode.value = 'reader'
    }
    const popoutReader = () => {
      window.open(
        `/literature?rid=${encodeURIComponent(selectedRid.value)}`,
        '_blank',
        'noopener,noreferrer'
      )
    }
    const openReaderLink = async (target) => {
      const query = parseLiteratureLink(target),
        sourceRid = selectedRid.value
      if (!query) {
        readerLinkNotice.value = '链接不是受支持的文献双链'
        return
      }
      try {
        const data = await apiGet(
          `/api/literature/resolve-link?target=${encodeURIComponent(query)}`
        )
        if (selectedRid.value !== sourceRid) return
        if (data.rid) openPaper({ rid: data.rid })
        else
          readerLinkNotice.value = '未找到唯一可访问文献，请在目录中按标题或 DOI 检索；未自动跳转。'
      } catch (error) {
        readerLinkNotice.value = error.message || '文献链接解析失败'
      }
    }
    const kindLabel = (kind) => KIND_LABELS[kind] || kind

    onMounted(() => {
      if (typeof route.query.rid === 'string' && route.query.rid.length <= 255)
        openPaper({ rid: route.query.rid })
      load()
    })
    onBeforeUnmount(() => loadGeneration++)

    return {
      userStore,
      workspaceTabs,
      switchWorkspace,
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
      secondaryRid,
      readerLinkNotice,
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
      importing,
      loadConcepts,
      generateMoc,
      loadReports,
      loadTasks,
      importBib,
      importPaperAgent
    }
  }
}
</script>

<style lang="less" scoped>
@import '@/assets/css/base.css';

.literature-view {
  padding: 16px 24px;
}
.workspace-nav,
.directory-filters,
.settings-links {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin: 12px 0;
}
.workspace-nav {
  border-bottom: 1px solid var(--gray-150);
  padding-bottom: 12px;
}
.workspace-nav .active {
  border-color: var(--main-color);
  color: var(--main-color);
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
  grid-template-columns: minmax(360px, 1fr) minmax(360px, 1fr);
  gap: 16px;
}
@media (max-width: 1100px) {
  .reading-workspace.split {
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
  color: #cf1322;
  font-size: 12px;
}
</style>
