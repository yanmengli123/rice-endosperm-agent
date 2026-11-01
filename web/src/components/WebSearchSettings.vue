<template>
  <section class="web-settings">
    <h3>网络搜索</h3>
    <p>为智能体的网络搜索和网页正文工具选择供应商。密钥保存后隐藏；搜索结果作为外部来源展示。</p>
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <a-spin :spinning="loading"><a-form layout="vertical">
      <a-form-item label="默认搜索供应商"><a-select v-model:value="search" :options="choices(false)" /></a-form-item>
      <a-form-item label="默认正文供应商"><a-select v-model:value="fetch" :options="choices(true)" /></a-form-item>
      <article v-for="provider in providers" :key="provider.id">
        <h4>{{ provider.name }} <a-tag>{{ provider.configured ? '已配置' : '未配置' }}</a-tag></h4>
        <p>{{ provider.supports_fetch ? '支持搜索与网页正文' : '仅支持搜索' }} · {{ provider.source === 'environment' ? '环境配置' : '已保存配置' }}</p>
        <a-input-password v-model:value="keys[provider.id]" autocomplete="new-password" :placeholder="provider.configured ? '留空保持原密钥，输入可替换' : '输入 API Key'" />
        <a-space class="actions">
          <a-button :loading="testing === provider.id" :disabled="!!testing" @click="test(provider, 'search')">测试搜索</a-button>
          <a-button v-if="provider.supports_fetch" :loading="testing === provider.id" :disabled="!!testing" @click="test(provider, 'fetch')">测试正文</a-button>
          <a-popconfirm title="清空后该供应商不再使用环境密钥，确认清空？" @confirm="clear(provider)"><a-button danger :disabled="loading">清空密钥</a-button></a-popconfirm>
        </a-space>
        <p v-if="results[provider.id]" role="status">{{ results[provider.id] }}</p>
      </article>
      <a-button type="primary" :loading="loading" @click="save">保存网络搜索配置</a-button>
    </a-form></a-spin>
  </section>
</template>
<script setup>
import { onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { webToolsApi } from '@/apis/web_tools_api'
const providers = ref([]), search = ref(''), fetch = ref(''), loading = ref(false), error = ref(''), testing = ref('')
const keys = reactive({}), results = reactive({})
const choices = (fetchOnly) => [{ value: '', label: '自动选择已配置供应商' }, ...providers.value.filter(p => !fetchOnly || p.supports_fetch).map(p => ({ value: p.id, label: p.name }))]
function apply(value) { providers.value = value.providers; search.value = value.search_provider || ''; fetch.value = value.fetch_provider || ''; for (const p of providers.value) keys[p.id] = '' }
async function load() { loading.value = true; try { apply(await webToolsApi.get()) } catch (e) { error.value = e.message } finally { loading.value = false } }
async function save() {
  loading.value = true; error.value = ''
  try {
    apply(await webToolsApi.save({ search_provider: search.value, fetch_provider: fetch.value, credentials: Object.fromEntries(Object.entries(keys).filter(([, value]) => value.trim())) }))
    message.success('网络搜索配置已保存')
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
async function clear(provider) {
  loading.value = true; error.value = ''
  try { apply(await webToolsApi.save({ search_provider: search.value, fetch_provider: fetch.value, credentials: { [provider.id]: '' } })); message.success('密钥已清空') }
  catch (e) { error.value = e.message } finally { loading.value = false }
}
async function test(provider, operation) {
  testing.value = provider.id; results[provider.id] = ''
  try {
    const value = await webToolsApi.test({ provider: provider.id, operation, ...(keys[provider.id]?.trim() ? { api_key: keys[provider.id].trim() } : {}) })
    results[provider.id] = `${operation === 'search' ? '搜索' : '正文'}测试通过，用时 ${value.response_time.toFixed(2)} 秒`
  } catch (e) { results[provider.id] = e.message || '测试失败' } finally { testing.value = '' }
}
onMounted(load)
</script>
<style scoped lang="less">
.web-settings { padding: 16px; max-width: 760px; p { color: var(--color-text-secondary); font-size: 12px; } article { border-top: 1px solid var(--gray-150); padding: 16px 0; } .actions { margin-top: 12px; flex-wrap: wrap; } }
</style>
