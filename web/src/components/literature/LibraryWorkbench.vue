<template>
  <section class="workbench">
    <h3>建库与文献工作台</h3>
    <p>复用知识源权限；笔记与向量均为派生导航，不作为原文证据。</p>
    <div class="actions">
      <button class="btn" :disabled="busy" @click="createSession('global-qa')">全库问答</button>
      <button class="btn" :disabled="busy" @click="createSession('manage')">管理会话</button>
      <button class="btn" :disabled="busy" @click="createSession('lit')">目录检索会话</button>
      <button class="btn" :disabled="busy" @click="downloadVault">导出 Obsidian</button>
      <button class="btn" :disabled="busy" @click="buildIndex">构建导航向量</button>
    </div>
    <p v-if="indexState">
      向量索引 {{ indexState.index_id }}：{{ indexState.status }}
      <span v-if="indexState.document_count"> · {{ indexState.document_count }} 个片段</span>
      <span v-if="indexState.error"> · {{ indexState.error }}</span>
    </p>
    <h4>六步建库（后台可恢复）</h4>
    <label>可选 Bib 文件 <input type="file" accept=".bib,.txt" @change="readBib" /></label>
    <p>清洗规则按“任一命中”计算；默认仅引用记录，人工锁定文献受保护。空规则不移除文献。</p>
    <label
      >高级规则 JSON
      <textarea v-model="rulesText" rows="4" aria-label="清洗规则 JSON" spellcheck="false" />
    </label>
    <div class="cleaning-fields">
      <label>删除年份低于<input v-model="yearMin" type="number" min="0" /></label>
      <label>删除 IF 低于<input v-model="ifMin" type="number" min="0" step="0.1" /></label>
      <label>删除库内被引低于<input v-model="citedMin" type="number" min="0" /></label>
      <label
        >保留分区（不选则不启用）
        <select v-model="quartiles" multiple aria-label="清洗保留分区">
          <option v-for="q in ['Q1', 'Q2', 'Q3', 'Q4']" :key="q" :value="q">{{ q }}</option>
          <option value="">未知分区</option>
        </select>
      </label>
    </div>
    <label
      ><input v-model="includeMains" type="checkbox" />允许规则覆盖主文献（仍须预览确认）</label
    >
    <div class="actions">
      <button class="btn" :disabled="busy" @click="startLibrary">启动六步建库</button>
      <button class="btn" :disabled="busy" @click="previewCleaning">单独预览清洗</button>
      <button class="btn" v-if="runId" :disabled="busy" @click="refreshLibrary">
        刷新建库进度
      </button>
    </div>
    <template v-if="libraryState">
      <p>建库 {{ libraryState.run_id }}：{{ libraryState.status }}</p>
      <ol>
        <li v-for="(step, name) in libraryState.steps" :key="name">
          {{ name }}：{{ step.status }}
          <span v-if="step.detail?.processed !== undefined">
            · 已处理 {{ step.detail.processed }}</span
          >
          <span v-if="step.detail?.ok !== undefined">
            · 成功 {{ step.detail.ok }} · 跳过 {{ step.detail.skipped || 0 }} · 失败
            {{ step.detail.failed || 0 }}</span
          >
          <span v-if="step.detail?.error" class="error"> · {{ step.detail.error }}</span>
          <span v-if="step.detail?.matched !== undefined">
            · 指标匹配 {{ step.detail.matched }}</span
          >
        </li>
      </ol>
      <p v-if="libraryState.error" class="error">{{ libraryState.error }}</p>
    </template>
    <div v-if="cleanPreview" class="preview">
      <p>
        预览：保留 {{ cleanPreview.to_keep }} 篇，移入回收站 {{ cleanPreview.to_remove }} 篇。
        {{ cleanPreview.scope === 'references_only' ? '仅引用记录' : '包含主文献' }}
      </p>
      <ul>
        <li v-for="sample in cleanPreview.samples" :key="sample.rid">
          {{ sample.title || sample.rid }} · {{ sample.matched_rules.join(' / ') }}
        </li>
      </ul>
      <button class="btn" v-if="cleanPreview.to_remove" :disabled="busy" @click="confirmCleaning">
        确认将预览中的文献移入回收站
      </button>
    </div>
    <h4>我的全局研究卡片</h4>
    <CardsPanel @editing-change="$emit('editing-change', $event)" />
    <h4>可恢复回收站</h4>
    <button class="btn" :disabled="busy" @click="loadTrash">查看回收站</button>
    <ul v-if="trashLoaded">
      <li v-for="paper in trash" :key="paper.rid">
        {{ paper.title || paper.rid }}
        <button class="btn" :disabled="busy" @click="restore(paper.rid)">恢复</button>
      </li>
      <li v-if="!trash.length">当前权限范围内没有回收站文献</li>
    </ul>
    <h4>期刊指标（JCR / 中科院分区）</h4>
    <p>
      导入后按期刊名（&amp;/and
      等价）关联影响因子与分区；关联为幂等操作，最新年份优先，人工锁定文献受保护。
    </p>
    <p v-if="journalStatus">
      已导入映射 {{ journalStatus.mapping_count }} 条 · {{ journalStatus.journal_count }} 种期刊
      <span v-if="journalStatus.year_max">
        · 年份 {{ journalStatus.year_min }}–{{ journalStatus.year_max }}</span
      >
      · 已覆盖 IF 文献 {{ journalStatus.if_covered_count }} 篇
    </p>
    <label
      >指标表文件（xlsx / csv）<input
        type="file"
        accept=".xlsx,.xlsm,.csv,.tsv,.txt"
        @change="previewJournals"
    /></label>
    <template v-if="journalPreview">
      <p>
        预览：{{ journalPreview.row_count }} 行 · 来源
        {{ journalPreview.sources.join(' / ') || '未识别' }}
        <span v-if="journalPreview.skipped">
          · 跳过（无期刊名 {{ journalPreview.skipped.no_journal }} / 无年份
          {{ journalPreview.skipped.no_year }} / 无指标 {{ journalPreview.skipped.no_value }}）
        </span>
      </p>
      <ul>
        <li v-for="sheet in journalPreview.sheets" :key="sheet.name">
          {{ sheet.name || '默认表' }} · {{ sheet.source }} · {{ sheet.row_count }} 行
          <span v-if="!sheet.recognized">（未识别期刊名列）</span>
        </li>
      </ul>
      <ul>
        <li v-for="(sample, index) in journalPreview.sample" :key="index">
          {{ sample.journal_name }} · {{ sample.year }} · IF {{ sample.jif ?? '—' }} ·
          {{ sample.quartile || sample.cas_zone || '—' }}
        </li>
      </ul>
      <div class="actions">
        <button class="btn" :disabled="busy" @click="importJournals">确认导入指标表</button>
        <button class="btn" :disabled="busy" @click="journalPreview = null">取消导入</button>
      </div>
    </template>
    <div class="actions">
      <button class="btn" :disabled="busy" @click="refreshJournalStatus">刷新指标状态</button>
      <button class="btn" :disabled="busy" @click="attachJournalMetrics">
        关联期刊指标（写入 IF / 分区）
      </button>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
import { literatureWorkspaceApi } from '@/apis/literature_api'
import CardsPanel from './CardsPanel.vue'

const emit = defineEmits(['session', 'editing-change'])
const props = defineProps({ resumeRun: { type: Object, default: null } })
const busy = ref(false)
const notice = ref('')
const bibText = ref('')
const sourceName = ref('')
const rulesText = ref('{}')
const yearMin = ref('')
const ifMin = ref('')
const citedMin = ref('')
const quartiles = ref([])
const includeMains = ref(false)
const runId = ref('')
const libraryState = ref(null)
const indexState = ref(null)
const cleanPreview = ref(null)
const trash = ref([])
const trashLoaded = ref(false)
// 期刊指标（JCR/中科院）：预览 → 显式确认导入 → 关联写回 IF/分区
const journalFile = ref(null)
const journalPreview = ref(null)
const journalStatus = ref(null)
let disposed = false
let libraryTimer = null
let indexTimer = null
const perform = async (action) => {
  if (busy.value || disposed) return
  busy.value = true
  notice.value = ''
  try {
    await action()
  } catch (error) {
    notice.value = error?.message || '操作失败'
  } finally {
    busy.value = false
  }
}
const rules = () => {
  const value = JSON.parse(rulesText.value)
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('清洗规则必须是 JSON 对象')
  for (const [key, input] of [
    ['year', yearMin],
    ['impact_factor', ifMin],
    ['library_citations', citedMin]
  ]) {
    if (input.value !== '' && input.value != null) value[key] = { min: Number(input.value) }
  }
  if (quartiles.value.length) value.quartile = { keep: quartiles.value }
  return value
}
const readBib = (event) =>
  perform(async () => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    if (file.size > 10 * 1024 * 1024) throw new Error('Bib 文件不得超过 10 MiB')
    bibText.value = await file.text()
    sourceName.value = file.name
    notice.value = '已选择 ' + file.name + '，启动后才导入'
  })
// 期刊指标：文件先在服务端解析成摘要，人工确认后才落库（不自动导入）
const previewJournals = (event) =>
  perform(async () => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    if (file.size > 16 * 1024 * 1024) throw new Error('指标表不得超过 16 MiB')
    journalFile.value = file
    journalPreview.value = await literatureWorkspaceApi.previewJournals(file)
    notice.value = '已解析 ' + file.name + '，确认后才会写入指标表'
  })
const importJournals = () =>
  perform(async () => {
    if (!journalFile.value) throw new Error('请先选择指标表文件并预览')
    const result = await literatureWorkspaceApi.importJournals(journalFile.value)
    journalPreview.value = null
    journalFile.value = null
    notice.value =
      '指标表已导入：新增 ' +
      result.created +
      ' 条，更新 ' +
      result.updated +
      ' 条；可继续点击「关联期刊指标」'
    await refreshJournalStatus()
  })
// 只读状态刷新不走 perform 的 busy 门禁（导入/关联会 await 它，嵌套 perform 会自锁）
const refreshJournalStatus = async () => {
  try {
    journalStatus.value = await literatureWorkspaceApi.journalsStatus()
  } catch {
    // 失败保持上次状态，不打断工作台的写操作
  }
}
const attachJournalMetrics = () =>
  perform(async () => {
    const result = await literatureWorkspaceApi.attachJournalMetrics()
    notice.value =
      '已关联 ' +
      result.matched +
      ' 篇（未匹配 ' +
      result.unmatched +
      '，人工锁定 ' +
      result.protected +
      '）'
    await refreshJournalStatus()
  })
const createSession = (kind) =>
  perform(async () => {
    const result = await apiPost('/api/literature/sessions', { kind })
    emit('session', result.thread.id)
  })
const downloadVault = () =>
  perform(async () => {
    const response = await apiGet('/api/literature/export/obsidian', {}, true, 'blob')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'literature_vault.zip'
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    notice.value = '已导出当前可访问产物；缺失文件说明见压缩包 manifest'
  })
const refreshIndex = async () => {
  clearTimeout(indexTimer)
  if (disposed || !indexState.value?.index_id) return
  const id = indexState.value.index_id
  try {
    const result = await apiGet('/api/literature/indexes/navigation/' + encodeURIComponent(id))
    if (disposed || indexState.value?.index_id !== id) return
    indexState.value = result
    if (!disposed && ['PENDING', 'RUNNING'].includes(indexState.value.status))
      indexTimer = setTimeout(refreshIndex, 5000)
  } catch (error) {
    notice.value = error?.message || '读取索引状态失败'
  }
}
const buildIndex = () =>
  perform(async () => {
    indexState.value = await apiPost('/api/literature/indexes/navigation', {})
    notice.value = '索引已持久化入队；构建成功后启用向量通道'
    await refreshIndex()
  })
const refreshLibrary = async () => {
  clearTimeout(libraryTimer)
  if (disposed || !runId.value) return
  const id = runId.value
  try {
    const result = await apiGet('/api/literature/library/pipeline/' + encodeURIComponent(id))
    if (disposed || runId.value !== id) return
    libraryState.value = result
    const clean = libraryState.value.steps?.clean_preview
    cleanPreview.value =
      clean?.status === 'WAITING_CONFIRM' && clean.detail?.plan_id ? clean.detail : null
    if (
      !disposed &&
      ['PENDING', 'RUNNING', 'WAITING_CONFIRM'].includes(libraryState.value.status)
    ) {
      libraryTimer = setTimeout(refreshLibrary, 5000)
    }
  } catch (error) {
    notice.value = error?.message || '读取建库进度失败'
  }
}
const startLibrary = () =>
  perform(async () => {
    const result = await apiPost('/api/literature/library/pipeline', {
      bib_text: bibText.value,
      source_name: sourceName.value,
      cleaning_rules: rules(),
      include_mains: includeMains.value
    })
    runId.value = result.run_id
    cleanPreview.value = null
    notice.value = '后台建库已提交；命中清洗规则时会等待人工确认'
    await refreshLibrary()
  })
const previewCleaning = () =>
  perform(async () => {
    cleanPreview.value = await apiPost('/api/literature/library/cleaning/preview', {
      rules: rules(),
      include_mains: includeMains.value
    })
  })
const confirmCleaning = () =>
  perform(async () => {
    const plan = cleanPreview.value
    if (!window.confirm('确认将预览中 ' + plan.to_remove + ' 篇文献移入可恢复回收站？')) return
    const result = await apiPost('/api/literature/library/cleaning/' + plan.plan_id + '/confirm', {
      plan_hash: plan.plan_hash
    })
    cleanPreview.value = null
    notice.value = '已移入回收站 ' + result.removed + ' 篇；原文件未删除'
    if (runId.value) await refreshLibrary()
  })
const loadTrash = () =>
  perform(async () => {
    const result = await apiGet('/api/literature/library/trash')
    trash.value = result.items || []
    trashLoaded.value = true
  })
const restore = (rid) =>
  perform(async () => {
    await apiPost('/api/literature/library/trash/' + encodeURIComponent(rid) + '/restore')
    trash.value = trash.value.filter((paper) => paper.rid !== rid)
    notice.value = '文献已恢复；被撤销的导航发布不会自动重激活'
  })
watch(
  () => props.resumeRun,
  async (run) => {
    if (!run?.id) return
    clearTimeout(libraryTimer)
    clearTimeout(indexTimer)
    cleanPreview.value = null
    notice.value = '正在读取已有运行；不会重新提交或重复计费。'
    if (run.type === 'library') {
      runId.value = run.id
      await refreshLibrary()
    } else if (run.type === 'navigation') {
      indexState.value = { index_id: run.id }
      await refreshIndex()
    }
  },
  { immediate: true }
)
onUnmounted(() => {
  disposed = true
  clearTimeout(libraryTimer)
  clearTimeout(indexTimer)
})
// 期刊映射状态只读，进工作台即拉一次（失败不打断页面）
onMounted(() => {
  refreshJournalStatus()
})
</script>

<style scoped>
.workbench {
  max-width: 1000px;
  line-height: 1.7;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.cleaning-fields {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.cleaning-fields label {
  display: flex;
  flex-direction: column;
}
button,
input {
  padding: 6px;
  margin: 4px;
}
textarea {
  display: block;
  width: 100%;
  max-width: 800px;
  font-family: monospace;
  padding: 8px;
}
.preview {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 12px;
}
.error {
  color: var(--color-error-700);
}
</style>
