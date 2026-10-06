<template>
  <section class="topics-panel" aria-label="主题管理">
    <h3>主题管理</h3>
    <p>按书目字段确定性分组，不调用模型；主题导读（MOC）另行生成。主题不是原文证据。</p>
    <div class="actions">
      <select v-model="field" :disabled="busy" aria-label="主题分组字段" @change="load">
        <option value="wos_categories">WoS 类别</option>
        <option value="research_areas">研究领域</option>
        <option value="keywords">关键词</option>
      </select>
      <label
        >最少文献数<input
          v-model.number="minPapers"
          type="number"
          min="1"
          max="10000"
          :disabled="busy"
      /></label>
      <button
        class="btn"
        :disabled="busy || !Number.isInteger(minPapers) || minPapers < 1 || minPapers > 10000"
        @click="compile"
      >
        编译主题
      </button>
      <button class="btn" :disabled="busy" @click="load">刷新</button>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="catalog.stale" role="alert">
      书目、计量或可访问来源已变化；旧主题不再展示，请重新编译。
    </p>
    <p v-else-if="!catalog.compiled">该字段尚未编译。</p>
    <p v-else-if="!items.length">无满足阈值的主题。请导入或编辑对应书目字段。</p>
    <ul>
      <li v-for="item in items" :key="item.slug">
        <button class="btn" :disabled="busy" @click="details(item)">
          {{ item.name }} · {{ item.paper_count }} 篇
        </button>
        <span
          >平均被引 {{ Number(item.avg_cited).toFixed(1) }} · 平均 PaperRank
          {{ Number(item.avg_rank).toFixed(4) }}</span
        >
      </li>
    </ul>
    <button v-if="items.length < catalog.total" class="btn" :disabled="busy" @click="loadMore">
      加载更多
    </button>
    <section v-if="selected" aria-label="主题详情">
      <h4>{{ selected.name }}</h4>
      <ul>
        <li v-for="paper in selected.papers" :key="paper.rid">
          <button class="btn" @click="$emit('open-paper', paper)">
            {{ paper.title || paper.rid }}
          </button>
          {{ paper.doi || '无 DOI' }}
        </li>
      </ul>
    </section>
  </section>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'
defineEmits(['open-paper'])
const field = ref('wos_categories'),
  minPapers = ref(2),
  busy = ref(false),
  notice = ref('')
const catalog = ref({ topics: [], total: 0, compiled: false }),
  items = ref([]),
  selected = ref(null)
let generation = 0
const load = async () => {
  const ticket = ++generation
  busy.value = true
  selected.value = null
  items.value = []
  catalog.value = { topics: [], total: 0, compiled: false }
  try {
    const result = await api.topics(field.value)
    if (ticket !== generation) return
    catalog.value = result
    items.value = result.topics
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '主题读取失败'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
const compile = async () => {
  busy.value = true
  notice.value = ''
  const ticket = ++generation
  try {
    const result = await api.compileTopics(field.value, minPapers.value)
    if (ticket !== generation) return
    notice.value = `编译完成：${result.topics} 个主题，覆盖 ${result.papers_covered} 篇文献；未调用模型。`
    await load()
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '主题编译失败'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
const loadMore = async () => {
  const ticket = generation
  busy.value = true
  try {
    const result = await api.topics(field.value, items.value.length)
    if (ticket !== generation) return
    if (result.stale || result.updated_at !== catalog.value.updated_at) {
      await load()
      return
    }
    items.value.push(...result.topics)
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '主题分页失败'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
const details = async (item) => {
  const ticket = generation
  busy.value = true
  selected.value = null
  try {
    const result = await api.topic(item.slug, field.value)
    if (ticket === generation) selected.value = result
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '主题详情读取失败'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  generation++
})
</script>

<style lang="less" scoped>
.topics-panel {
  padding: 16px 24px;
  color: var(--color-text);
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
li {
  margin: 8px 0;
}
input {
  width: 100px;
}
</style>
