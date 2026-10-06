<template>
  <section class="metadata-panel" aria-label="人工元数据编辑">
    <p>人工修改会使旧编译笔记失效。勾选锁定后，自动补全不会覆盖这些字段。</p>
    <form v-if="draft" @submit.prevent="save">
      <fieldset :disabled="busy">
        <label>标题<input v-model="draft.title" maxlength="4096" /></label>
        <label
          >类型<select v-model="draft.kind">
            <option v-for="(name, key) in kinds" :key="key" :value="key">{{ name }}</option>
          </select></label
        >
        <label class="wide"
          >DOI<input
            v-model="draft.doi"
            maxlength="256"
            placeholder="10.NNNN/…（留空表示无；保存后可用于外部源补全）"
            inputmode="url"
        /></label>
        <label>作者（每行一位）<textarea v-model="authors" rows="3" /></label>
        <label>期刊<input v-model="draft.journal" maxlength="512" /></label>
        <label>ISSN<input v-model="draft.issn" maxlength="32" /></label>
        <label>年份<input v-model="draft.pub_year" type="number" min="1000" max="9999" /></label>
        <label>关键词（逗号分隔）<input v-model="keywords" /></label>
        <label>WoS 类别（分号分隔）<input v-model="categories" /></label>
        <label>研究领域（分号分隔）<input v-model="areas" /></label>
        <label class="wide"
          >摘要<textarea v-model="draft.abstract" rows="6" maxlength="50000" />
        </label>
        <label class="wide lock"
          ><input v-model="draft.meta_locked" type="checkbox" />保护人工元数据</label
        >
        <div class="wide actions">
          <button class="btn" :disabled="busy">保存元数据</button>
          <button type="button" class="btn" :disabled="busy" @click="reload">重新载入</button>
        </div>
      </fieldset>
    </form>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'
const props = defineProps({ rid: { type: String, required: true } })
const emit = defineEmits(['saved', 'editing-change'])
const kinds = {
  paper: '论文',
  thesis: '学位论文',
  book: '书籍',
  chapter: '章节',
  patent: '专利',
  standard: '标准'
}
const draft = ref(null)
const authors = ref('')
const keywords = ref('')
const categories = ref('')
const areas = ref('')
const busy = ref(false)
const notice = ref('')
const saved = ref('')
const snapshot = computed(() =>
  JSON.stringify([draft.value, authors.value, keywords.value, categories.value, areas.value])
)
watch([snapshot, busy], () =>
  emit('editing-change', busy.value || (!!draft.value && snapshot.value !== saved.value))
)
let generation = 0
const load = async () => {
  const ticket = ++generation
  draft.value = null
  try {
    const result = await api.metadata(props.rid)
    if (ticket !== generation) return
    draft.value = { ...result }
    authors.value = (result.authors_json || [])
      .map((author) => (typeof author === 'string' ? author : author.name || ''))
      .join('\n')
    keywords.value = (result.keywords_json || []).join(', ')
    categories.value = (result.wos_categories_json || []).join('; ')
    areas.value = (result.research_areas_json || []).join('; ')
    saved.value = snapshot.value
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '元数据载入失败'
  }
}
const reload = () => {
  if (window.confirm('重新载入会丢弃尚未保存的编辑，继续？')) load()
}
const save = async () => {
  if (busy.value || !draft.value) return
  const ticket = generation
  const rid = props.rid
  const value = draft.value
  busy.value = true
  notice.value = ''
  try {
    const result = await api.saveMetadata(rid, {
      expected_token: value.metadata_token,
      changes: {
        doi: (value.doi || '').trim() || null,
        title: value.title || null,
        kind: value.kind,
        journal: value.journal || null,
        issn: value.issn || null,
        abstract: value.abstract || null,
        pub_year: value.pub_year === '' || value.pub_year == null ? null : Number(value.pub_year),
        authors_json: authors.value
          .split('\n')
          .map((x) => x.trim())
          .filter(Boolean),
        keywords_json: keywords.value
          .split(/[,，]/)
          .map((x) => x.trim())
          .filter(Boolean),
        meta_locked: value.meta_locked,
        wos_categories_json: categories.value
          .split(/[;；]/)
          .map((x) => x.trim())
          .filter(Boolean),
        research_areas_json: areas.value
          .split(/[;；]/)
          .map((x) => x.trim())
          .filter(Boolean)
      }
    })
    if (ticket !== generation) return
    draft.value = { ...result }
    saved.value = snapshot.value
    notice.value = '已保存；需要重新编译的笔记保留为历史记录。'
    emit('saved')
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '保存失败；编辑内容仍保留'
  } finally {
    busy.value = false
  }
}
watch(() => props.rid, load, { immediate: true })
onBeforeUnmount(() => {
  generation++
  emit('editing-change', false)
})
</script>

<style lang="less" scoped>
fieldset {
  min-width: 0;
  border: 0;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
input,
textarea,
select {
  color: var(--color-text-primary);
  background: var(--color-bg-container);
  border: 1px solid var(--gray-150);
  padding: 7px;
  border-radius: 6px;
}
.wide {
  grid-column: 1 / -1;
}
.lock,
.actions {
  display: flex;
  flex-direction: row;
  gap: 8px;
}
@media (max-width: 640px) {
  form {
    grid-template-columns: 1fr;
  }
}
</style>
