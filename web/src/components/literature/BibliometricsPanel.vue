<template>
  <section class="bibliometrics-panel">
    <section class="intake-card" aria-labelledby="bibliometric-intake-title">
      <h3 id="bibliometric-intake-title">导入题录，建立分析清单</h3>
      <p class="hint">
        支持 PubMed MEDLINE/CSV、WoS CSV/文本、知网官网 XLS、万方 XLSX，以及 RIS/EndNote。无需上传
        PDF，不调用 OCR 或大模型。
      </p>
      <label class="file-picker"
        >上传清单文件
        <input
          type="file"
          multiple
          accept=".csv,.xls,.xlsx,.txt,.nbib,.ris,.enw,.ciw"
          :disabled="importing"
          @change="uploadReferences"
        />
      </label>
      <p class="hint">
        单文件 ≤64 MiB、≤50000 条，每批最多 20 个文件。PubMed 推荐完整 MEDLINE；仅 PMID
        列表、趋势统计表、摘要展示文本不属于完整题录。
      </p>
      <p class="hint">
        原 BibTeX 通道仍在「导入」面板。文献与导入批次保存在服务端，继续上传或离开页面不会覆盖历史。
      </p>
      <p v-if="importing" role="status" aria-live="polite">{{ importProgress }}</p>
      <ul v-if="importReports.length" class="import-reports" aria-label="逐文件导入结果">
        <li v-for="(report, index) in importReports" :key="index" :class="{ error: report.error }">
          <strong>{{ report.source }}</strong>
          <span v-if="report.error">：{{ report.error }}</span>
          <template v-else
            >：{{ sourceLabel(report.format_detected) }} · {{ report.records }} 条记录，新增
            {{ report.papers_written }}，重复 {{ report.papers_skipped }}，去重成员
            {{ report.papers_unique ?? '—' }}，补全元数据 {{ report.metadata_enriched || 0 }}
            <span class="hint"
              >缺年份 {{ report.quality?.missing_year || 0 }} · 缺摘要
              {{ report.quality?.missing_abstract || 0 }} · 缺关键词
              {{ report.quality?.missing_keywords || 0 }}</span
            >
          </template>
        </li>
      </ul>
      <p v-if="batchError" class="error" role="alert">{{ batchError }}</p>
    </section>
    <nav class="database-switch" aria-label="文献数据库来源">
      <button
        v-for="item in databaseOptions"
        :key="item.key"
        class="btn"
        :aria-pressed="databaseChoice === item.key"
        :disabled="importing"
        @click="selectDatabase(item.key)"
      >
        {{ item.label }} <span>{{ sourceCounts[item.key] ?? '—' }}</span>
      </button>
    </nav>
    <p class="hint">
      默认显示全部可见主文献，包含旧通道题录。点击来源立即切换并清除旧筛选；来源数量未应用检索或年份条件。
      同一文献可属于多个数据库，各来源数量不可相加；来源列仍显示首次登记格式。
    </p>
    <p v-if="sourceError" class="error" role="alert">{{ sourceError }}</p>
    <form class="scope-toolbar" @submit.prevent="applyFilters" aria-label="分析范围">
      <label
        >数据范围
        <select v-model="scopeChoice" :disabled="importing" @change="selectScope">
          <option value="imports">我的全部导入批次（跨文件去重）</option>
          <option value="all">全部可见文献（含历史题录）</option>
          <option v-for="batch in batches" :key="batch.batch_id" :value="batch.batch_id">
            {{ batch.source }} · {{ sourceLabel(batch.format) }} ·
            {{ batch.report.papers_unique }} 篇
          </option>
        </select>
      </label>
      <label
        >检索<input
          v-model="queryDraft"
          maxlength="200"
          placeholder="题名、作者、期刊、DOI、关键词"
          :disabled="importing"
      /></label>
      <label
        >起始年<input
          v-model="yearFromDraft"
          type="number"
          min="1500"
          max="2100"
          :disabled="importing"
      /></label>
      <label
        >结束年<input
          v-model="yearToDraft"
          type="number"
          min="1500"
          max="2100"
          :disabled="importing"
      /></label>
      <button class="btn" :disabled="importing || loading">应用筛选</button>
    </form>
    <p v-if="batchesHaveMore" class="hint">
      已载入 {{ batches.length }} 个历史批次，较早批次也可继续查看。
      <button class="btn" :disabled="batchesLoading || importing" @click="loadBatches(true)">
        {{ batchesLoading ? '载入中…' : '载入更多历史批次' }}
      </button>
    </p>
    <div class="view-tabs" role="tablist" aria-label="计量视图">
      <button
        v-for="(label, key) in {
          records: '文献清单',
          analysis: '统计与网络'
        }"
        :key="key"
        class="btn"
        role="tab"
        :data-tab-key="key"
        :id="`bibliometric-tab-${key}`"
        :aria-selected="view === key"
        :tabindex="view === key ? 0 : -1"
        :aria-controls="`bibliometric-panel-${key}`"
        @click="view = key"
        @keydown="onTabKeydown"
      >
        {{ label }}
      </button>
    </div>
    <header class="panel-header">
      <h3>{{ view === 'records' ? '文献清单' : '文献计量分析' }}</h3>
      <span v-if="overview" class="hint">
        当前范围 {{ overview.papers }} 篇主文献（{{ overview.year_min || '—' }}–{{
          overview.year_max || '—'
        }}）， 排除参考条目与回收站
      </span>
      <span class="toolbar-spacer"></span>
      <button class="btn" :disabled="loading || importing" @click="refreshWorkspace">
        {{ loading ? '统计中…' : '刷新' }}
      </button>
    </header>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="!loading && overview && overview.papers === 0" class="hint">
      当前范围没有文献，请上传官网导出的清单或调整数据范围与筛选条件。
    </p>

    <p v-if="overview && overview.papers > 0" class="hint">
      清单与统计采用同一范围，跨批次文献只计一次；{{
        overview.missing_year || 0
      }}
      篇缺少年份，不进入年度趋势。 {{ countryUnidentified }} 篇未识别国家/地区，不猜测城市或邮箱。
      机构按地址串统计，作者姓名未消歧，不等同于独立机构或研究者人数。
    </p>

    <section
      v-show="view === 'records'"
      id="bibliometric-panel-records"
      role="tabpanel"
      aria-labelledby="bibliometric-tab-records"
      tabindex="0"
    >
      <p class="hint">
        官网清单是题录元数据，不代表已获得全文
        PDF。可展开记录查看摘要与关键词；无被引数字时显示“—”，不当作零。
      </p>
      <p v-if="recordsLoading" role="status">正在载入文献清单…</p>
      <p v-if="recordsError" class="error" role="alert">{{ recordsError }}</p>
      <div class="records-table-wrap">
        <table class="records-table" :aria-busy="recordsLoading">
          <thead>
            <tr>
              <th scope="col">序号</th>
              <th scope="col">文献</th>
              <th scope="col">作者</th>
              <th scope="col">期刊 / 年份</th>
              <th scope="col">来源</th>
              <th scope="col">被引</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(paper, index) in records" :key="paper.rid">
              <td>{{ offset + index + 1 }}</td>
              <td class="record-title">
                <details>
                  <summary>{{ paper.title || paper.doi || paper.pmid || '无题名' }}</summary>
                  <p>
                    DOI：{{ paper.doi || '—' }} · PMID：{{ paper.pmid || '—' }} ·
                    {{ paper.has_pdf ? '已关联 PDF 来源' : '仅题录，未关联 PDF' }}
                  </p>
                  <p class="abstract">{{ paper.abstract || '清单未提供摘要' }}</p>
                  <p>关键词：{{ paper.keywords.join('；') || '清单未提供关键词' }}</p>
                  <p>机构：{{ paper.affiliations.join('；') || '清单未提供机构' }}</p>
                </details>
              </td>
              <td>{{ paper.authors.join('；') || '—' }}</td>
              <td>{{ paper.journal || '—' }}<br />{{ paper.year ?? '—' }}</td>
              <td>{{ sourceLabel(paper.source) }}</td>
              <td>{{ paper.external_cited ?? '—' }}</td>
            </tr>
            <tr v-if="!recordsLoading && !recordsError && !records.length">
              <td colspan="6">暂无匹配文献，上传清单后会在这里显示。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <nav class="pagination" aria-label="文献清单分页">
        <span>共 {{ recordsTotal }} 篇 · 第 {{ Math.floor(offset / pageSize) + 1 }} 页</span>
        <button class="btn" :disabled="recordsLoading || offset === 0" @click="changePage(-1)">
          上一页
        </button>
        <button
          class="btn"
          :disabled="recordsLoading || offset + pageSize >= recordsTotal"
          @click="changePage(1)"
        >
          下一页
        </button>
      </nav>
    </section>

    <section
      v-show="view === 'analysis'"
      id="bibliometric-panel-analysis"
      role="tabpanel"
      aria-labelledby="bibliometric-tab-analysis"
      tabindex="0"
    >
      <template v-if="overview && overview.papers > 0">
        <div class="stat-chips" aria-label="文献库概览">
          <span class="chip">期刊 {{ overview.journals }}</span>
          <span class="chip">作者 {{ overview.authors }}</span>
          <span class="chip">关键词 {{ overview.keywords }}</span>
          <span v-for="(count, kind) in overview.by_kind" :key="kind" class="chip"
            >{{ kindLabel(kind) }} {{ count }}</span
          >
          <span v-for="(count, language) in overview.by_language" :key="language" class="chip"
            >{{ languageLabel(language) }} {{ count }}</span
          >
        </div>

        <div class="charts-grid">
          <div class="chart-card span-2">
            <h4>年度发文趋势（按语言）</h4>
            <div ref="trendEl" class="chart chart-trend"></div>
          </div>
          <div class="chart-card">
            <h4>期刊分布 Top20</h4>
            <div ref="journalsEl" class="chart chart-bars"></div>
          </div>
          <div class="chart-card">
            <h4>机构地址 Top20（按篇计数）</h4>
            <div ref="affiliationsEl" class="chart chart-bars"></div>
            <p v-if="countries.length" class="country-chips">
              国家/地区：<span v-for="item in countries" :key="item.name" class="chip">
                {{ item.name }} {{ item.count }}
              </span>
            </p>
          </div>
          <div class="chart-card">
            <h4>文献类型</h4>
            <div ref="kindEl" class="chart chart-donut"></div>
          </div>
          <div class="chart-card">
            <h4>语言分布</h4>
            <div ref="languageEl" class="chart chart-donut"></div>
          </div>
          <div class="chart-card span-2">
            <h4>
              共现网络
              <span class="network-toggle" role="group" aria-label="网络类型">
                <button
                  class="btn"
                  :class="{ active: networkType === 'keyword' }"
                  :disabled="loading"
                  @click="switchNetwork('keyword')"
                >
                  关键词
                </button>
                <button
                  class="btn"
                  :class="{ active: networkType === 'author' }"
                  :disabled="loading"
                  @click="switchNetwork('author')"
                >
                  作者合作
                </button>
              </span>
              <label class="hint"
                >TopN
                <select v-model.number="networkTop" :disabled="loading" @change="loadNetwork">
                  <option v-for="count in [20, 50, 100, 200]" :key="count" :value="count">
                    {{ count }}
                  </option>
                </select>
              </label>
              <label class="hint"
                >最少共现
                <select v-model.number="minPair" :disabled="loading" @change="loadNetwork">
                  <option v-for="count in [1, 2, 5, 10]" :key="count" :value="count">
                    {{ count }}
                  </option>
                </select>
              </label>
              <span v-if="networkStats" class="hint">
                {{ networkStats.nodes }} 节点 · {{ networkStats.links }} 条边
                <template v-if="networkStats.links_truncated">（边数已截断）</template>
              </span>
            </h4>
            <p v-if="networkLoading" class="hint" role="status">网络统计中…</p>
            <div ref="networkEl" class="chart chart-network" :aria-busy="networkLoading"></div>
            <p class="hint">节点大小=出现频次，连线粗细=同篇共现次数；拖拽平移、滚轮缩放。</p>
          </div>
        </div>
      </template>
    </section>
  </section>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'
import { literatureWorkspaceApi } from '@/apis/literature_api'
import { getColorByIndex, initColorPalette } from '@/utils/chartColors'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'
import { useUserStore } from '@/stores/user'
import { useTablistKeyboard } from '@/composables/useTablistKeyboard'
import {
  buildDistributionOption,
  buildNetworkOption,
  buildTopBarsOption,
  buildYearlyTrendOption,
  kindLabel,
  languageLabel
} from '@/utils/literature/bibliometricCharts'

const emit = defineEmits(['imported', 'open-paper'])

const loading = ref(false)
const error = ref('')
const overview = ref(null)
const metricData = shallowRef(null)
const countries = ref([])
const countryUnidentified = ref(0)
const networkType = ref('keyword')
const networkTop = ref(50)
const minPair = ref(2)
const networkLoading = ref(false)
const networkStats = ref(null)
const trendEl = ref(null)
const journalsEl = ref(null)
const affiliationsEl = ref(null)
const kindEl = ref(null)
const languageEl = ref(null)
const networkEl = ref(null)
const view = ref('records')
const { onTabKeydown } = useTablistKeyboard(
  (key) => {
    view.value = key
  },
  () => ['records', 'analysis']
)
const batches = ref([]),
  batchesHaveMore = ref(false),
  batchesLoading = ref(false),
  batchError = ref('')
const databaseOptions = [
  { key: '', label: '全部' },
  { key: 'pubmed', label: 'PubMed' },
  { key: 'wos', label: 'WoS' },
  { key: 'cnki', label: '知网' },
  { key: 'wanfang', label: '万方' },
  { key: 'other', label: '其他 / 未分类' }
]
const databaseChoice = ref(''),
  sourceCounts = ref({}),
  sourceError = ref('')
const importReports = ref([]),
  importing = ref(false),
  importProgress = ref('')
const scopeChoice = ref('all'),
  queryDraft = ref(''),
  yearFromDraft = ref(''),
  yearToDraft = ref('')
const filters = ref({ only_imports: false, database: '' })
const records = ref([]),
  recordsTotal = ref(0),
  recordsLoading = ref(false),
  recordsError = ref('')
const pageSize = 25,
  offset = ref(0)
let recordRequest = 0,
  batchRequest = 0,
  sourceRequest = 0,
  nextBatchOffset = 0,
  uploadRequest = 0
const sourceLabel = (value) =>
  ({
    'pubmed-nbib': 'PubMed MEDLINE',
    'pubmed-csv': 'PubMed CSV',
    'wos-txt': 'WoS 文本',
    'wos-csv': 'WoS CSV',
    'cnki-xls': '知网 XLS',
    'wanfang-xlsx': '万方 XLSX',
    ris: 'RIS',
    endnote: 'EndNote',
    bibtex: 'BibTeX'
  })[value] ||
  value ||
  '未知'

// echarts 实例按容器元素持有（复用 setOption 刷新，卸载统一 dispose）
const charts = new Map()
const user = useUserStore()
let overviewRequest = 0
let networkRequest = 0
let resizeObserver
const clearCharts = () => {
  charts.forEach((chart) => chart.dispose())
  charts.clear()
  resizeObserver?.disconnect()
}
const pause = () => {
  overviewRequest += 1
  networkRequest += 1
  recordRequest += 1
  batchRequest += 1
  sourceRequest += 1
  uploadRequest += 1
  loading.value = false
  networkLoading.value = false
  recordsLoading.value = false
  importing.value = false
  batchesLoading.value = false
}
const active = useLiteraturePollingScope(pause, () => refreshWorkspace())
const theme = { colors: [], textColor: '#666', splitLineColor: '#e5e6eb', axisLineColor: '#c9cdd4' }

const cssVar = (name, fallback) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name)
  return value && value.trim() ? value.trim() : fallback
}

const mountChart = (element, option) => {
  if (!element) return
  let chart = charts.get(element)
  if (!chart) {
    chart = echarts.init(element)
    charts.set(element, chart)
    resizeObserver?.observe(element)
  }
  chart.setOption(option, true)
}

const loadNetwork = async () => {
  if (!active.value || !user.isLoggedIn) return
  const ticket = ++networkRequest
  networkLoading.value = true
  networkStats.value = null
  error.value = ''
  charts.get(networkEl.value)?.clear()
  try {
    const network = await literatureWorkspaceApi.metricNetwork(
      networkType.value,
      networkTop.value,
      minPair.value,
      filters.value
    )
    if (ticket !== networkRequest || !active.value) return
    networkStats.value = network.stats || null
    await nextTick()
    if (ticket !== networkRequest || !active.value) return
    mountChart(networkEl.value, buildNetworkOption(network, theme))
  } catch (err) {
    if (ticket === networkRequest && active.value) error.value = err.message || '网络数据加载失败'
  } finally {
    if (ticket === networkRequest) networkLoading.value = false
  }
}

const loadAll = async () => {
  if (loading.value || !active.value || !user.isLoggedIn) return
  const ticket = ++overviewRequest
  networkRequest += 1
  networkLoading.value = false
  loading.value = true
  error.value = ''
  try {
    initColorPalette()
    theme.colors = Array.from({ length: 10 }, (_, index) => getColorByIndex(index))
    theme.textColor = cssVar('--color-text-secondary', '#666')
    theme.splitLineColor = cssVar('--gray-150', '#e5e6eb')
    theme.axisLineColor = cssVar('--gray-300', '#c9cdd4')
    const data = await literatureWorkspaceApi.bibliometrics(20, filters.value)
    if (ticket !== overviewRequest || !active.value) return
    clearCharts()
    overview.value = data.overview
    metricData.value = data
    countries.value = data.countries_top || []
    countryUnidentified.value = data.country_unidentified_papers || 0
    networkStats.value = null
    if (!overview.value?.papers) return
    if (view.value === 'analysis') await renderMetrics(data)
  } catch (err) {
    if (ticket === overviewRequest && active.value) {
      clearCharts()
      overview.value = null
      metricData.value = null
      error.value = err.message || '计量数据加载失败'
    }
  } finally {
    if (ticket === overviewRequest) loading.value = false
  }
}

const renderMetrics = async (data) => {
  await nextTick()
  if (metricData.value !== data || !active.value || view.value !== 'analysis') return
  mountChart(trendEl.value, buildYearlyTrendOption(data.yearly || [], theme))
  mountChart(journalsEl.value, buildTopBarsOption(data.journals_top || [], theme))
  mountChart(
    affiliationsEl.value,
    buildTopBarsOption(data.affiliations_top || [], theme, { color: theme.colors[1] })
  )
  mountChart(kindEl.value, buildDistributionOption(data.overview?.by_kind, theme, kindLabel))
  mountChart(
    languageEl.value,
    buildDistributionOption(data.overview?.by_language, theme, languageLabel)
  )
  if (overview.value?.papers > 0) await loadNetwork()
}

const switchNetwork = (type) => {
  if (networkType.value === type) return
  networkType.value = type
  loadNetwork()
}

const handleResize = () => {
  charts.forEach((chart) => chart.resize())
}

const loadSources = async () => {
  if (!active.value || !user.isLoggedIn) return
  const ticket = ++sourceRequest
  try {
    const data = await literatureWorkspaceApi.metricSources()
    if (ticket !== sourceRequest || !active.value) return
    sourceCounts.value = Object.fromEntries(
      (data.items || []).map((item) => [item.database, item.papers])
    )
    sourceError.value = ''
  } catch (err) {
    if (ticket === sourceRequest && active.value)
      sourceError.value = err.message || '数据库来源载入失败'
  }
}

const loadBatches = async (append = false) => {
  if (!active.value || !user.isLoggedIn || batchesLoading.value) return
  const ticket = ++batchRequest
  const start = append ? nextBatchOffset : 0
  batchesLoading.value = true
  try {
    const data = await literatureWorkspaceApi.referenceBatches(start)
    if (ticket !== batchRequest || !active.value) return
    const items = data.items || []
    // Receipts are immutable. Refresh the newest page without losing already
    // loaded historical choices or replacing an older selected batch.
    const ordered = append ? [...batches.value, ...items] : [...items, ...batches.value]
    batches.value = [...new Map(ordered.map((batch) => [batch.batch_id, batch])).values()]
    nextBatchOffset = start + items.length
    batchesHaveMore.value = Boolean(data.has_more)
    batchError.value = ''
  } catch (err) {
    if (ticket === batchRequest && active.value)
      batchError.value = err.message || '导入批次载入失败'
  } finally {
    if (ticket === batchRequest) batchesLoading.value = false
  }
}

const loadRecords = async () => {
  if (!active.value || !user.isLoggedIn) return
  const ticket = ++recordRequest
  recordsLoading.value = true
  recordsError.value = ''
  try {
    const data = await literatureWorkspaceApi.metricRecords(filters.value, offset.value, pageSize)
    if (ticket !== recordRequest || !active.value) return
    records.value = data.items || []
    recordsTotal.value = data.total || 0
  } catch (err) {
    if (ticket === recordRequest && active.value) {
      records.value = []
      recordsTotal.value = 0
      recordsError.value = err.message || '文献清单载入失败'
    }
  } finally {
    if (ticket === recordRequest) recordsLoading.value = false
  }
}

const refreshWorkspace = () => {
  loadSources()
  loadBatches()
  loadRecords()
  loadAll()
}

const applyFilters = () => {
  if (importing.value) return
  const from = yearFromDraft.value ? Number(yearFromDraft.value) : null
  const to = yearToDraft.value ? Number(yearToDraft.value) : null
  if (
    [from, to].some(
      (value) => value !== null && (!Number.isInteger(value) || value < 1500 || value > 2100)
    )
  ) {
    recordsError.value = '年份必须为 1500–2100 的整数'
    return
  }
  if (from && to && from > to) {
    recordsError.value = '起始年份不能大于结束年份'
    return
  }
  overviewRequest += 1
  networkRequest += 1
  recordRequest += 1
  loading.value = networkLoading.value = recordsLoading.value = false
  filters.value = {
    only_imports: scopeChoice.value !== 'all',
    database: databaseChoice.value,
    batch_ids: ['all', 'imports'].includes(scopeChoice.value) ? [] : [scopeChoice.value],
    query: queryDraft.value.trim(),
    year_from: from,
    year_to: to
  }
  offset.value = 0
  records.value = []
  recordsTotal.value = 0
  overview.value = null
  metricData.value = null
  clearCharts()
  loadRecords()
  loadAll()
}

const selectDatabase = (database) => {
  if (importing.value) return
  databaseChoice.value = database
  scopeChoice.value = 'all'
  queryDraft.value = yearFromDraft.value = yearToDraft.value = ''
  view.value = 'records'
  applyFilters()
}

const selectScope = () => {
  if (importing.value) return
  databaseChoice.value = ''
  queryDraft.value = yearFromDraft.value = yearToDraft.value = ''
  view.value = 'records'
  applyFilters()
}

const changePage = (delta) => {
  if (recordsLoading.value || importing.value) return
  offset.value = Math.max(0, offset.value + delta * pageSize)
  loadRecords()
}

const uploadReferences = async (event) => {
  const files = Array.from(event.target.files || [])
  event.target.value = ''
  if (!files.length || importing.value || !active.value || !user.isLoggedIn) return
  if (files.length > 20 || files.some((file) => file.size > 64 * 1024 * 1024)) {
    batchError.value = '每批最多 20 个清单文件，单文件不得超过 64 MiB'
    return
  }
  if (files.some((file) => file.name.toLowerCase().endsWith('.bib'))) {
    batchError.value = 'BibTeX 请使用原「导入」面板，并选择全部可见文献查看'
    return
  }
  const ticket = ++uploadRequest
  const owner = JSON.stringify([user.uid, user.departmentId, user.isLoggedIn])
  const stillCurrent = () =>
    ticket === uploadRequest &&
    active.value &&
    owner === JSON.stringify([user.uid, user.departmentId, user.isLoggedIn])
  importing.value = true
  importReports.value = []
  try {
    for (const [index, file] of files.entries()) {
      if (!stillCurrent()) return
      importProgress.value = `正在导入 ${index + 1}/${files.length}：${file.name}`
      try {
        const report = await literatureWorkspaceApi.importReferences(file)
        if (!stillCurrent()) return
        importReports.value.push({ ...report, source: file.name })
      } catch (err) {
        if (!stillCurrent()) return
        importReports.value.push({
          source: file.name,
          error: err.message || '导入失败，请刷新批次确认状态后重试'
        })
      }
    }
    if (stillCurrent()) {
      importing.value = false
      if (importReports.value.some((report) => !report.error)) {
        emit('imported')
        selectDatabase('')
        loadBatches()
        loadSources()
      }
    }
  } finally {
    if (stillCurrent()) importing.value = false
  }
}

onMounted(() => {
  resizeObserver = new ResizeObserver(handleResize)
  refreshWorkspace()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  pause()
  window.removeEventListener('resize', handleResize)
  clearCharts()
})

watch(
  () => [user.uid, user.departmentId, user.isLoggedIn],
  () => {
    pause()
    clearCharts()
    overview.value = null
    metricData.value = null
    countries.value = []
    networkStats.value = null
    error.value = ''
    batches.value = []
    nextBatchOffset = 0
    databaseChoice.value = ''
    sourceCounts.value = {}
    sourceError.value = ''
    batchError.value = recordsError.value = importProgress.value = ''
    importReports.value = []
    records.value = []
    recordsTotal.value = 0
    queryDraft.value = yearFromDraft.value = yearToDraft.value = ''
    scopeChoice.value = 'all'
    filters.value = { only_imports: false, database: '' }
    offset.value = 0
    refreshWorkspace()
  },
  { flush: 'sync' }
)
watch(view, async (value) => {
  if (value === 'analysis' && metricData.value?.overview?.papers)
    await renderMetrics(metricData.value)
  await nextTick()
  handleResize()
})
</script>

<style lang="less" scoped>
.bibliometrics-panel {
  max-width: 1180px;
  line-height: 1.6;
}
.intake-card {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 16px;
  background: var(--gray-0);
  margin-bottom: 16px;
  h3 {
    margin: 0 0 8px;
  }
}
.file-picker,
.scope-toolbar label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.file-picker input {
  padding: 8px 0;
}
.scope-toolbar {
  display: flex;
  align-items: end;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  input,
  select {
    padding: 6px 8px;
    border: 1px solid var(--gray-150);
    border-radius: 4px;
    color: var(--color-text);
    background: var(--gray-0);
  }
  input[type='number'] {
    width: 90px;
  }
}
.database-switch {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  [aria-pressed='true'] {
    color: var(--main-color);
    border-color: var(--main-color);
    background: var(--main-10);
  }
  button:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: 2px;
  }
}
.view-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  [aria-selected='true'] {
    color: var(--main-color);
    border-color: var(--main-color);
  }
}
.import-reports {
  padding-left: 20px;
  font-size: 13px;
  .hint {
    display: block;
  }
}
.records-table-wrap {
  overflow-x: auto;
  margin-top: 12px;
}
.records-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
  min-width: 760px;
  th {
    padding: 10px;
    text-align: left;
    background: var(--gray-25);
    color: var(--color-text);
  }
  td {
    padding: 12px 10px;
    border-bottom: 1px solid var(--gray-150);
    vertical-align: top;
    color: var(--color-text);
  }
  .record-title {
    min-width: 270px;
    width: 42%;
  }
  summary {
    cursor: pointer;
    line-height: 1.6;
  }
  .abstract {
    white-space: pre-wrap;
    max-width: 70ch;
  }
  summary:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: 2px;
  }
}
.pagination {
  display: flex;
  justify-content: end;
  gap: 10px;
  align-items: center;
  margin-top: 12px;
  font-size: 13px;
}
.panel-header {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  h3,
  h4 {
    margin: 0;
  }
}
.toolbar-spacer {
  flex: 1;
}
.hint {
  font-size: 13px;
  color: var(--color-text-secondary, #888);
}
.error {
  color: var(--color-error-700, #d03029);
}
.stat-chips,
.country-chips .chip {
  display: inline-flex;
  margin: 2px 8px 2px 0;
}
.stat-chips {
  flex-wrap: wrap;
  margin: 8px 0 14px;
}
.chip {
  padding: 2px 10px;
  border: 1px solid var(--gray-150, #e5e6eb);
  border-radius: 12px;
  font-size: 12px;
  color: var(--color-text-secondary, #666);
}
.charts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 14px;
}
.chart-card {
  padding: 12px 14px;
  border: 1px solid var(--gray-150, #e5e6eb);
  border-radius: 10px;
  background: var(--color-bg-container, #fff);
  h4 {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    font-size: 14px;
    margin: 0 0 8px;
  }
}
.span-2 {
  grid-column: 1 / -1;
}
.network-toggle {
  display: inline-flex;
  gap: 6px;
}
.chart {
  width: 100%;
}
.chart-trend {
  height: 300px;
}
.chart-bars {
  height: 420px;
}
.chart-donut {
  height: 260px;
}
.chart-network {
  height: 520px;
}
@media (max-width: 960px) {
  .charts-grid {
    grid-template-columns: 1fr;
  }
}
</style>
