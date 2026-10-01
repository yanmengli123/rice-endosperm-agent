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
    </div>

    <div v-if="loading" class="lit-loading">加载中…</div>
    <div v-else-if="!papers.length" class="lit-empty">文献库为空（可经 PaperAgent 导入器迁移数据）</div>

    <ul v-else class="paper-list">
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

    <div class="lit-pagination" v-if="total > pageSize">
      <button class="btn" :disabled="page <= 1" @click="gotoPage(page - 1)">上一页</button>
      <span>{{ page }} / {{ totalPages }}</span>
      <button class="btn" :disabled="page >= totalPages" @click="gotoPage(page + 1)">下一页</button>
    </div>

    <PaperDetail v-if="selectedRid" :rid="selectedRid" @close="selectedRid = ''" />
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { listLiteraturePapers } from '@/apis/literature_api'
import PaperDetail from '@/components/literature/PaperDetail.vue'

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
    const papers = ref([])
    const total = ref(0)
    const page = ref(1)
    const pageSize = 20
    const loading = ref(false)
    const searchQuery = ref('')
    const currentKind = ref('')
    const selectedRid = ref('')

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
      } catch (err) {
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
      kindLabel
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
.lit-empty,
.lit-loading {
  padding: 32px 0;
  opacity: 0.6;
}
</style>
