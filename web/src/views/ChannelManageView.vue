<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { channelApi } from '@/apis/channel_api'

const loading = ref(false)
const error = ref('')
const apps = ref([])
const types = ref([])

const showCreate = ref(false)
const createForm = ref({
  channel_type: 'feishu',
  name: '',
  platform_app_id: '',
  platform_agent_id: '',
  service_uid: '',
  credentials: {},
  bound_agent_slug: '',
  allowed_chats: '',
  mention_only: true,
  push_placeholder: true,
  daily_limit: null,
  transport_mode: 'webhook',
  ip_allowlist: '',
  ip_allowlist_mode: 'log'
})
const createdReveal = ref(null)
const editingApp = ref(null)
const editForm = ref({})

const detailApp = ref(null)
const detailTab = ref('messages')
const detailMessages = ref([])
const detailOutbox = ref([])
const detailEndUsers = ref([])
const pairingResult = ref(null)

const channelTypeOptions = computed(() => types.value)

const credentialFields = {
  feishu: [
    { key: 'app_id', label: 'App ID', required: true },
    { key: 'app_secret', label: 'App Secret', required: true },
    { key: 'encrypt_key', label: 'Encrypt Key（事件加密，强烈建议）', required: false },
    { key: 'verification_token', label: 'Verification Token（建议）', required: false }
  ],
  wecom: [
    { key: 'corp_id', label: 'Corp ID', required: true },
    { key: 'corp_secret', label: 'Corp Secret', required: true },
    { key: 'agent_id', label: 'Agent ID', required: true },
    { key: 'token', label: '回调 Token', required: true },
    { key: 'encoding_aes_key', label: 'EncodingAESKey（43 位）', required: true }
  ],
  wechat_oa: [
    { key: 'app_id', label: 'AppID', required: true },
    { key: 'app_secret', label: 'AppSecret', required: true },
    { key: 'token', label: '回调 Token', required: true },
    { key: 'encoding_aes_key', label: 'EncodingAESKey（43 位）', required: true }
  ],
  dingtalk: [
    { key: 'app_secret', label: 'Robot Secret（明文模式签名校验，可选）', required: false },
    { key: 'encoding_aes_key', label: 'EncodingAESKey（加密回调，推荐）', required: false },
    { key: 'token', label: '回调 Token（加密回调配套）', required: false },
    { key: 'corp_id', label: 'Corp ID（加密回调 receiveid）', required: false }
  ],
  telegram: [
    { key: 'bot_token', label: 'Bot Token', required: true },
    { key: 'webhook_secret', label: 'Webhook Secret Token（可选）', required: false }
  ]
}

// 接入硬前提：不满足时表现为「能收不能回」或回调打不进来，现场最难排查。
const prerequisiteHints = {
  feishu: '前置：自建应用需订阅 im.message.receive_v1 并发布版本；开加密模式（encrypt_key）。',
  wecom:
    '前置：必须在企业微信后台配置「企业可信 IP」（本服务出口 IP），否则出站全部失败（errcode 60020）。群聊回复将以应用消息私聊送达。',
  wechat_oa:
    '前置：必须是认证服务号且已开通「客服消息」，否则能收不能回；回复仅在用户 48 小时内互动过的会话有效；回调域名需 ICP 备案且仅支持 80/443。',
  dingtalk:
    '前置：配 EncodingAESKey + Token + Corp ID 即启用加密回调（推荐）；仅明文模式时务必在下方配置 IP 白名单（enforce）。出站依赖消息携带的 sessionWebhook（约 2 小时时效），超长任务请在 Web 端完成。',
  telegram:
    '说明：webhook 需公网 HTTPS；无公网环境可不填回调地址——channels 运行时会自动长轮询（单实例）。'
}

const activeCredentialFields = computed(() => credentialFields[createForm.value.channel_type] || [])
const editCredentialFields = computed(() => credentialFields[editingApp.value?.channel_type] || [])
const activeTransportModes = computed(() => {
  const found = types.value.find((item) => item.channel_type === createForm.value.channel_type)
  return found?.inbound_modes || ['webhook']
})
const createdWebhookUrl = computed(() => {
  const value = createdReveal.value
  if (!value?.webhook_url_template || !value?.path_token) return ''
  return value.webhook_url_template.replace('{path_token}', value.path_token)
})

watch(
  () => createForm.value.channel_type,
  (channelType) => {
    createForm.value.transport_mode = channelType === 'telegram' ? 'long_poll' : 'webhook'
  }
)

const channelTypeLabel = (type) => {
  const found = types.value.find((t) => t.channel_type === type)
  return found ? found.label : type
}

const toggleCreate = () => {
  showCreate.value = !showCreate.value
  resetCreate()
}

const loadApps = async () => {
  loading.value = true
  error.value = ''
  try {
    const [appsRes, typesRes] = await Promise.all([channelApi.listApps(), channelApi.listTypes()])
    apps.value = appsRes.items || []
    types.value = typesRes.items || []
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

const submitCreate = async () => {
  error.value = ''
  try {
    const payload = {
      channel_type: createForm.value.channel_type,
      name: createForm.value.name || createForm.value.platform_app_id,
      platform_app_id: createForm.value.platform_app_id,
      platform_agent_id: createForm.value.platform_agent_id || null,
      service_uid: createForm.value.service_uid,
      credentials: createForm.value.credentials,
      config: {
        bound_agent_slug: createForm.value.bound_agent_slug,
        allowed_chats: createForm.value.allowed_chats
          ? createForm.value.allowed_chats.split(/[,，\s]+/).filter(Boolean)
          : null,
        mention_only: createForm.value.mention_only,
        push_placeholder: createForm.value.push_placeholder,
        daily_limit: createForm.value.daily_limit ? Number(createForm.value.daily_limit) : null,
        transport_mode: createForm.value.transport_mode,
        ip_allowlist: createForm.value.ip_allowlist
          ? createForm.value.ip_allowlist.split(/[,，\s]+/).filter(Boolean)
          : null,
        ip_allowlist_mode: createForm.value.ip_allowlist_mode
      }
    }
    const created = await channelApi.createApp(payload)
    createdReveal.value = created
    showCreate.value = false
    await loadApps()
  } catch (e) {
    error.value = typeof e === 'string' ? e : e.message || '创建失败'
  }
}

const resetCreate = () => {
  createForm.value = {
    channel_type: createForm.value.channel_type,
    name: '',
    platform_app_id: '',
    platform_agent_id: '',
    service_uid: '',
    credentials: {},
    bound_agent_slug: '',
    allowed_chats: '',
    mention_only: true,
    push_placeholder: true,
    daily_limit: null,
    transport_mode: createForm.value.channel_type === 'telegram' ? 'long_poll' : 'webhook',
    ip_allowlist: '',
    ip_allowlist_mode: 'log'
  }
}

const toggleEnabled = async (app) => {
  try {
    if (app.is_enabled) await channelApi.deactivateApp(app.id)
    else await channelApi.activateApp(app.id)
    await loadApps()
  } catch (e) {
    error.value = e.message || '更新失败'
  }
}

const testConnection = async (app) => {
  try {
    const result = await channelApi.testApp(app.id)
    if (!result.ok) error.value = `${result.code}：${result.message}`
    await loadApps()
  } catch (e) {
    error.value = e.message || '连接测试失败'
  }
}

const openEdit = (app) => {
  editingApp.value = app
  editForm.value = {
    name: app.name,
    platform_agent_id: app.platform_agent_id || '',
    service_uid: app.service_uid,
    bound_agent_slug: app.config?.bound_agent_slug || '',
    allowed_chats: (app.config?.allowed_chats || []).join(', '),
    daily_limit: app.config?.daily_limit || null,
    transport_mode: app.transport_mode,
    ip_allowlist: (app.config?.ip_allowlist || []).join(', '),
    ip_allowlist_mode: app.config?.ip_allowlist_mode || 'log',
    credentials: {}
  }
}

const submitEdit = async () => {
  const app = editingApp.value
  if (!app) return
  try {
    const credentials = Object.fromEntries(
      Object.entries(editForm.value.credentials || {}).filter(([, value]) =>
        String(value || '').trim()
      )
    )
    const payload = {
      name: editForm.value.name,
      platform_agent_id: editForm.value.platform_agent_id || null,
      service_uid: editForm.value.service_uid,
      config: {
        bound_agent_slug: editForm.value.bound_agent_slug,
        allowed_chats: editForm.value.allowed_chats
          ? editForm.value.allowed_chats.split(/[,，\s]+/).filter(Boolean)
          : null,
        daily_limit: editForm.value.daily_limit ? Number(editForm.value.daily_limit) : null,
        transport_mode: editForm.value.transport_mode,
        ip_allowlist: editForm.value.ip_allowlist
          ? editForm.value.ip_allowlist.split(/[,，\s]+/).filter(Boolean)
          : null,
        ip_allowlist_mode: editForm.value.ip_allowlist_mode
      }
    }
    if (Object.keys(credentials).length) payload.credentials = credentials
    await channelApi.updateApp(app.id, payload)
    editingApp.value = null
    await loadApps()
  } catch (e) {
    error.value = e.message || '保存失败'
  }
}

const copyWebhook = async () => {
  if (!createdWebhookUrl.value) return
  await navigator.clipboard.writeText(createdWebhookUrl.value)
}

const regenerate = async (app) => {
  if (!window.confirm('重置后旧回调地址立即失效，平台侧需同步改配。继续？')) return
  try {
    const result = await channelApi.regeneratePathToken(app.id)
    createdReveal.value = {
      ...result,
      name: `${app.name}（重置后）`,
      channel_type: app.channel_type
    }
    await loadApps()
  } catch (e) {
    error.value = e.message || '重置失败'
  }
}

const removeApp = async (app) => {
  if (!window.confirm(`删除渠道应用「${app.name}」？关联消息流水将一并删除（run 审计保留）。`))
    return
  try {
    await channelApi.deleteApp(app.id)
    if (detailApp.value?.id === app.id) detailApp.value = null
    await loadApps()
  } catch (e) {
    error.value = e.message || '删除失败'
  }
}

const openDetail = async (app) => {
  detailApp.value = app
  detailTab.value = 'messages'
  pairingResult.value = null
  await loadDetail()
}

const loadDetail = async () => {
  const app = detailApp.value
  if (!app) return
  try {
    const [messages, outbox, endUsers] = await Promise.all([
      channelApi.listMessages(app.id, { limit: 50 }),
      channelApi.listOutbox(app.id, { limit: 50 }),
      channelApi.listEndUsers(app.id)
    ])
    detailMessages.value = messages.items || []
    detailOutbox.value = outbox.items || []
    detailEndUsers.value = endUsers.items || []
  } catch (e) {
    error.value = e.message || '加载明细失败'
  }
}

const requeue = async (row) => {
  try {
    await channelApi.requeueOutbox(row.id)
    await loadDetail()
  } catch (e) {
    error.value = e.message || '重排队失败'
  }
}

const unbindUser = async (endUser) => {
  try {
    await channelApi.unbindEndUser(detailApp.value.id, endUser.id)
    await loadDetail()
  } catch (e) {
    error.value = e.message || '解绑失败'
  }
}

const createPairing = async () => {
  pairingResult.value = null
  try {
    pairingResult.value = await channelApi.createPairing(detailApp.value.id)
  } catch (e) {
    error.value = e.message || '生成绑定码失败'
  }
}

const statusText = (status) =>
  ({
    received: '已接收',
    ignored: '已忽略',
    rejected: '已拒绝',
    dispatched: '已派发',
    replied: '已回复',
    failed: '失败'
  })[status] || status

const lifecycleText = (status) =>
  ({ DRAFT: '草稿', READY: '待激活', ACTIVE: '运行中', BLOCKED: '阻塞', PAUSED: '已停用' })[
    status
  ] || status

onMounted(loadApps)
</script>

<template>
  <div class="channel-manage">
    <header class="page-head">
      <h2>渠道接入</h2>
      <a-button type="primary" @click="toggleCreate">
        {{ showCreate ? '收起' : '新建渠道应用' }}
      </a-button>
    </header>

    <p v-if="error" class="error">{{ error }}</p>

    <section v-if="createdReveal" class="reveal-card">
      <h3>「{{ createdReveal.name || createdReveal.channel_type }}」已保存为草稿</h3>
      <template v-if="createdWebhookUrl">
        <code>{{ createdWebhookUrl }}</code>
        <p class="hint">令牌仅此一次展示。请配置到平台后台，完成连接测试后再激活。</p>
        <a-button size="small" @click="copyWebhook">复制回调地址</a-button>
      </template>
      <p v-else-if="createdReveal.transport_mode === 'long_poll'" class="hint">
        长轮询模式无需回调地址；系统将在连接测试通过并激活后自动接管。
      </p>
      <p v-else class="error">
        尚未配置公网 HTTPS 地址，本应用会保持阻塞。请设置 CHANNEL_PUBLIC_BASE_URL 后重启 api。
      </p>
      <a-button @click="createdReveal = null">我知道了</a-button>
    </section>

    <section v-if="showCreate" class="create-card">
      <div class="grid">
        <label
          >渠道类型
          <a-select v-model:value="createForm.channel_type">
            <a-select-option
              v-for="t in channelTypeOptions"
              :key="t.channel_type"
              :value="t.channel_type"
            >
              {{ t.label }}
            </a-select-option>
          </a-select>
        </label>
        <label
          >传输模式
          <a-select v-model:value="createForm.transport_mode">
            <a-select-option v-for="mode in activeTransportModes" :key="mode" :value="mode">
              {{ mode === 'long_poll' ? '长轮询（无需公网）' : 'Webhook（需要公网 HTTPS）' }}
            </a-select-option>
          </a-select>
        </label>
        <label
          >应用名称<a-input v-model:value="createForm.name" placeholder="如：科研问答机器人"
        /></label>
        <label
          >平台应用 ID<a-input
            v-model:value="createForm.platform_app_id"
            placeholder="飞书 App ID / 企微 CorpID / 公众号 AppID / TG bot username"
        /></label>
        <label
          >二级标识（企微 AgentID 等，可选）<a-input v-model:value="createForm.platform_agent_id"
        /></label>
        <label
          >服务账号 uid<a-input
            v-model:value="createForm.service_uid"
            placeholder="组织级服务账号（未绑定时以该账号执行）"
        /></label>
        <label
          >绑定智能体 slug<a-input
            v-model:value="createForm.bound_agent_slug"
            placeholder="如 default-chatbot"
        /></label>
        <label
          >会话白名单（可选）<a-input
            v-model:value="createForm.allowed_chats"
            placeholder="chat_id 逗号分隔；留空不限"
        /></label>
        <label
          >每日消息上限（可选）<a-input-number
            v-model:value="createForm.daily_limit"
            :min="1"
            style="width: 100%"
        /></label>
        <label
          >来源 IP 白名单（CIDR，可选）<a-input
            v-model:value="createForm.ip_allowlist"
            placeholder="如 1.2.3.0/24，多个用逗号分隔"
        /></label>
        <label
          >IP 白名单模式
          <a-select v-model:value="createForm.ip_allowlist_mode">
            <a-select-option value="log">仅记录（上线观察期）</a-select-option>
            <a-select-option value="enforce">强制拦截</a-select-option>
          </a-select>
        </label>
      </div>
      <p v-if="prerequisiteHints[createForm.channel_type]" class="prereq-hint">
        ⚠️ {{ prerequisiteHints[createForm.channel_type] }}
      </p>
      <div class="grid">
        <label v-for="field in activeCredentialFields" :key="field.key">
          {{ field.label }}
          <a-input-password
            v-model:value="createForm.credentials[field.key]"
            :placeholder="field.required ? '必填' : '可选'"
            autocomplete="new-password"
          />
        </label>
      </div>
      <div class="checks">
        <a-checkbox v-model:checked="createForm.mention_only">群聊仅响应 @机器人</a-checkbox>
        <a-checkbox v-model:checked="createForm.push_placeholder"
          >发送「正在分析」占位回复</a-checkbox
        >
      </div>
      <a-button type="primary" @click="submitCreate">创建</a-button>
    </section>

    <p v-if="loading">加载中……</p>
    <p v-else-if="!apps.length" class="empty">
      暂无渠道应用。点击「新建渠道应用」接入飞书 / 企微 / 公众号 / 钉钉 / Telegram。
    </p>

    <section v-if="editingApp" class="create-card">
      <header class="page-head">
        <h3>编辑「{{ editingApp.name }}」</h3>
        <a-button size="small" @click="editingApp = null">取消</a-button>
      </header>
      <div class="grid">
        <label>应用名称<a-input v-model:value="editForm.name" /></label>
        <label>服务账号 uid<a-input v-model:value="editForm.service_uid" /></label>
        <label>二级标识<a-input v-model:value="editForm.platform_agent_id" /></label>
        <label>绑定智能体 slug<a-input v-model:value="editForm.bound_agent_slug" /></label>
        <label>会话白名单<a-input v-model:value="editForm.allowed_chats" /></label>
        <label
          >每日消息上限<a-input-number
            v-model:value="editForm.daily_limit"
            :min="1"
            style="width: 100%"
        /></label>
        <label
          >传输模式
          <a-select v-model:value="editForm.transport_mode">
            <a-select-option
              v-for="mode in types.find((item) => item.channel_type === editingApp.channel_type)
                ?.inbound_modes || ['webhook']"
              :key="mode"
              :value="mode"
              >{{ mode === 'long_poll' ? '长轮询（无需公网）' : 'Webhook' }}</a-select-option
            >
          </a-select>
        </label>
        <label>IP 白名单<a-input v-model:value="editForm.ip_allowlist" /></label>
        <label
          >IP 白名单模式
          <a-select v-model:value="editForm.ip_allowlist_mode">
            <a-select-option value="log">仅记录</a-select-option>
            <a-select-option value="enforce">强制拦截</a-select-option>
          </a-select>
        </label>
      </div>
      <p class="hint">
        当前凭据：{{
          editingApp.credentials_masked_hint || '未显示'
        }}。下列字段全部留空则保持原凭据；填写时将整体轮换并要求重新测试。
      </p>
      <div class="grid">
        <label v-for="field in editCredentialFields" :key="field.key">
          {{ field.label }}
          <a-input-password
            v-model:value="editForm.credentials[field.key]"
            placeholder="留空保持不变"
            autocomplete="new-password"
          />
        </label>
      </div>
      <a-button type="primary" @click="submitEdit">保存并重新测试</a-button>
    </section>

    <table v-if="apps.length" class="app-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>类型</th>
          <th>平台 ID</th>
          <th>服务账号</th>
          <th>状态</th>
          <th>连接测试</th>
          <th>最近入站</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="app in apps" :key="app.id" :class="{ active: detailApp?.id === app.id }">
          <td>{{ app.name }}</td>
          <td>{{ channelTypeLabel(app.channel_type) }}</td>
          <td>
            <code>{{ app.platform_app_id }}</code>
          </td>
          <td>
            <code>{{ app.service_uid }}</code>
          </td>
          <td>
            <span :class="['badge', app.lifecycle_status === 'ACTIVE' ? 'ok' : 'off']">
              {{ lifecycleText(app.lifecycle_status) }}
            </span>
            <span v-if="app.outbox_dead > 0" class="badge off" title="出站死信，需在明细中 requeue"
              >死信 {{ app.outbox_dead }}</span
            >
            <span v-else-if="app.outbox_pending > 0" class="badge" title="出站待投递积压">{{
              '待发 ' + app.outbox_pending
            }}</span>
          </td>
          <td>
            <span :class="['badge', app.connection_test?.ok ? 'ok' : 'off']">
              {{ app.connection_test?.ok ? '已通过' : app.connection_test?.code || '未测试' }}
            </span>
          </td>
          <td>{{ app.last_inbound_at || '—' }}</td>
          <td class="actions">
            <a-button size="small" @click="openDetail(app)">明细</a-button>
            <a-button size="small" @click="openEdit(app)">编辑</a-button>
            <a-button size="small" @click="testConnection(app)">连接测试</a-button>
            <a-button size="small" @click="toggleEnabled(app)">{{
              app.is_enabled ? '停用' : '激活'
            }}</a-button>
            <a-button size="small" @click="regenerate(app)">重置令牌</a-button>
            <a-button size="small" danger @click="removeApp(app)">删除</a-button>
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="detailApp" class="detail-card">
      <header>
        <h3>{{ detailApp.name }} · 运营明细</h3>
        <nav class="tabs">
          <button
            type="button"
            :class="{ on: detailTab === 'messages' }"
            @click="detailTab = 'messages'"
          >
            消息流水
          </button>
          <button
            type="button"
            :class="{ on: detailTab === 'outbox' }"
            @click="detailTab = 'outbox'"
          >
            出站队列
          </button>
          <button type="button" :class="{ on: detailTab === 'users' }" @click="detailTab = 'users'">
            终端用户
          </button>
          <button
            type="button"
            :class="{ on: detailTab === 'pairing' }"
            @click="detailTab = 'pairing'"
          >
            身份绑定
          </button>
        </nav>
      </header>

      <div v-if="detailTab === 'messages'">
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>方向</th>
              <th>会话</th>
              <th>摘要</th>
              <th>状态</th>
              <th>run</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in detailMessages" :key="m.id">
              <td>{{ m.created_at }}</td>
              <td>{{ m.direction === 'in' ? '入' : '出' }}</td>
              <td>
                <code>{{ m.platform_chat_id }}</code>
              </td>
              <td>{{ m.content_digest || '—' }}</td>
              <td>
                {{ statusText(m.status)
                }}<small v-if="m.status_detail">（{{ m.status_detail }}）</small>
              </td>
              <td>
                <code v-if="m.run_id">{{ m.run_id.slice(0, 8) }}</code
                ><span v-else>—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="detailTab === 'outbox'">
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>状态</th>
              <th>尝试</th>
              <th>最近错误</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in detailOutbox" :key="row.id">
              <td>{{ row.created_at }}</td>
              <td>
                <span
                  :class="[
                    'badge',
                    row.status === 'PUSHED' ? 'ok' : row.status === 'DEAD' ? 'off' : ''
                  ]"
                  >{{ row.status }}</span
                >
              </td>
              <td>{{ row.attempts }}/{{ row.max_attempts }}</td>
              <td>{{ row.last_error || '—' }}</td>
              <td>
                <a-button v-if="row.status === 'DEAD'" size="small" @click="requeue(row)"
                  >重新投递</a-button
                >
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="detailTab === 'users'">
        <table class="data-table">
          <thead>
            <tr>
              <th>平台身份</th>
              <th>昵称</th>
              <th>绑定账号</th>
              <th>绑定时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in detailEndUsers" :key="u.id">
              <td>
                <code>{{ u.platform_user_id }}</code>
              </td>
              <td>{{ u.display_name || '—' }}</td>
              <td>{{ u.bound_uid ? `已绑定 ${u.bound_uid}` : '未绑定（服务账号）' }}</td>
              <td>{{ u.bound_at || '—' }}</td>
              <td>
                <a-button v-if="u.bound_uid" size="small" @click="unbindUser(u)">解绑</a-button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="detailTab === 'pairing'">
        <a-button type="primary" @click="createPairing">生成绑定码</a-button>
        <div v-if="pairingResult" class="pairing-box">
          <p>
            绑定码（10 分钟内有效，一次性）：<code class="big">{{ pairingResult.code }}</code>
          </p>
          <p>
            用户在渠道对话中发送：<code>/bind {{ pairingResult.code }}</code>
          </p>
          <p v-if="pairingResult.qr_url">
            或扫码绑定：<a :href="pairingResult.qr_url" target="_blank">打开带参二维码</a>
          </p>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped lang="less">
// 颜色一律走 base.css 变量：base.dark.css 在 :root.dark 下整体反转同名变量，
// 本页无需任何暗色分支；antd 控件由 theme store 的 darkAlgorithm 自适应。
.channel-manage {
  padding: 20px 24px;
  max-width: 1100px;
  margin: 0 auto;
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.error {
  color: var(--color-error-500);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px 16px;
  margin-bottom: 12px;

  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }
}
.checks {
  display: flex;
  gap: 20px;
  margin-bottom: 16px;
}
.reveal-card,
.create-card,
.detail-card {
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 12px 0;
  background: var(--color-bg-container);
}
.reveal-card code {
  display: block;
  padding: 8px;
  background: var(--gray-100);
  border-radius: 6px;
  word-break: break-all;
  margin: 8px 0;
}
.hint {
  font-size: 12px;
  color: var(--gray-600);
  margin-bottom: 8px;
}
.prereq-hint {
  grid-column: 1 / -1;
  font-size: 12px;
  color: var(--color-warning-900);
  background: var(--color-warning-50);
  border-radius: 6px;
  padding: 6px 10px;
  margin: 0 0 12px;
}
.app-table,
.data-table {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;

  th,
  td {
    border-bottom: 1px solid var(--gray-150);
    padding: 8px 10px;
    text-align: left;
    font-size: 13px;
  }

  code {
    padding: 1px 6px;
    background: var(--gray-100);
    border-radius: 4px;
    font-size: 12px;
  }

  tr.active {
    background: var(--gray-50);
  }

  .actions {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: var(--gray-150);

  &.ok {
    background: var(--color-success-50);
    color: var(--color-success-900);
  }

  &.off {
    background: var(--color-error-50);
    color: var(--color-error-900);
  }

  & + .badge {
    margin-left: 4px;
  }
}
.tabs {
  display: flex;
  gap: 8px;
  margin-top: 8px;

  button {
    border: none;
    background: transparent;
    padding: 4px 10px;
    cursor: pointer;
    color: var(--gray-800);
    border-bottom: 2px solid transparent;

    &.on {
      border-bottom-color: var(--main-700);
      color: var(--main-700);
    }
  }
}
.pairing-box {
  margin-top: 10px;

  .big {
    font-size: 20px;
    letter-spacing: 2px;
  }
}
.empty {
  color: var(--gray-600);
}
</style>
