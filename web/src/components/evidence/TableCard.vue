<template>
  <figure ref="cardRoot" class="table-card" role="group" :aria-label="title">
    <header class="table-card-header">
      <TableIcon :size="14" aria-hidden />
      <span class="table-card-title" :title="title">{{ title }}</span>
      <span class="table-card-meta">
        {{ table.row_count }} 行 × {{ table.col_count }} 列
        <template v-if="table.page"> · 第 {{ table.page }} 页</template>
        <template v-if="truncationNote"> · {{ truncationNote }}</template>
      </span>
      <button
        v-if="canOpenSource"
        type="button"
        class="table-card-source"
        title="在原文中查看该表"
        @click="emit('open-source', { evidence_id: table.evidence_id, table_id: table.table_id })"
      >
        查看原文
      </button>
    </header>

    <!-- 受控渲染：单元格只做文本插值（框架自动转义），rowspan/colspan 为数值绑定；
         服务端已剥离一切标记语言，此处永不使用 v-html -->
    <div class="table-card-scroll">
      <table class="table-card-table">
        <thead v-if="headerRows.length">
          <tr v-for="(row, rowIndex) in headerRows" :key="`h${rowIndex}`">
            <CellComponent
              v-for="(cell, cellIndex) in row"
              :key="`h${rowIndex}-${cellIndex}`"
              :cell="cell"
              tag="th"
            />
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in visibleBodyRows" :key="`b${rowIndex}`">
            <CellComponent
              v-for="(cell, cellIndex) in row"
              :key="`b${rowIndex}-${cellIndex}`"
              :cell="cell"
              tag="td"
            />
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="hasMoreRows" class="table-card-more">
      <button type="button" @click="expanded = !expanded">
        {{ expanded ? '收起' : `展开全部 ${table.row_count} 行` }}
      </button>
    </div>

    <figcaption v-if="table.caption && table.caption !== title" class="table-card-caption">
      {{ table.caption }}
    </figcaption>
  </figure>
</template>

<script setup>
import { computed, ref } from 'vue'
import { Table as TableIcon } from '@lucide/vue'
import CellComponent from './TableCell.vue'

const COLLAPSED_ROW_LIMIT = 12

const props = defineProps({
  // 表格卡片（citation_ready.tables[]，后端受控解析的行列 JSON；数据永不包含 HTML）
  table: {
    type: Object,
    required: true
  },
  // 可跳原文：证据集中存在同 evidence_id（与图卡同款守卫，不绕开证据通道）
  canOpenSource: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['open-source'])

const expanded = ref(false)
const cardRoot = ref(null)

defineExpose({ cardRoot })

const title = computed(() => {
  const label = String(props.table?.label || '').trim()
  if (label) return label
  const page = Number(props.table?.page)
  return Number.isInteger(page) && page >= 1 ? `表 · 第${page}页` : '表格'
})

const truncationNote = computed(() => {
  // 截断必须可见：规模护栏截断与跨页延续块截断给出不同文案（后端 limited 分流）
  if (props.table?.limited) return '已按规模截断'
  return props.table?.truncated ? '跨页已截取' : ''
})

const safeRows = computed(() => {
  const rows = Array.isArray(props.table?.rows) ? props.table.rows.slice(0, 200) : []
  return rows.map((row) => (Array.isArray(row) ? row.slice(0, 60) : []))
})

const headerRows = computed(() => {
  const rows = safeRows.value
  const headerCount = Math.max(0, Math.min(Number(props.table?.header_rows) || 0, rows.length))
  return headerCount > 0 ? rows.slice(0, headerCount) : []
})

const bodyRows = computed(() => {
  const rows = safeRows.value
  const headerCount = Math.max(0, Math.min(Number(props.table?.header_rows) || 0, rows.length))
  return headerCount > 0 ? rows.slice(headerCount) : rows
})

const visibleBodyRows = computed(() =>
  expanded.value ? bodyRows.value : bodyRows.value.slice(0, COLLAPSED_ROW_LIMIT)
)

const hasMoreRows = computed(() => bodyRows.value.length > COLLAPSED_ROW_LIMIT)
</script>

<style lang="less" scoped>
.table-card {
  margin: 0;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: var(--gray-0);
  overflow: hidden;
  transition:
    box-shadow 0.3s ease,
    border-radius 0.3s ease;
}

.table-card-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-100);
  color: var(--gray-700);
  font-size: 13px;
}

.table-card-title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.table-card-meta {
  color: var(--gray-400);
  font-size: 12px;
  white-space: nowrap;
}

.table-card-source {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--main-700);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;

  &:hover {
    text-decoration: underline;
  }
}

.table-card-scroll {
  overflow-x: auto;
  max-height: 420px;
  overflow-y: auto;
}

.table-card-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 12.5px;
  line-height: 1.5;

  :deep(th),
  :deep(td) {
    border: 1px solid var(--gray-100);
    padding: 4px 10px;
    text-align: left;
    vertical-align: top;
    white-space: nowrap;
  }

  :deep(th) {
    background: var(--gray-25);
    color: var(--gray-800);
    font-weight: 600;
  }
}

.table-card-more {
  text-align: center;
  padding: 4px;

  button {
    border: none;
    background: transparent;
    color: var(--main-700);
    font-size: 12px;
    cursor: pointer;
    padding: 4px 12px;

    &:hover {
      text-decoration: underline;
    }
  }
}

.table-card-caption {
  padding: 6px 12px;
  color: var(--gray-500);
  font-size: 12px;
  border-top: 1px solid var(--gray-100);
}
</style>
