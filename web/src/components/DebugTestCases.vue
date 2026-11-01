<template>
  <a-modal v-model:open="open" title="平台能力测试用例库" width="880px" :footer="null">
    <p>选择用例后可复制或带入新会话草稿；点击发送后才执行任务。文件、知识库和扩展能力需要先配置。</p>
    <a-space wrap><a-input v-model:value="query" placeholder="搜索标题、标签或提示词" allow-clear />
      <a-select v-model:value="category" :options="[{ value: '', label: '全部类别' }, ...testCaseCategories.map(item => ({ value: item.key, label: item.label }))]" style="min-width: 150px" />
      <a-select v-model:value="agent" :options="agents.map(item => ({ value: item.id, label: item.name }))" placeholder="选择智能体" style="min-width: 160px" />
    </a-space>
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <p>{{ filtered.length }} / {{ testCases.length }} 个用例</p>
    <div class="case-list"><article v-for="item in filtered" :key="item.id">
      <h4>{{ item.title }} <a-tag>{{ difficultyLabels[item.difficulty] }}</a-tag></h4>
      <p>{{ item.note }}</p><a-tag v-for="tag in item.tags" :key="tag">{{ tag }}</a-tag>
      <details><summary>查看提示词</summary><pre>{{ item.prompt }}</pre></details>
      <a-space><a-button @click="copy(item)">复制提示词</a-button><a-button :loading="busy === item.id" :disabled="!agent || !!busy" @click="use(item)">带入新会话草稿</a-button></a-space>
    </article></div>
  </a-modal>
</template>
<script setup>
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { useUserStore } from '@/stores/user'
import { publicSessionApi } from '@/apis/public_session_api'
import { testCases, testCaseCategories, difficultyLabels } from '@/utils/v08TestCases'
const open = defineModel('open')
const emit = defineEmits(['launched'])
const user = useUserStore(), router = useRouter()
const query = ref(''), category = ref(''), agent = ref(''), agents = ref([]), busy = ref(''), error = ref('')
const filtered = computed(() => testCases.filter(item => (!category.value || item.category === category.value) && `${item.title} ${item.tags.join(' ')} ${item.prompt}`.toLowerCase().includes(query.value.toLowerCase())))
async function copy(item) { try { await navigator.clipboard.writeText(item.prompt); message.success('提示词已复制') } catch { message.error('复制失败，请展开后手动复制') } }
async function use(item) {
  busy.value = item.id; error.value = ''; const uid = user.uid
  try {
    const created = await publicSessionApi.create({ agent_id: agent.value, title: `测试：${item.title}` }, crypto.randomUUID())
    if (user.uid !== uid) return
    localStorage.setItem(`yuxi:session-draft:${uid}:${created.id}`, item.prompt)
    await router.push(`/sessions/${created.id}`)
    open.value = false; emit('launched')
  } catch (e) { error.value = e.message || '创建测试会话失败' } finally { busy.value = '' }
}
watch(open, async (value) => {
  if (!value) return
  error.value = ''; const uid = user.uid
  try { const result = await publicSessionApi.agents(); if (uid === user.uid) { agents.value = result.data; agent.value = result.data[0]?.id || '' } }
  catch (e) { error.value = e.message }
})
</script>
<style scoped lang="less">
.case-list { max-height: 60vh; overflow: auto; article { padding: 14px 0; border-top: 1px solid var(--gray-150); } pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: var(--gray-50); } p { color: var(--color-text-secondary); } details { margin: 12px 0; } }
</style>
