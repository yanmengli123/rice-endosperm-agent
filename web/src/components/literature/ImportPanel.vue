<template>
  <section class="import-panel">
    <h3>导入文献</h3>
    <div class="import-tabs" role="tablist" aria-label="导入类型">
      <button
        v-for="(label, key) in tabs"
        :key="key"
        class="btn"
        :aria-selected="mode === key"
        role="tab"
        @click="mode = key"
      >
        {{ label }}
      </button>
    </div>
    <template v-if="mode === 'pdf'">
      <p>
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
            <option value="activate">解析并激活（进入文献库）</option>
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
          同名文件需确认；不会覆盖旧证据版本。此入口只提交知识源处理，编译/完整流程在文献出现后明确启动。
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
        <MarkdownPreview
          v-if="draftDocument"
          :content="draftDocument.markdown"
          :knowledge-base-id="draftDocument.kb_id"
        />
      </section>
    </template>
    <template v-else-if="mode === 'markdown'">
      <p>Markdown 免 OCR 导入为阅读产物，不自动赋予原文证据资格。</p>
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
      <p>BibTeX 保留身份与引用边；重复记录跳过，不覆盖人工元数据。</p>
      <input type="file" accept=".bib,.txt" :disabled="busy" @change="importBib" />
    </template>
    <p v-if="notice" role="status">{{ notice }}</p>
    <HistoryPanel />
    <ol v-if="results.length">
      <li v-for="(result, index) in results" :key="index">
        {{ result.name }}：{{ result.message }}
      </li>
    </ol>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { databaseApi, documentApi, fileApi } from '@/apis/knowledge_api'
import { literatureWorkspaceApi } from '@/apis/literature_api'
import { apiGet, apiPost } from '@/apis/base'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import HistoryPanel from '@/components/literature/HistoryPanel.vue'
import { useUserStore } from '@/stores/user'
const emit = defineEmits(['imported'])
const userStore = useUserStore()
const tabs = { pdf: 'PDF 契约处理', markdown: 'Markdown 直导', bib: 'BibTeX 目录' }
const mode = ref('pdf')
const busy = ref(false)
const databases = ref([])
const kbId = ref('')
const processingMode = ref('activate')
const drafts = ref([])
const draftDocument = ref(null)
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
    for (const file of files) {
      notice.value = `正在提交 ${file.name}（${results.value.length + 1}/${files.length}）`
      try {
        const upload = await fileApi.uploadFile(file, target)
        if (
          upload.has_same_name &&
          !window.confirm(`${file.name} 已有同名记录。确认作为新来源提交？`)
        ) {
          results.value.push({ name: file.name, message: '已跳过；未提交解析' })
          continue
        }
        const result = await documentApi.addDocuments(target, [upload.file_path], {
          content_type: 'file',
          format_template: 'pdf_literature',
          pdf_evidence_pipeline: true,
          scientific_processing_mode: processingMode.value,
          source_paths: { [upload.file_path]: file.name }
        })
        const failed = (result.result?.items || []).find((item) => item.status === 'failed')
        results.value.push({
          name: file.name,
          message: failed
            ? failed.error || '处理接收失败'
            : result.message || '已提交后台 PDF 处理，请在知识源任务查看进度'
        })
      } catch (error) {
        results.value.push({ name: file.name, message: error.message || '提交失败' })
      }
    }
    notice.value = '提交结束；尚不表示解析成功。质量门禁拒绝或任务失败不会登记为可读文献。'
    emit('imported')
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
const importBib = (event) => {
  const file = event.target.files?.[0]
  event.target.value = ''
  return perform(async () => {
    if (!file) return
    if (file.size > 10 * 1024 * 1024) throw new Error('Bib 文件不得超过 10 MiB')
    const form = new FormData()
    form.append('file', file)
    const result = await apiPost('/api/literature/papers/import/bib', form)
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
.import-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
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
