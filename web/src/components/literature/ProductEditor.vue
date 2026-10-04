<template>
  <section class="product-editor">
    <button v-if="!editing" class="sub-tab" :disabled="busy" @click="open">编辑并冻结此产物</button>
    <template v-else>
      <p>人工修订保持为派生产物；自动生成不会覆盖。版本冲突时保留本地草稿，请重新载入比对。</p>
      <textarea v-model="content" :disabled="busy" rows="16" aria-label="人工笔记或译文" />
      <button class="btn" :disabled="busy" @click="save">保存并冻结</button>
      <button class="btn" :disabled="busy" @click="cancel">取消</button>
    </template>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>
<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiGet, apiPut } from '@/apis/base'
const props = defineProps({
  rid: { type: String, required: true },
  kind: { type: String, required: true }
})
const emit = defineEmits(['saved', 'editing-change'])
const editing = ref(false),
  busy = ref(false),
  content = ref(''),
  notice = ref('')
let snapshot = null,
  generation = 0
watch([editing, busy], () => emit('editing-change', editing.value || busy.value))
const open = async () => {
  if (busy.value) return
  const ticket = ++generation,
    rid = props.rid,
    kind = props.kind
  busy.value = true
  try {
    const data = await apiGet(`/api/literature/papers/${encodeURIComponent(rid)}/products/${kind}`)
    if (ticket !== generation) return
    snapshot = Object.fromEntries(
      Object.entries(data).filter(([key]) => key.startsWith('expected_'))
    )
    content.value = data.content
    editing.value = true
    notice.value = data.stale
      ? '来源已更新：这里是历史人工草稿。请与当前正文比对，保存将明确确认它适用于当前版本。'
      : ''
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '无法加载编辑产物'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
const save = async () => {
  if (busy.value || !snapshot) return
  if (
    notice.value.startsWith('来源已更新') &&
    !window.confirm('已核对当前正文，确认将这份人工草稿用于当前来源版本？')
  )
    return
  const ticket = generation,
    rid = props.rid,
    kind = props.kind
  busy.value = true
  try {
    await apiPut(`/api/literature/papers/${encodeURIComponent(rid)}/products/${kind}`, {
      content: content.value,
      ...snapshot
    })
    if (ticket !== generation) return
    editing.value = false
    notice.value = '人工产物已冻结。'
    emit('saved')
  } catch (error) {
    if (ticket === generation) notice.value = error.message || '保存失败，草稿未清除'
  } finally {
    if (ticket === generation) busy.value = false
  }
}
const cancel = () => {
  if (window.confirm('放弃尚未保存的编辑？')) editing.value = false
}
watch(
  () => [props.rid, props.kind],
  () => {
    ++generation
    editing.value = busy.value = false
    content.value = notice.value = ''
    snapshot = null
  }
)
onBeforeUnmount(() => {
  ++generation
  emit('editing-change', false)
})
</script>
<style scoped>
textarea {
  width: 100%;
  box-sizing: border-box;
  background: var(--color-bg-container);
  color: var(--color-text-primary);
}
</style>
