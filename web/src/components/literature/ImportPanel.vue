<template>
  <section class="import-panel">
    <h3>导入文献</h3>
    <div class="import-tabs" role="tablist" aria-label="导入类型">
      <button
        v-for="(label, key) in tabs"
        :key="key"
        class="btn"
        role="tab"
        :data-tab-key="key"
        :id="`import-tab-${key}`"
        :aria-selected="mode === key"
        :aria-controls="`import-panel-${key}`"
        :tabindex="mode === key ? 0 : -1"
        @click="mode = key"
        @keydown="onTabKeydown"
      >
        {{ label }}
      </button>
    </div>
    <template v-if="mode === 'pdf'">
      <p role="tabpanel" :id="`import-panel-pdf`" :aria-labelledby="`import-tab-pdf`" tabindex="0">
        PDF 进入所选知识源的 pdf_evidence
        契约：共用解析和质量门禁。默认索引激活后进入文献库；仅解析留在个人草稿，不纳入证据检索。
      </p>
      <template v-if="userStore.isAdmin">
        <label
          >目标 PDF 知识源<select v-model="kbId" aria-label="目标 PDF 知识源">
            <option value="">请选择</option>
            <option v-for="kb in databases" :key="kb.kb_id" :value="kb.kb_id">{{ kb.name }}</option>
          </select></label
        >
        <button class="btn" :disabled="busy" @click="loadDatabases">刷新知识源</button>
        <label
          >处理模式<select v-model="processingMode" :disabled="busy">
            <option value="parse_compile">解析并激活 + 自动编译（渲染 → L1/L2）</option>
            <option value="full">完整流程（渲染 → 复核 → 编译 → 翻译/总结，含计费）</option>
            <option value="parse_only">仅解析（草稿阅读，不索引、不激活）</option>
          </select></label
        >
        <label
          >批量 PDF<input
            :disabled="busy || !kbId"
            type="file"
            accept=".pdf"
            multiple
            @change="importPdf"
        /></label>
        <p>
          同名文件需确认；不会覆盖旧证据版本。选择编译/完整流程时，解析入库完成后自动继续对应工作流。
        </p>
      </template>
      <p v-else>
        PDF 写入沿用知识库管理员权限；请联系管理员上传或授权，不能以文献导入绕过来源治理。
      </p>
      <section>
        <h4>仅解析草稿（不属于文献库或证据库）</h4>
        <button class="btn" :disabled="busy" @click="loadDrafts">刷新草稿</button>
        <button
          v-for="draft in drafts"
          :key="draft.revision_id"
          class="btn"
          :disabled="!draft.ready || busy"
          @click="openDraft(draft)"
        >
          {{ draft.title }} · {{ draft.status }}
        </button>
        <DraftReader v-if="draftDocument" :document="draftDocument" @close="draftDocument = null" />
      </section>
    </template>
    <template v-else-if="mode === 'markdown'">
      <p
        role="tabpanel"
        id="import-panel-markdown"
        aria-labelledby="import-tab-markdown"
        tabindex="0"
      >
        Markdown 免 OCR 导入为阅读产物，不自动赋予原文证据资格。
      </p>
      <label>标题<input v-model="title" maxlength="4096" /></label>
      <label>DOI（可选）<input v-model="doi" maxlength="255" /></label>
      <label
        >Markdown 文件<input
          type="file"
          accept=".md,.markdown"
          :disabled="busy"
          @change="readMarkdown"
      /></label>
      <textarea v-model="markdown" rows="10" aria-label="导入 Markdown 正文" />
      <button class="btn" :disabled="busy || !markdown.trim()" @click="importMarkdown">
        导入 Markdown
      </button>
    </template>
    <template v-else>
      <p role="tabpanel" id="import-panel-bib" aria-labelledby="import-tab-bib" tabindex="0">
        文献清单保留身份与引用边；重复记录（DOI/PMID/指纹）自动跳过，不覆盖人工元数据。
      </p>
      <label
        >格式<select v-model="referenceFormat" :disabled="busy" aria-label="清单格式">
          <option value="auto">自动识别（推荐）</option>
          <option value="bibtex">BibTeX（.bib）</option>
          <option value="pubmed-nbib">PubMed（.nbib）</option>
          <option value="pubmed-csv">PubMed（CSV / 合并题录 CSV）</option>
          <option value="wos-txt">Web of Science（Plain Text / Tab-delimited .txt）</option>
          <option value="wos-csv">Web of Science（CSV）</option>
          <option value="cnki-xls">知网（官网 HTML-XLS）</option>
          <option value="wanfang-xlsx">万方（Excel .xlsx）</option>
          <option value="ris">RIS（.ris / 部分万方导出）</option>
          <option value="endnote">EndNote（知网/万方导出 .txt）</option>
        </select></label
      >
      <label
        >清单文件<input
          type="file"
          accept=".bib,.txt,.ris,.nbib,.enw,.ciw,.csv,.xls,.xlsx"
          :disabled="busy"
          @change="importBib"
      /></label>
      <details class="format-guide">
        <summary>各官网导出路径</summary>
        <p>
          单文件最多 64 MiB / 50000 条；导入后可在「计量分析」查看批次清单和同范围统计，上限 50000
          篇。
        </p>
        <ul>
          <li>PubMed：检索结果 → Send to → Citation manager（.nbib）</li>
          <li>
            Web of Science：检索结果 → Export → Plain Text 或 Tab-delimited（.txt）；建议选 Full
            Record and Cited References，保留作者、关键词、机构及引用字段。
          </li>
          <li>万方：Excel（.xlsx，自动识别分组标题行）或 EndNote / RIS（.txt）</li>
          <li>知网：官网 Excel（HTML .xls）或 EndNote（.txt）；保留原始编码</li>
        </ul>
        <p>
          清单不含字段就无法统计对应维度；只包含 PMID、趋势统计或摘要展示的文本不是完整题录。PubMed
          建议优先导入 MEDLINE（.txt/.nbib），字段更完整。
        </p>
      </details>
      <p v-if="referenceReport" class="reference-report">
        {{ referenceReport.source || '清单' }}：识别 {{ referenceReport.format_detected }}（{{
          referenceReport.encoding
        }}）， 共 {{ referenceReport.records }} 条 —— 新增 {{ referenceReport.papers_written }}、
        跳过 {{ referenceReport.papers_skipped }}、 引用边 {{ referenceReport.citations_written
        }}<template v-if="referenceReport.no_identity">
          、无身份 {{ referenceReport.no_identity }} 条</template
        >
      </p>
    </template>
    <p v-if="notice" role="status">{{ notice }}</p>
    <HistoryPanel />
    <ol v-if="results.length">
      <li v-for="(result, index) in results" :key="index">
        {{ result.name }}：{{ result.message }}
        <!-- 提交即回执：带单号的文件就地显示实时阶段（数据来自共享任务轮询源，不建第二真相） -->
        <template v-if="result.ticket">
          <span
            class="ticket-chip"
            :class="{
              'ticket-failure': ticketTask(result.ticket)?.is_failure,
              'ticket-done':
                ticketTask(result.ticket)?.is_terminal && !ticketTask(result.ticket)?.is_failure
            }"
          >
            {{ ticketTask(result.ticket)?.stage || '已提交，等待排队快照…' }}
          </span>
          <button class="btn" type="button" @click="emit('track', result.ticket)">跟踪</button>
        </template>
      </li>
    </ol>
  </section>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { useLiteratureTaskFeed } from '@/composables/useLiteratureTaskFeed'
import { databaseApi, documentApi, fileApi } from '@/apis/knowledge_api'
import { literatureWorkspaceApi } from '@/apis/literature_api'
import { apiGet } from '@/apis/base'
import DraftReader from '@/components/literature/DraftReader.vue'
import HistoryPanel from '@/components/literature/HistoryPanel.vue'
import { useUserStore } from '@/stores/user'
import { parseOptionsPayload } from '@/composables/useLiteratureSettings'
import { resolveDefaultOcrEngine } from '@/utils/ocr_engine'
import { confirmAction } from '@/composables/useLiteratureSessions'
import { useTablistKeyboard } from '@/composables/useTablistKeyboard'
const emit = defineEmits(['imported', 'track'])
const userStore = useUserStore()
// 就地进度吃全局共享轮询源（与状态栏/任务视图同源同频）
const taskFeed = useLiteratureTaskFeed()
const ticketTask = (ticket) =>
  taskFeed.tasks.value.find((task) => task.type === 'ingest' && task.ref === ticket)
const tabs = {
  pdf: 'PDF 契约处理',
  markdown: 'Markdown 直导',
  bib: '文献清单（BibTeX/PubMed/WoS/万方/知网）'
}
const mode = ref('pdf')
// tablist 键盘导航（roving tabindex + 方向键/Home/End）
const { onTabKeydown } = useTablistKeyboard(
  (key) => {
    mode.value = key
  },
  () => Object.keys(tabs)
)
const busy = ref(false)
const databases = ref([])
const kbId = ref('')
const processingMode = ref('parse_compile')
const drafts = ref([])
const draftDocument = ref(null)
// 已自动推进过的工作流单据（ticket → paper_rid），防重复触发与计费
const advancedTickets = new Set()

// 三模式向导的自动推进：解析入库终态（共享任务源口径）且已登记文献后，
// 按上传时选择的模式自动启动对应工作流；失败/拒绝不推进，用户可手动重试。
watch(
  () => taskFeed.tasks.value,
  () => {
    if (processingMode.value === 'parse_only') return
    for (const result of results.value) {
      if (!result.ticket || advancedTickets.has(result.ticket)) continue
      const task = ticketTask(result.ticket)
      if (!task?.paper_rid || !task.is_terminal || task.is_failure) continue
      advancedTickets.add(result.ticket)
      literatureWorkspaceApi
        .startWorkflow(task.paper_rid, processingMode.value)
        .then(() => {
          result.message = `入库完成，已自动启动${processingMode.value === 'full' ? '完整' : '编译'}工作流；进度见「任务与历史」`
        })
        .catch((error) => {
          result.message = `入库完成，但自动${processingMode.value === 'full' ? '完整流程' : '编译'}启动失败：${error.message || '请手动触发'}`
        })
    }
  },
  { deep: true }
)
const loadDrafts = () =>
  perform(async () => {
    drafts.value = (await apiGet('/api/literature/drafts')).items || []
    draftDocument.value = null
  })
const openDraft = (draft) =>
  perform(async () => {
    draftDocument.value = null
    draftDocument.value = await apiGet(
      `/api/literature/drafts/${encodeURIComponent(draft.revision_id)}/document`
    )
  })
const title = ref('')
const doi = ref('')
const markdown = ref('')
const notice = ref('')
const results = ref([])
watch(
  () => taskFeed.tasks.value,
  () => {
    let completed = false
    for (const result of results.value) {
      if (!result.ticket) continue
      const task = ticketTask(result.ticket)
      if (!task?.is_terminal) continue
      const text = task.is_failure
        ? `导入失败：${task.error || task.stage || '请查看任务详情'}`
        : task.status === 'PARSED_ONLY'
          ? '解析完成：草稿（未入库）'
          : task.status === 'CANCELLED' || task.status === 'SKIPPED'
            ? task.stage || '本次处理已取消或跳过'
            : task.paper_rid
              ? '入库成功：可打开原文、PDF 和已提取图片'
              : task.stage || '处理结束；尚未登记为文献'
      if (result.message !== text) {
        result.message = text
        completed = true
      }
    }
    if (completed) emit('imported')
  },
  { deep: true }
)
const perform = async (action) => {
  if (busy.value) return
  busy.value = true
  notice.value = ''
  try {
    await action()
  } catch (error) {
    notice.value = error.message || '导入失败'
  } finally {
    busy.value = false
  }
}
const loadDatabases = () =>
  perform(async () => {
    const data = await databaseApi.getDatabases()
    databases.value = (data.databases || []).filter((kb) => kb.contract_key === 'pdf_evidence')
    if (!databases.value.some((kb) => kb.kb_id === kbId.value)) kbId.value = ''
    if (!databases.value.length)
      notice.value = '没有可写的 pdf_evidence 知识源，请先在知识源管理中创建。'
  })
const importPdf = (event) => {
  const files = [...(event.target.files || [])]
  event.target.value = ''
  return perform(async () => {
    if (!kbId.value || files.length > 20) throw new Error('请选择 PDF 知识源；每批至多 20 个文件')
    if (
      files.some(
        (file) => !file.name.toLowerCase().endsWith('.pdf') || file.size > 100 * 1024 * 1024
      )
    )
      throw new Error('仅接受 PDF，单文件上限 100 MiB')
    const target = kbId.value
    results.value = []
    // 同名文件批量裁决：整批只问一次（原来逐文件弹原生框，20 个同名要点 20 次）。
    // "确认"= 全部同名作为新来源提交；"取消"= 全部跳过。
    let duplicateSubmit = null
    for (const file of files) {
      notice.value = `正在提交 ${file.name}（${results.value.length + 1}/${files.length}）`
      try {
        const upload = await fileApi.uploadFile(file, target)
        if (upload.has_same_name) {
          if (duplicateSubmit === null) {
            duplicateSubmit = await confirmAction(
              `检测到同名文件（如 ${file.name}）。确认将本批全部同名文件作为新来源提交？选择取消将跳过全部同名文件。`
            )
          }
          if (!duplicateSubmit) {
            results.value.push({ name: file.name, message: '已跳过（同名，未提交解析）' })
            continue
          }
        }
        const result = await documentApi.addDocuments(target, [upload.file_path], {
          ...parseOptionsPayload(),
          content_type: 'file',
          format_template: 'pdf_literature',
          pdf_evidence_pipeline: true,
          ocr_engine: resolveDefaultOcrEngine({ contractKey: 'pdf_evidence' }),
          // 上传层只有"解析+激活 / 仅解析"两种物理路径；编译/完整流程由
          // 入库终态后的自动推进（watchWorkflowTickets）衔接 workflow 三模式。
          scientific_processing_mode:
            processingMode.value === 'parse_only' ? 'parse_only' : 'activate',
          auto_index: processingMode.value !== 'parse_only',
          content_hashes: { [upload.file_path]: upload.content_hash },
          file_sizes: { [upload.file_path]: upload.size ?? file.size },
          source_paths: { [upload.file_path]: file.name }
        })
        const failed = (result.result?.items || []).find((item) => item.status === 'failed')
        // add_documents 响应带 parse_revision_id——把它作为"处理单号"交给用户，
        // 进度与终态在工作台「任务与历史 → 入库中」实时可见（不再指向知识源任务页）。
        const ticket = (result.result?.items || []).find(
          (item) => item.parse_revision_id
        )?.parse_revision_id
        const ticketText = ticket ? `（单号 ${String(ticket).slice(0, 16)}…）` : ''
        results.value.push({
          name: file.name,
          ticket: failed ? null : ticket || null,
          message: failed
            ? failed.error || '处理接收失败'
            : `上传成功，已提交处理${ticketText}，下方实时显示阶段；也可在工作台「任务与历史 → 入库中」查看`
        })
        // 立即拉一次共享任务源，让就地芯片尽快从"等待快照"变为真实阶段
        if (ticket && !failed) taskFeed.refresh()
      } catch (error) {
        results.value.push({ name: file.name, ticket: null, message: error.message || '提交失败' })
      }
    }
    const submitted = results.value.filter((result) => result.ticket).length
    notice.value = submitted
      ? `${submitted} 个 PDF 已提交；尚不表示解析成功。质量门禁拒绝或任务失败不会登记为可读文献；终态见「任务与历史 → 入库中」。`
      : '没有 PDF 成功提交解析，请查看各文件的失败或跳过原因。'
    if (submitted) emit('imported')
  })
}
const readMarkdown = (event) => {
  const file = event.target.files?.[0]
  event.target.value = ''
  return perform(async () => {
    if (!file) return
    if (file.size > 5 * 1024 * 1024) throw new Error('Markdown 不得超过 5 MiB')
    markdown.value = await file.text()
    if (!title.value) title.value = file.name.replace(/\.(md|markdown)$/i, '')
  })
}
const importMarkdown = () =>
  perform(async () => {
    if (new Blob([markdown.value]).size > 5 * 1024 * 1024)
      throw new Error('Markdown 不得超过 5 MiB')
    const result = await literatureWorkspaceApi.importMarkdown({
      title: title.value,
      doi: doi.value,
      markdown: markdown.value
    })
    notice.value = `Markdown 已导入：${result.rid || ''}`
    emit('imported')
  })
const referenceFormat = ref('auto')
const referenceReport = ref(null)
const importBib = (event) => {
  const file = event.target.files?.[0]
  event.target.value = ''
  return perform(async () => {
    if (!file) return
    referenceReport.value = null
    if (file.size > 64 * 1024 * 1024) throw new Error('清单文件不得超过 64 MiB')
    // .bib/.txt 之外的扩展名也交给后端：嗅探按内容特征判定，扩展名不可信
    const result = await literatureWorkspaceApi.importReferences(file, referenceFormat.value)
    referenceReport.value = result
    notice.value = `新增 ${result.papers_written || 0}，跳过 ${result.papers_skipped || 0}，引用边 ${result.citations_written || 0}`
    emit('imported')
  })
}
onMounted(() => {
  if (userStore.isAdmin) loadDatabases()
})
</script>

<style lang="less" scoped>
.import-panel {
  max-width: 960px;
  line-height: 1.7;
}
/* 提交即回执：就地阶段芯片（失败描红、完成置绿、进行中主色） */
.ticket-chip {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border: 1px solid var(--main-color, #2f6b4f);
  border-radius: 10px;
  font-size: 12px;
  color: var(--main-color, #2f6b4f);
}
.ticket-chip.ticket-failure {
  border-color: var(--color-error-700, #d03029);
  color: var(--color-error-700, #d03029);
}
.ticket-chip.ticket-done {
  border-color: #3a8f5f;
  color: #2f7a4f;
}
.import-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.format-guide {
  margin: 10px 0;
  font-size: 13px;
  color: var(--color-text-secondary, #666);
}
.format-guide ul {
  margin: 6px 0 0;
  padding-left: 20px;
}
.reference-report {
  padding: 8px 10px;
  border-left: 3px solid var(--main-color, #2f6b4f);
  background: var(--color-bg-container, #fafafa);
}
label {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin: 10px 0;
}
textarea {
  width: 100%;
  box-sizing: border-box;
}
input,
select,
textarea {
  color: var(--color-text-primary);
  background: var(--color-bg-container);
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  padding: 7px;
}
</style>
