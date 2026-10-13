<template>
  <section class="topic-workspace">
    <h3>BERTopic 文献主题建模</h3>
    <p class="hint">
      基于标题与摘要进行探索性聚类，不替代原文证据或人工主题审阅。历史分析保留各自的语料快照，切换数据库不会自动重新训练。
    </p>
    <p
      v-if="capabilities.database_security && !capabilities.database_security.ready"
      class="hint"
      role="status"
    >
      {{
        capabilities.database_security.required
          ? '生产准入未通过，主题数据操作已被服务端阻断；请联系管理员检查受限数据库连接。'
          : '当前为开发/验收配置，数据库最小权限尚未通过生产准入；不能将离线测试通过视为生产上线完成。'
      }}
    </p>
    <form class="controls" @submit.prevent="preview">
      <label
        >分析口径<select v-model="options.mode">
          <option value="papers">去重独立文献</option>
          <option value="records">原始导入记录（保留重复）</option>
        </select></label
      >
      <label>分析名称<input v-model="options.name" maxlength="160" /></label>
      <button class="btn" :disabled="busy">预览当前筛选语料</button>
      <label class="upload"
        >导入离线 BERTopic 结果包<input
          type="file"
          accept=".zip"
          :disabled="busy"
          @change="importArchive"
      /></label>
    </form>
    <p v-if="previewData" class="hint" role="status">
      本次有效输入 {{ previewData.documents }} 条，关联
      {{ previewData.unique_papers }} 篇文献；仅标题 {{ previewData.title_only }} 条，缺少年份
      {{ previewData.missing_year }} 条。中文 {{ previewData.languages.zh || 0 }} 条，其他文本
      {{ previewData.languages.en || 0 }} 条。
    </p>
    <details class="settings">
      <summary>模型与运行设置</summary>
      <div class="controls">
        <label
          >联合嵌入模型<select v-model="options.embedding_spec">
            <option value="">请选择已配置模型</option>
            <option
              v-for="m in capabilities.models.filter((x) => x.model === 'BAAI/bge-m3')"
              :key="m.spec"
              :value="m.spec"
            >
              {{ m.name }}
            </option>
          </select></label
        >
        <label
          ><input v-model="options.chinese_comparison" type="checkbox" />
          中文补充与双模型对照</label
        >
        <label v-if="options.chinese_comparison"
          >中文模型<select v-model="options.chinese_embedding_spec">
            <option value="">请选择</option>
            <option
              v-for="m in capabilities.models.filter((x) => x.model === 'BAAI/bge-large-zh-v1.5')"
              :key="m.spec"
              :value="m.spec"
            >
              {{ m.name }}
            </option>
          </select></label
        >
        <label
          >随机种子<input v-model.number="options.seed" type="number" min="0" max="2147483647"
        /></label>
        <label
          ><input v-model="options.stability" type="checkbox" /> 比较两个随机种子的稳定性</label
        >
        <label
          ><input v-model="options.allow_remote" type="checkbox" />
          允许将标题和摘要发送至已配置嵌入供应商，可能产生费用</label
        >
      </div>
    </details>
    <div class="controls">
      <button
        class="btn primary"
        :disabled="busy || !previewData || !options.allow_remote || !capabilities.worker_available"
        @click="start"
      >
        创建新的分析
      </button>
      <span class="hint">{{
        capabilities.worker_available
          ? '专用计算 worker 在线'
          : '专用计算 worker 未在线；仍可查看或导入离线历史'
      }}</span>
      <span v-if="busy" role="status">处理中，请勿重复提交…</span>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="controls history">
      <label
        >历史分析<select
          :value="run?.run_id || ''"
          :disabled="busy"
          @change="openRun($event.target.value)"
        >
          <option value="">选择分析</option>
          <option v-for="r in runs" :key="r.run_id" :value="r.run_id">
            {{ r.name }} · {{ statusLabel(r.status) }} · {{ r.created_at }}
          </option>
        </select></label
      >
      <button class="btn" :disabled="busy" @click="loadHistory(false)">刷新历史</button>
      <button v-if="hasMore" class="btn" :disabled="busy" @click="loadHistory(true)">
        更早的分析
      </button>
    </div>
    <template v-if="run">
      <p class="hint">
        {{ run.name }} · {{ statusLabel(run.status) }} ·
        {{ run.mode === 'records' ? '原始记录口径' : '去重文献口径' }} · 快照
        {{ run.input_digest.slice(0, 12) }} · {{ run.progress.stage || '等待调度' }}
        {{ run.progress.completed ?? 0 }}/{{ run.progress.total ?? run.progress.documents }}
      </p>
      <p v-if="run.error" class="error" role="alert">{{ run.error }}</p>
      <button v-if="isRunning" class="btn" :disabled="busy" @click="cancel">取消分析</button>
      <template v-if="run.models">
        <div class="controls">
          <label
            >模型<select v-model="model">
              <option v-for="(_, key) in run.models" :key="key" :value="key">
                {{ modelLabel(key) }}
              </option>
            </select></label
          >
          <label
            >主题<select
              :value="selectedTopic ?? ''"
              @change="chooseTopic($event.target.value === '' ? null : Number($event.target.value))"
            >
              <option value="">全部记录</option>
              <option value="-1">离群 (-1)</option>
              <option v-for="t in currentModel.topics" :key="t.topic" :value="t.topic">
                T{{ t.topic }} {{ t.label }}（{{ t.count }}）
              </option>
            </select></label
          >
          <button class="btn" :disabled="busy" @click="exportCsv">导出逐条结果 CSV</button>
          <button
            class="btn"
            :disabled="busy || !run.has_embeddings || !capabilities.worker_available"
            @click="rerun"
          >
            用原快照离线重新拟合
          </button>
        </div>
        <p class="hint">
          {{ currentModel.topics.length }} 个非离群主题，{{
            currentModel.documents - currentModel.outliers
          }}
          条已归类，{{ currentModel.outliers }}
          条离群。成员强度不是判断正确率，离群记录不会被自动分配为最近主题。
        </p>
        <p v-for="warning in currentModel.warnings || []" :key="warning" class="hint">
          {{ warning }}
        </p>
        <p
          v-for="[language, value] in Object.entries(currentModel.language_concentration || {})"
          :key="language"
          class="hint"
        >
          {{ language === 'zh' ? '中文' : '其他文本' }} {{ value.documents }} 条，最大集中组
          {{ value.largest_topic === -1 ? '离群' : `T${value.largest_topic}` }} 占
          {{ (value.largest_topic_share * 100).toFixed(1) }}%。
          <span v-if="value.largest_topic_share >= 0.8"
            >集中度较高，请人工检查语言效应与主题内容。</span
          >
        </p>
        <details>
          <summary>质量、参数与时间演化诊断</summary>
          <p class="hint">
            两个随机种子仅提供敏感性比较，不是稳定性置信区间。主题编号不能跨模型直接比较。
          </p>
          <pre>{{
            JSON.stringify(
              {
                selected: currentModel.selected,
                stability: currentModel.stability,
                method: run.method
              },
              null,
              2
            )
          }}</pre>
          <table v-if="currentModel.dynamic?.length">
            <thead>
              <tr>
                <th>五年窗口起点</th>
                <th>主题</th>
                <th>频数</th>
                <th>该时期关键词</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in currentModel.dynamic" :key="i">
                <td>{{ row.year }}</td>
                <td>T{{ row.topic }}</td>
                <td>{{ row.count }}</td>
                <td>{{ row.words }}</td>
              </tr>
            </tbody>
          </table>
        </details>
        <div class="chart-tabs" role="tablist" aria-label="主题分析图表">
          <button
            v-for="[key, label] in TOPIC_CHARTS"
            :key="key"
            class="btn"
            role="tab"
            :id="`topic-chart-${key}`"
            :data-tab-key="key"
            :aria-selected="chartKind === key"
            :tabindex="chartKind === key ? 0 : -1"
            aria-controls="topic-chart-panel"
            @click="chartKind = key"
            @keydown="onChartKeydown"
          >
            {{ label }}
          </button>
        </div>
        <section
          id="topic-chart-panel"
          role="tabpanel"
          :aria-labelledby="`topic-chart-${chartKind}`"
        >
          <label v-if="chartKind === 'groups'"
            >分组维度<select v-model="chartSettings.groupBy">
              <option value="source">数据库</option>
              <option value="journal">期刊</option>
              <option value="kind">文献类型</option>
              <option value="language">实际文本语言</option>
              <option value="country">明确识别的国家</option>
              <option value="affiliation">机构地址（原字段）</option>
              <option value="clc">中图分类号（原字段）</option>
            </select></label
          >
          <label v-if="chartKind === 'cloud'"
            >词云口径<select v-model="chartSettings.wordWeight">
              <option value="ctfidf">c-TF-IDF 权重</option>
              <option value="frequency">原始分词频数</option>
            </select></label
          >
          <p v-if="chartLoading" role="status">载入图表数据…</p>
          <div ref="chartEl" class="topic-chart" :aria-busy="chartLoading"></div>
          <p class="hint">
            投影距离仅供探索；气泡和节点大小经过显示缩放。权重词云与原始词频不同。年度分母包含离群记录，超出分析年份的记录只保留在语料中，当前年份可能不完整。数据库到主题的桑基图表示构成，不代表文献迁移。
          </p>
          <p v-if="chartKind === 'groups'" class="hint">
            显示频数前 20 组，其余归入“其他”。多机构、多国记录按组内整计数；缺失值保留为
            unknown，各组不一定互斥，不猜测城市所属国家。
          </p>
          <p
            v-if="selectedTopic === -1 && ['keywords', 'cloud', 'ranks'].includes(chartKind)"
            class="hint"
          >
            离群记录没有主题关键词，未用其他主题替代。
          </p>
          <button class="btn" :disabled="!chartData" @click="downloadChart">下载当前图 SVG</button>
        </section>
        <h4>主题关联文献</h4>
        <p v-if="documentsLoading" role="status">载入文献…</p>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>题名（点击查看）</th>
                <th>年份</th>
                <th>来源</th>
                <th>主题</th>
                <th>成员强度</th>
                <th>最近主题余弦</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in rows" :key="d.document_key">
                <td>
                  <button class="paper-link" @click="emit('open-paper', { rid: d.rid })">
                    {{ d.title }}
                  </button>
                  <details>
                    <summary>摘要与来源记录</summary>
                    <p>{{ d.abstract || '此记录只有标题' }}</p>
                    <small>{{ d.document_key }}</small>
                  </details>
                </td>
                <td>{{ d.year ?? '—' }}</td>
                <td>{{ d.source }}</td>
                <td>{{ d.topic === -1 ? '离群' : `T${d.topic}` }}</td>
                <td>{{ d.membership.toFixed(3) }}</td>
                <td>{{ d.centroid_cosine.toFixed(3) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="controls">
          <button class="btn" :disabled="offset === 0 || documentsLoading" @click="offset -= 25">
            上一页</button
          ><span
            >{{ total }} 条，当前 {{ total ? offset + 1 : 0 }}–{{
              Math.min(offset + 25, total)
            }}</span
          ><button
            class="btn"
            :disabled="offset + 25 >= total || documentsLoading"
            @click="offset += 25"
          >
            下一页
          </button>
        </div>
      </template>
    </template>
    <p v-else class="hint">
      先预览已导入文献，或上传 D:\bertopic 的四库结果 ZIP。离线包中的脚本和 pickle 模型不会执行。
    </p>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'
import { useUserStore } from '@/stores/user'
import { useThemeStore } from '@/stores/theme'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'
import { useTablistKeyboard } from '@/composables/useTablistKeyboard'
import { getColorByIndex, initColorPalette } from '@/utils/chartColors'
import { buildTopicChart, TOPIC_CHARTS } from '@/utils/literature/topicCharts'

const props = defineProps({ filters: { type: Object, default: () => ({}) } })
const emit = defineEmits(['open-paper'])
const user = useUserStore(),
  themeStore = useThemeStore()
const options = ref({
  mode: 'papers',
  name: '文献主题分析',
  embedding_spec: '',
  chinese_embedding_spec: '',
  allow_remote: false,
  chinese_comparison: false,
  seed: 42,
  stability: true
})
const previewData = ref(null),
  runs = ref([]),
  hasMore = ref(false),
  run = shallowRef(null)
const capabilities = ref({ worker_available: false, models: [] }),
  busy = ref(false),
  error = ref('')
const model = ref('joint_bge_m3'),
  selectedTopic = ref(null),
  offset = ref(0),
  total = ref(0),
  rows = ref([])
const documentsLoading = ref(false),
  chartLoading = ref(false),
  chartEl = ref(null),
  chartData = shallowRef(null),
  chartKind = ref('overview')
const chartSettings = ref({ groupBy: 'source', wordWeight: 'ctfidf' })
const isRunning = computed(() => ['PENDING', 'RUNNING'].includes(run.value?.status))
const currentModel = computed(() => run.value?.models?.[model.value] || { topics: [] })
let generation = 0,
  historyOffset = 0,
  historyRequest = 0,
  runRequest = 0,
  documentRequest = 0,
  chartRequest = 0,
  previewRequest = 0,
  timer,
  chart,
  observer
const owner = () => JSON.stringify([user.uid, user.departmentId, user.isLoggedIn])
const stamp = () => ({ generation, owner: owner() })
const valid = (s) => active.value && s.generation === generation && s.owner === owner()
const statusLabel = (value) =>
  ({
    PENDING: '等待调度',
    RUNNING: '分析中',
    SUCCEEDED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消'
  })[value] || value
const modelLabel = (value) =>
  ({
    joint_bge_m3: '四库联合 bge-m3',
    chinese_bge_zh: '中文专用模型',
    chinese_bge_m3_comparison: '中文 bge-m3 对照'
  })[value] || value
const { onTabKeydown: onChartKeydown } = useTablistKeyboard(
  (key) => {
    chartKind.value = key
  },
  () => TOPIC_CHARTS.map(([key]) => key)
)
const active = useLiteraturePollingScope(
  () => {
    generation++
    clearTimeout(timer)
    busy.value = false
    chartLoading.value = false
    documentsLoading.value = false
    disposeChart()
  },
  () => {
    initialize()
    if (run.value) openRun(run.value.run_id)
  }
)

async function action(fn) {
  if (busy.value) return
  const s = stamp()
  busy.value = true
  error.value = ''
  try {
    await fn(s)
  } catch (e) {
    if (valid(s)) error.value = e.message || '主题操作失败'
  } finally {
    if (valid(s)) busy.value = false
  }
}
async function preview() {
  const ticket = ++previewRequest
  await action(async (s) => {
    const value = await api.previewTopics(options.value, props.filters)
    if (valid(s) && ticket === previewRequest) previewData.value = value
  })
}
async function start() {
  await action(async (s) => {
    const value = await api.startTopics(
      { ...options.value, expected_digest: previewData.value.input_digest },
      props.filters
    )
    if (valid(s)) {
      await loadHistory(false)
      await openRun(value.run_id)
    }
  })
}
async function importArchive(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  if (file.size > 1024 * 1024 * 1024) {
    error.value = '离线结果包不能超过 1 GiB'
    return
  }
  await action(async (s) => {
    const value = await api.importTopics(file)
    if (valid(s)) {
      await loadHistory(false)
      await openRun(value.run_id)
    }
  })
}
async function loadHistory(append = false) {
  const s = stamp(),
    ticket = ++historyRequest
  try {
    const page = await api.topicRuns(append ? historyOffset : 0)
    if (!valid(s) || ticket !== historyRequest) return
    const map = new Map((append ? runs.value : []).map((r) => [r.run_id, r]))
    for (const r of page.items) map.set(r.run_id, r)
    runs.value = [...map.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
    historyOffset = page.next_offset ?? (append ? historyOffset : 0) + page.items.length
    hasMore.value = page.has_more
    if (run.value) await openRun(run.value.run_id)
    else if (page.items.length) await openRun(page.items[0].run_id)
  } catch (e) {
    if (valid(s) && ticket === historyRequest) error.value = e.message
  }
}
async function openRun(id) {
  if (!id) return
  const s = stamp(),
    ticket = ++runRequest
  clearTimeout(timer)
  try {
    const value = await api.topicRun(id)
    if (!valid(s) || ticket !== runRequest) return
    const changed = run.value?.run_id !== id
    run.value = value
    if (changed) {
      model.value = 'joint_bge_m3'
      selectedTopic.value = null
      offset.value = 0
      chartData.value = null
      rows.value = []
    }
    if (value.models) {
      await loadDocuments()
      await loadCharts()
    }
    if (isRunning.value) timer = setTimeout(() => openRun(id), 4000)
  } catch (e) {
    if (valid(s) && ticket === runRequest) {
      error.value = e.message
      run.value = null
      rows.value = []
      chartData.value = null
      disposeChart()
    }
  }
}
async function loadDocuments() {
  if (!run.value?.models) return
  const s = stamp(),
    ticket = ++documentRequest,
    id = run.value.run_id,
    selectedModel = model.value
  documentsLoading.value = true
  try {
    const value = await api.topicData(id, selectedModel, {
      topic: selectedTopic.value,
      offset: offset.value
    })
    if (
      !valid(s) ||
      ticket !== documentRequest ||
      run.value?.run_id !== id ||
      model.value !== selectedModel
    )
      return
    rows.value = value.items
    total.value = value.total
  } catch (e) {
    if (valid(s) && ticket === documentRequest) {
      error.value = e.message
      rows.value = []
    }
  } finally {
    if (valid(s) && ticket === documentRequest) documentsLoading.value = false
  }
}
async function loadCharts() {
  if (!run.value?.models) return
  const s = stamp(),
    ticket = ++chartRequest,
    id = run.value.run_id,
    selectedModel = model.value
  chartLoading.value = true
  try {
    const value = await api.topicData(id, selectedModel, { view: 'charts' })
    if (
      !valid(s) ||
      ticket !== chartRequest ||
      run.value?.run_id !== id ||
      model.value !== selectedModel
    )
      return
    chartData.value = value
    await drawChart()
  } catch (e) {
    if (valid(s) && ticket === chartRequest) error.value = e.message
  } finally {
    if (valid(s) && ticket === chartRequest) chartLoading.value = false
  }
}
function chooseTopic(topic) {
  selectedTopic.value = topic
  offset.value = 0
  loadDocuments()
  drawChart()
}
async function drawChart() {
  await nextTick()
  if (!active.value || !chartEl.value || !chartData.value) return
  initColorPalette()
  const css = getComputedStyle(document.documentElement)
  if (!chart) {
    chart = echarts.init(chartEl.value, null, { renderer: 'svg' })
    chart.on('click', (event) => {
      if (event.data?.documentKey) {
        const rid = chartData.value?.metadata?.[event.data.documentKey]?.rid
        if (rid) emit('open-paper', { rid })
        return
      }
      if (typeof event.data?.topic === 'number') chooseTopic(event.data.topic)
    })
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(chartEl.value)
  }
  chart.setOption(
    buildTopicChart(
      chartKind.value,
      chartData.value,
      {
        colors: Array.from({ length: 12 }, (_, i) => getColorByIndex(i)),
        textColor: css.getPropertyValue('--color-text'),
        splitLineColor: css.getPropertyValue('--gray-150')
      },
      selectedTopic.value,
      chartSettings.value
    ),
    { notMerge: true }
  )
}
function disposeChart() {
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
}
function download(blob, name) {
  const url = URL.createObjectURL(blob),
    a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function exportCsv() {
  await action(async (s) => {
    const blob = await api.exportTopics(run.value.run_id, model.value)
    if (valid(s)) download(blob, 'document_topics.csv')
  })
}
function downloadChart() {
  if (chart) {
    const url = chart.getDataURL({ type: 'svg' }),
      a = document.createElement('a')
    a.href = url
    a.download = `bertopic-${chartKind.value}.svg`
    a.click()
  }
}
async function cancel() {
  await action(async (s) => {
    const id = run.value.run_id
    await api.cancelTopics(id)
    if (valid(s)) await openRun(id)
  })
}
async function rerun() {
  await action(async (s) => {
    const value = await api.rerunTopics(run.value.run_id, {
      name: `${run.value.name.slice(0, 150)} · 重新拟合`
    })
    if (valid(s)) {
      await loadHistory(false)
      await openRun(value.run_id)
    }
  })
}
watch(
  () => props.filters,
  () => {
    previewRequest++
    previewData.value = null
  },
  { deep: true }
)
watch(
  () => options.value.mode,
  () => {
    previewRequest++
    previewData.value = null
  }
)
watch(model, () => {
  selectedTopic.value = null
  offset.value = 0
  chartData.value = null
  loadDocuments()
  loadCharts()
})
watch(offset, loadDocuments)
watch([chartKind, () => themeStore.isDark], drawChart)
watch(chartSettings, drawChart, { deep: true })
watch(owner, () => {
  generation++
  clearTimeout(timer)
  runs.value = []
  run.value = null
  rows.value = []
  chartData.value = null
  previewData.value = null
  busy.value = false
  error.value = ''
  total.value = 0
  capabilities.value = { worker_available: false, models: [] }
  documentsLoading.value = false
  chartLoading.value = false
  historyOffset = 0
  disposeChart()
  if (active.value) initialize()
})
async function initialize() {
  const s = stamp()
  try {
    const value = await api.topicCapabilities()
    if (valid(s)) capabilities.value = value
  } catch (e) {
    if (valid(s)) error.value = e.message
  }
  await loadHistory(false)
}
onMounted(initialize)
onBeforeUnmount(() => {
  generation++
  clearTimeout(timer)
  disposeChart()
})
</script>

<style scoped lang="less">
.topic-workspace {
  display: grid;
  gap: 12px;
  color: var(--color-text);
}
.controls,
.chart-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
label {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
}
input,
select,
.btn {
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
  color: var(--color-text);
  padding: 6px 10px;
}
.btn {
  cursor: pointer;
}
.btn:hover {
  border-color: var(--main-color);
}
.btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.btn[aria-selected='true'],
.primary {
  border-color: var(--main-color);
  color: var(--main-color);
  background: var(--main-10);
}
.btn:focus-visible,
select:focus-visible,
input:focus-visible,
.paper-link:focus-visible {
  outline: 2px solid var(--main-color);
  outline-offset: 2px;
}
.hint {
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.6;
  margin: 0;
}
.error {
  color: var(--color-error-700);
}
.settings,
.history {
  padding: 12px;
  background: var(--gray-25);
  border: 1px solid var(--gray-150);
  border-radius: 8px;
}
summary {
  cursor: pointer;
}
pre {
  max-height: 320px;
  overflow: auto;
  font-size: 12px;
  background: var(--gray-25);
  padding: 12px;
}
.topic-chart {
  width: 100%;
  height: 560px;
  min-width: 300px;
}
.table-wrap {
  overflow: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  padding: 8px;
  text-align: left;
  border-bottom: 1px solid var(--gray-150);
  vertical-align: top;
}
.paper-link {
  color: var(--main-color);
  background: none;
  border: none;
  text-align: left;
  cursor: pointer;
  padding: 0;
}
details p {
  white-space: pre-wrap;
  max-width: 80ch;
}
.upload {
  flex-wrap: wrap;
}
@media (max-width: 700px) {
  .controls label {
    width: 100%;
    flex-wrap: wrap;
  }
  .topic-chart {
    height: 420px;
  }
}
</style>
