<template>
  <section id="rice-databases" class="database-section" aria-labelledby="database-title">
    <div class="database-heading">
      <div class="database-heading-copy">
        <p class="section-kicker">
          <Database :size="16" aria-hidden="true" />
          Research Data Gateway
        </p>
        <h2 id="database-title">权威水稻数据库导航</h2>
        <p class="section-description">
          从基因注释、表达调控到群体变异与种质资源，集中访问水稻研究常用的官方平台和论文团队资源。
        </p>
      </div>

      <div class="database-overview" aria-label="数据库目录概况">
        <strong>{{ riceDatabases.length }}</strong>
        <span>个精选入口</span>
        <small>
          核验日期
          <time :datetime="RICE_DATABASE_LAST_VERIFIED">{{ verifiedDateLabel }}</time>
        </small>
      </div>
    </div>

    <div class="trust-note">
      <ShieldCheck :size="18" aria-hidden="true" />
      <p>
        优先展示机构官方入口；论文团队资源和历史归档均单独标识。链接将在新标签页打开，本站不存储目标数据库内容。
      </p>
    </div>

    <div class="database-tools">
      <label class="database-search" for="rice-database-search">
        <Search :size="18" aria-hidden="true" />
        <span class="visually-hidden">搜索水稻数据库</span>
        <input
          id="rice-database-search"
          v-model.trim="query"
          type="search"
          autocomplete="off"
          placeholder="搜索数据库名称、机构或用途，例如：胚乳、GWAS、表达谱"
        />
      </label>

      <div class="category-filters" role="group" aria-label="按研究方向筛选数据库">
        <button
          v-for="category in riceDatabaseCategories"
          :key="category.id"
          type="button"
          :class="['category-button', { 'is-active': activeCategory === category.id }]"
          :aria-pressed="activeCategory === category.id"
          @click="activeCategory = category.id"
        >
          {{ category.label }}
          <span>{{ categoryCounts[category.id] }}</span>
        </button>
      </div>
    </div>

    <div class="result-bar">
      <p aria-live="polite">
        {{ resultSummary }}
      </p>
      <div class="status-legend" aria-label="入口状态说明">
        <span v-for="item in statusLegend" :key="item.id" :class="`is-${item.id}`">
          <i aria-hidden="true"></i>{{ item.label }}
        </span>
      </div>
    </div>

    <div v-if="visibleDatabases.length" class="database-grid">
      <a
        v-for="database in visibleDatabases"
        :key="database.id"
        class="database-card"
        :href="database.url"
        target="_blank"
        rel="noopener noreferrer"
        :aria-label="`${database.shortName}：${database.description}（在新标签页打开）`"
      >
        <div class="card-topline">
          <span class="resource-mark" aria-hidden="true">{{ getResourceMark(database.shortName) }}</span>
          <span :class="['status-badge', `is-${database.status}`]">
            {{ statusDetails[database.status].label }}
          </span>
        </div>

        <div class="card-title-row">
          <div>
            <h3>{{ database.shortName }}</h3>
            <p>{{ database.name }}</p>
          </div>
          <ExternalLink :size="17" aria-hidden="true" />
        </div>

        <p class="institution">{{ database.institution }}</p>
        <p class="card-description">{{ database.description }}</p>

        <ul class="keyword-list" aria-label="适用范围">
          <li v-for="keyword in database.keywords.slice(0, 3)" :key="keyword">{{ keyword }}</li>
        </ul>
      </a>
    </div>

    <div v-else class="empty-state" role="status">
      <SearchX :size="24" aria-hidden="true" />
      <strong>没有找到匹配的数据库</strong>
      <p>请尝试更短的名称、机构名或研究用途。</p>
      <button type="button" @click="resetFilters">清除筛选</button>
    </div>

    <button
      v-if="canToggleAll"
      type="button"
      class="expand-button"
      :aria-expanded="isExpanded"
      @click="isExpanded = !isExpanded"
    >
      {{ isExpanded ? '收起数据库目录' : `查看全部 ${riceDatabases.length} 个数据库` }}
      <ChevronUp v-if="isExpanded" :size="17" aria-hidden="true" />
      <ChevronDown v-else :size="17" aria-hidden="true" />
    </button>

    <p class="database-disclaimer">
      外部数据库的服务状态、使用条款和数据许可由对应维护机构负责；正式分析前请核对版本、物种、参考基因组和引用要求。
    </p>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import {
  ChevronDown,
  ChevronUp,
  Database,
  ExternalLink,
  Search,
  SearchX,
  ShieldCheck
} from 'lucide-vue-next'
import {
  RICE_DATABASE_LAST_VERIFIED,
  riceDatabaseCategories,
  riceDatabases
} from '@/data/riceDatabases'

const INITIAL_VISIBLE_COUNT = 12

const statusDetails = {
  official: { label: '官方入口' },
  'official-data': { label: '官方数据页' },
  research: { label: '研究资源' },
  archive: { label: '官方归档' }
}

const statusLegend = Object.entries(statusDetails).map(([id, value]) => ({ id, ...value }))

const query = ref('')
const activeCategory = ref('all')
const isExpanded = ref(false)

const verifiedDateLabel = computed(() => RICE_DATABASE_LAST_VERIFIED.replaceAll('-', '.'))

const categoryCounts = computed(() => {
  const counts = Object.fromEntries(riceDatabaseCategories.map((category) => [category.id, 0]))
  counts.all = riceDatabases.length

  for (const database of riceDatabases) {
    counts[database.category] += 1
  }

  return counts
})

const filteredDatabases = computed(() => {
  const normalizedQuery = query.value.toLocaleLowerCase('zh-CN')

  return riceDatabases.filter((database) => {
    const matchesCategory =
      activeCategory.value === 'all' || database.category === activeCategory.value
    if (!matchesCategory) return false
    if (!normalizedQuery) return true

    const searchableText = [
      database.name,
      database.shortName,
      database.institution,
      database.description,
      ...database.keywords
    ]
      .join(' ')
      .toLocaleLowerCase('zh-CN')

    return searchableText.includes(normalizedQuery)
  })
})

const hasFocusedFilter = computed(() => Boolean(query.value) || activeCategory.value !== 'all')

const visibleDatabases = computed(() => {
  if (hasFocusedFilter.value || isExpanded.value) return filteredDatabases.value
  return filteredDatabases.value.slice(0, INITIAL_VISIBLE_COUNT)
})

const canToggleAll = computed(
  () => !hasFocusedFilter.value && riceDatabases.length > INITIAL_VISIBLE_COUNT
)

const resultSummary = computed(() => {
  if (hasFocusedFilter.value) return `找到 ${filteredDatabases.value.length} 个匹配入口`
  if (!isExpanded.value) return `优先展示 ${visibleDatabases.value.length} 个常用入口`
  return `已展示全部 ${filteredDatabases.value.length} 个入口`
})

const getResourceMark = (shortName) =>
  shortName
    .split(/\s+/)
    .map((part) => part[0])
    .join('')
    .slice(0, 3)
    .toUpperCase()

const resetFilters = () => {
  query.value = ''
  activeCategory.value = 'all'
}
</script>

<style lang="less" scoped>
.database-section {
  width: min(1180px, calc(100% - 40px));
  margin: 0 auto;
  padding: 72px 0;
  border-top: 1px solid var(--rice-border);
}

.database-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 40px;
}

.database-heading-copy {
  max-width: 760px;

  h2 {
    margin: 0;
    color: var(--rice-text);
    font-size: clamp(26px, 3vw, 36px);
    font-weight: 680;
    line-height: 1.3;
  }
}

.section-kicker {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 9px;
  color: var(--main-700);
  font-size: 12px;
  font-weight: 650;
  letter-spacing: 0.075em;
  text-transform: uppercase;
}

.section-description {
  max-width: 700px;
  margin: 14px 0 0;
  color: var(--rice-text-secondary);
  font-size: 15px;
  line-height: 1.75;
}

.database-overview {
  display: grid;
  grid-template-columns: auto 1fr;
  flex: 0 0 auto;
  padding: 15px 18px;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 12px;

  strong {
    grid-row: span 2;
    margin-right: 10px;
    color: var(--main-700);
    font-size: 32px;
    font-weight: 700;
    line-height: 1;
  }

  span {
    color: var(--rice-text);
    font-size: 13px;
    font-weight: 650;
  }

  small {
    margin-top: 4px;
    color: var(--rice-text-secondary);
    font-size: 11px;
  }
}

.trust-note {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 26px;
  padding: 13px 15px;
  color: var(--main-800);
  background: var(--main-50);
  border: 1px solid var(--main-100);
  border-radius: 10px;

  svg {
    flex: 0 0 auto;
    margin-top: 1px;
  }

  p {
    margin: 0;
    font-size: 12px;
    line-height: 1.65;
  }
}

.database-tools {
  margin-top: 24px;
}

.database-search {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 48px;
  padding: 0 15px;
  color: var(--rice-text-secondary);
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 10px;
  transition: border-color 0.18s ease;

  &:focus-within {
    border-color: var(--main-400);
    outline: 3px solid color-mix(in srgb, var(--main-color) 18%, transparent);
  }

  svg {
    flex: 0 0 auto;
  }

  input {
    width: 100%;
    min-width: 0;
    padding: 12px 0;
    color: var(--rice-text);
    font: inherit;
    background: transparent;
    border: 0;
    outline: 0;

    &::placeholder {
      color: var(--rice-text-secondary);
    }

    &::-webkit-search-cancel-button {
      cursor: pointer;
    }
  }
}

.category-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.category-button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 34px;
  padding: 6px 11px;
  color: var(--rice-text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 999px;

  span {
    min-width: 20px;
    padding: 1px 5px;
    color: inherit;
    font-size: 10px;
    text-align: center;
    background: color-mix(in srgb, currentColor 8%, transparent);
    border-radius: 999px;
  }

  &:hover {
    color: var(--main-700);
    border-color: var(--main-200);
  }

  &:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--main-color) 24%, transparent);
    outline-offset: 2px;
  }

  &.is-active {
    color: var(--main-800);
    font-weight: 650;
    background: var(--main-50);
    border-color: var(--main-200);
  }
}

.result-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  min-height: 24px;
  margin: 20px 0 13px;

  > p {
    margin: 0;
    color: var(--rice-text-secondary);
    font-size: 12px;
  }
}

.status-legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 12px;
  color: var(--rice-text-secondary);
  font-size: 10px;

  span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  i {
    width: 7px;
    height: 7px;
    background: currentColor;
    border-radius: 50%;
  }

  .is-official {
    color: var(--color-success-700);
  }

  .is-official-data {
    color: var(--color-info-700);
  }

  .is-research {
    color: var(--main-700);
  }

  .is-archive {
    color: var(--color-warning-700);
  }
}

.database-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

.database-card {
  display: flex;
  min-width: 0;
  min-height: 278px;
  flex-direction: column;
  padding: 20px;
  color: inherit;
  text-decoration: none;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 12px;
  transition:
    background-color 0.18s ease,
    border-color 0.18s ease;

  &:hover {
    background: var(--main-10);
    border-color: var(--main-200);

    .card-title-row > svg {
      color: var(--main-700);
    }
  }

  &:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--main-color) 25%, transparent);
    outline-offset: 2px;
  }
}

.card-topline,
.card-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.resource-mark {
  display: inline-flex;
  min-width: 40px;
  height: 34px;
  align-items: center;
  justify-content: center;
  padding: 0 8px;
  color: var(--main-800);
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.02em;
  background: var(--main-50);
  border: 1px solid var(--main-100);
  border-radius: 8px;
}

.status-badge {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 650;
  border: 1px solid transparent;
  border-radius: 999px;

  &.is-official {
    color: var(--color-success-700);
    background: var(--color-success-10);
    border-color: var(--color-success-100);
  }

  &.is-official-data {
    color: var(--color-info-700);
    background: var(--color-info-10);
    border-color: var(--color-info-100);
  }

  &.is-research {
    color: var(--main-700);
    background: var(--main-50);
    border-color: var(--main-100);
  }

  &.is-archive {
    color: var(--color-warning-700);
    background: var(--color-warning-10);
    border-color: var(--color-warning-100);
  }
}

.card-title-row {
  margin-top: 17px;

  > div {
    min-width: 0;
  }

  h3 {
    margin: 0;
    color: var(--rice-text);
    font-size: 17px;
    font-weight: 680;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  p {
    margin: 4px 0 0;
    color: var(--rice-text-secondary);
    font-size: 10px;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  > svg {
    flex: 0 0 auto;
    margin-top: 2px;
    color: var(--rice-text-secondary);
  }
}

.institution {
  margin: 13px 0 0;
  color: var(--main-700);
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
}

.card-description {
  margin: 8px 0 16px;
  color: var(--rice-text-secondary);
  font-size: 13px;
  line-height: 1.65;
}

.keyword-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: auto 0 0;
  padding: 0;
  list-style: none;

  li {
    padding: 3px 7px;
    color: var(--rice-text-secondary);
    font-size: 10px;
    background: var(--rice-page-bg);
    border: 1px solid var(--rice-border);
    border-radius: 5px;
  }
}

.empty-state {
  padding: 52px 20px;
  color: var(--rice-text-secondary);
  text-align: center;
  background: var(--rice-surface);
  border: 1px dashed var(--rice-border);
  border-radius: 12px;

  svg {
    color: var(--main-600);
  }

  strong {
    display: block;
    margin-top: 10px;
    color: var(--rice-text);
    font-size: 15px;
  }

  p {
    margin: 5px 0 14px;
    font-size: 12px;
  }

  button {
    padding: 7px 12px;
    color: var(--main-700);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    background: var(--main-50);
    border: 1px solid var(--main-200);
    border-radius: 7px;
  }
}

.expand-button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  min-width: 220px;
  min-height: 40px;
  margin: 22px auto 0;
  padding: 8px 16px;
  color: var(--main-700);
  font: inherit;
  font-size: 12px;
  font-weight: 650;
  cursor: pointer;
  background: var(--rice-surface);
  border: 1px solid var(--main-200);
  border-radius: 8px;

  &:hover {
    background: var(--main-50);
  }

  &:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--main-color) 24%, transparent);
    outline-offset: 2px;
  }
}

.database-disclaimer {
  margin: 22px 0 0;
  color: var(--rice-text-secondary);
  font-size: 10px;
  line-height: 1.6;
  text-align: center;
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 1000px) {
  .database-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .database-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 22px;
  }

  .database-overview {
    align-self: stretch;
    width: fit-content;
  }

  .result-bar {
    align-items: flex-start;
    flex-direction: column;
    gap: 9px;
  }

  .status-legend {
    justify-content: flex-start;
  }
}

@media (max-width: 640px) {
  .database-section {
    width: min(100% - 32px, 1180px);
    padding: 56px 0;
  }

  .database-search {
    min-height: 46px;
  }

  .database-grid {
    grid-template-columns: 1fr;
  }

  .database-card {
    min-height: 0;
  }

  .status-legend {
    gap: 8px 12px;
  }
}
</style>
