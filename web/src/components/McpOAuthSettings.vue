<template>
  <section>
    <h2>个人 MCP 连接</h2>
    <p>授权只供当前账户使用。撤销后，后续工具调用将停止使用这份授权。</p>
    <a-button :loading="loading" @click="loadConnections">刷新状态</a-button>
    <a-empty v-if="!loading && !connections.length" description="管理员尚未启用个人 OAuth 服务" />
    <div v-for="item in connections" :key="item.slug" class="connection">
      <div>
        <strong>{{ item.name }}</strong>
        <p>{{ statusLabel(item.status) }}</p>
      </div>
      <a-space>
        <a-button :loading="busy === item.slug" @click="authorize(item.slug)">授权连接</a-button>
        <a-popconfirm title="撤销当前账户的授权？" @confirm="disconnect(item.slug)">
          <a-button danger :disabled="busy === item.slug">撤销</a-button>
        </a-popconfirm>
      </a-space>
    </div>

    <template v-if="userStore.isAdmin">
      <a-divider />
      <h3>配置租户 OAuth 服务</h3>
      <p>使用服务商已注册的客户端。保存配置后，用户需要重新授权。</p>
      <a-select
        v-model:value="selected"
        class="server-select"
        placeholder="选择远程 MCP 服务"
        @change="loadConfig"
      >
        <a-select-option v-for="item in servers" :key="item.slug" :value="item.slug">{{
          item.name
        }}</a-select-option>
      </a-select>
      <a-form v-if="selected" layout="vertical" @finish="saveConfig">
        <a-form-item label="回调地址">
          <a-typography-text copyable v-if="redirectUri">{{ redirectUri }}</a-typography-text>
          <a-alert
            v-else
            type="info"
            message="部署时先设置 YUXI_PUBLIC_URL，再将此回调地址登记到服务商。"
          />
        </a-form-item>
        <a-form-item label="启用个人 OAuth"
          ><a-switch v-model:checked="form.enabled"
        /></a-form-item>
        <a-form-item label="授权端点"
          ><a-input
            v-model:value="form.authorization_endpoint"
            placeholder="https://provider.example/authorize"
            required
        /></a-form-item>
        <a-form-item label="令牌端点"
          ><a-input
            v-model:value="form.token_endpoint"
            placeholder="https://provider.example/token"
            required
        /></a-form-item>
        <a-form-item label="MCP resource URL"
          ><a-input v-model:value="form.resource" required
        /></a-form-item>
        <a-form-item label="Client ID"
          ><a-input v-model:value="form.client_id" required
        /></a-form-item>
        <a-form-item label="客户端认证方式">
          <a-select v-model:value="form.token_endpoint_auth_method" :options="authMethods" />
        </a-form-item>
        <a-form-item label="Client Secret">
          <a-input-password
            v-model:value="clientSecret"
            autocomplete="new-password"
            :placeholder="hasSecret ? '已保存；留空保持' : '公共客户端可留空'"
          />
        </a-form-item>
        <a-form-item label="Scope"><a-input v-model:value="form.scope" /></a-form-item>
        <a-button type="primary" html-type="submit" :loading="saving">保存服务配置</a-button>
      </a-form>
    </template>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { apiGet, apiPost, apiDelete, apiAdminGet, apiAdminPut } from '@/apis/base'
import { getMcpServers } from '@/apis/mcp_api'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const base = '/api/system/mcp-oauth'
const loading = ref(false),
  saving = ref(false),
  busy = ref('')
const connections = ref([]),
  servers = ref([]),
  selected = ref(''),
  redirectUri = ref('')
const hasSecret = ref(false),
  clientSecret = ref('')
const defaults = () => ({
  enabled: true,
  authorization_endpoint: '',
  token_endpoint: '',
  resource: '',
  client_id: '',
  scope: '',
  token_endpoint_auth_method: 'none'
})
const form = reactive(defaults())
const authMethods = ['none', 'client_secret_post', 'client_secret_basic'].map((value) => ({
  value,
  label: value
}))
const statusLabel = (status) =>
  ({
    connected: '已连接',
    refresh_required: '令牌到期；下次调用时尝试刷新',
    pending: '等待授权',
    revoked: '已撤销',
    disconnected: '尚未授权'
  })[status] || status

async function loadConnections() {
  loading.value = true
  try {
    connections.value = (await apiGet(`${base}/connections`)).data || []
  } catch (error) {
    message.error(error.message || '读取连接失败')
  } finally {
    loading.value = false
  }
}
async function authorize(slug) {
  const popup = window.open('about:blank', '_blank')
  if (!popup) {
    message.warning('请允许授权窗口弹出')
    return
  }
  popup.opener = null
  busy.value = slug
  try {
    const result = await apiPost(`${base}/connections/${encodeURIComponent(slug)}/authorize`)
    popup.location.href = result.authorization_url
  } catch (error) {
    popup.close()
    message.error(error.message || '授权失败')
  } finally {
    busy.value = ''
  }
}
async function disconnect(slug) {
  busy.value = slug
  try {
    await apiDelete(`${base}/connections/${encodeURIComponent(slug)}`)
    await loadConnections()
  } catch (error) {
    message.error(error.message || '撤销失败')
  } finally {
    busy.value = ''
  }
}
async function loadConfig() {
  try {
    const result = await apiAdminGet(`${base}/config/${encodeURIComponent(selected.value)}`)
    const server = servers.value.find((item) => item.slug === selected.value)
    const { has_client_secret, ...config } = result.data || {}
    Object.assign(form, defaults(), { resource: server?.url || '' }, config)
    hasSecret.value = !!has_client_secret
    clientSecret.value = ''
    redirectUri.value = result.redirect_uri
  } catch (error) {
    message.error(error.message || '读取服务配置失败')
  }
}
async function saveConfig() {
  saving.value = true
  try {
    await apiAdminPut(`${base}/config/${encodeURIComponent(selected.value)}`, {
      ...form,
      client_secret: clientSecret.value || null
    })
    message.success('配置已保存，请重新授权')
    await loadConfig()
    await loadConnections()
  } catch (error) {
    message.error(error.message || '保存失败')
  } finally {
    saving.value = false
  }
}
onMounted(async () => {
  await loadConnections()
  if (userStore.isAdmin) {
    try {
      servers.value = ((await getMcpServers()).data || []).filter((item) =>
        ['sse', 'streamable_http'].includes(item.transport)
      )
    } catch (error) {
      message.error(error.message || '读取服务列表失败')
    }
  }
})
</script>

<style scoped>
.connection {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 0;
  border-bottom: 1px solid var(--border-color);
}
.connection p {
  margin: 4px 0;
  color: var(--text-color-secondary);
}
.server-select {
  width: 100%;
  margin-bottom: 20px;
}
</style>
