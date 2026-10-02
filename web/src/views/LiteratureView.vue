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

    <div class="lit-toolbar">
      <input
        v-model="searchQuery"
        class="search-input"
        placeholder="检索标题 / DOI（支持 kind:thesis 语法）"
        @keyup.enter="runSearch"
      />
      <button class="btn" @click="runSearch">搜索</button>
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

    <div v-if="viewMode === 'reports'" class="concepts-panel">
      <div v-if="!reports.length" class="lit-empty">暂无综述或主题导读</div>
      <ul v-else class="concept-list">
        <li v-for="report in reports" :key="report.id" class="concept-card">
          <div class="concept-head">
            <strong>{{ report.title }}</strong>
            <span class="concept-count">{{ report.report_kind }}</span>
          </div>
          <div class="concept-def">{{ report.created_at || '' }}</div>
        </li>
      </ul>
    </div>

    <div v-if="viewMode === 'tasks'" class="concepts-panel">
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
          <div class="concept-papers">{{ concept.papers.join(' · ') }}</div>
        </li>
      </ul>
    </div>

    <div v-if="viewMode === 'list' && loading" class="lit-loading">加载中…</div>
    <div v-else-if="viewMode === 'list' && !papers.length" class="lit-empty">
      暂无可访问文献。可在知识库中上传 PDF，完成证据处理后自动进入文献库；也可导入 Bib 或迁移
      PaperAgent。
      <router-link v-if="userStore.isAdmin" to="/extensions?tab=knowledge">前往知识库</router-link>
    </div>

    <ul v-else-if="viewMode === 'list'" class="paper-list">
      <li v-for="paper in papers" :key="paper.rid" class="paper-card" @click="openPaper(paper)">
        <div class="paper-title">
          <span class="kind-badge" :data-kind="paper.kind">{{ kindLabel(paper.kind) }}</span>
          {{ paper.title || paper.rid }}
        </div>
        <div class="paper-meta">
          <span v-if="paper.doi">DOI: {{ paper.doi }}</span>
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

    <PaperDetail v-if="selectedRid" :rid="selectedRid" @close="selectedRid = ''" />
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
import { listLiteraturePapers, listLiteratureTasks } from '@/apis/literature_api'
import PaperDetail from '@/components/literature/PaperDetail.vue'
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
  components: { PaperDetail },
  setup() {
    const userStore = useUserStore()
    const papers = ref([])
    const total = ref(0)
    const page = ref(1)
    const pageSize = 20
    const loading = ref(false)
    const searchQuery = ref('')
    const currentKind = ref('')
    const selectedRid = ref('')
    const viewMode = ref('list')
    const concepts = ref([])
    const mocTopic = ref('')
    const mocLoading = ref(false)
    const reports = ref([])
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
      literatureTasks.value = await listLiteratureTasks()
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
        const summary = `将迁移 ${report.papers?.source || 0} 篇文献、${report.variants?.source || 0} 个变体；冲突 ${report.conflicts?.length || 0} 项。`
        if (!window.confirm(`${summary}\n确认执行正式导入？`)) return
        const importForm = new FormData()
        importForm.append('file', file)
        await apiPost('/api/literature/papers/import/paperagent?dry_run=false', importForm)
        alert('PaperAgent 数据迁移完成')
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

    const load = async () => {
      loading.value = true
      try {
        const parsed = parseSearchSyntax(searchQuery.value)
        const data = await listLiteraturePapers({
          query: parsed.query,
          kind: parsed.kind,
          page: page.value,
          page_size: pageSize
        })
        papers.value = data.items || []
        total.value = data.total || 0
      } catch {
        papers.value = []
        total.value = 0
      } finally {
        loading.value = false
      }
    }

    const runSearch = () => {
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
    }
    const kindLabel = (kind) => KIND_LABELS[kind] || kind

    onMounted(load)

    return {
      userStore,
      papers,
      total,
      page,
      pageSize,
      totalPages,
      loading,
      searchQuery,
      currentKind,
      selectedRid,
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
  border: 1px solid var(--border-color, #ddd);
  border-radius: 12px;
  background: transparent;
  padding: 2px 12px;
  cursor: pointer;
  &.active {
    background: var(--primary-color, #1677ff);
    color: #fff;
  }
}
.lit-toolbar {
  display: flex;
  gap: 8px;
  margin: 12px 0;
}
.search-input {
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
  border: 1px solid var(--border-color, #eee);
  border-radius: 8px;
  padding: 10px 14px;
  cursor: pointer;
  &:hover {
    border-color: var(--primary-color, #1677ff);
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
  background: rgba(22, 119, 255, 0.1);
}
.paper-meta {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.75;
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
  border: 1px solid var(--border-color, #eee);
  border-radius: 8px;
  padding: 10px 14px;
  &.mature {
    border-color: rgba(82, 196, 26, 0.5);
  }
}
.concept-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.concept-count {
  font-size: 12px;
  opacity: 0.7;
}
.concept-def {
  margin: 6px 0;
  font-size: 13px;
}
.concept-papers {
  font-size: 11px;
  font-family: monospace;
  opacity: 0.65;
  word-break: break-all;
}
.lit-empty,
.lit-loading {
  padding: 32px 0;
  opacity: 0.6;
}
.task-error {
  margin-top: 6px;
  color: #cf1322;
  font-size: 12px;
}
</style>
