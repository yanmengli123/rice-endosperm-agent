<script setup>
import { computed, onMounted, ref } from 'vue'
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
  daily_limit: null
})
const createdReveal = ref(null)

const detailApp = ref(null)
const detailTab = ref('messages')
const detailMessages = ref([])
const detailOutbox = ref([])
const detailEndUsers = ref([])
const pairingResult = ref(null)

const channelTypeOptions = computed(() => types.value)

const pageOrigin = window.location.origin

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
        daily_limit: createForm.value.daily_limit ? Number(createForm.value.daily_limit) : null
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
    daily_limit: null
  }
}

const toggleEnabled = async (app) => {
  try {
    await channelApi.updateApp(app.id, { is_enabled: !app.is_enabled })
    await loadApps()
  } catch (e) {
    error.value = e.message || '更新失败'
  }
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
      <h3>
        「{{ createdReveal.name || createdReveal.channel_type }}」回调地址已生成（令牌仅此一次展示）
      </h3>
      <code
        >{{ pageOrigin
        }}{{
          createdReveal.webhook_path ||
          createdReveal.webhook_path_template?.replace('{path_token}', createdReveal.path_token)
        }}</code
      >
      <p class="hint">请将该地址配置到平台后台的事件订阅/回调 URL；重置令牌后旧地址立即失效。</p>
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
      </div>
      <p v-if="prerequisiteHints[createForm.channel_type]" class="prereq-hint">
        ⚠️ {{ prerequisiteHints[createForm.channel_type] }}
      </p>
      <div class="grid">
        <label v-for="field in activeCredentialFields" :key="field.key">
          {{ field.label }}
          <a-input
            v-model:value="createForm.credentials[field.key]"
            :placeholder="field.required ? '必填' : '可选'"
            autocomplete="off"
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

    <table v-else class="app-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>类型</th>
          <th>平台 ID</th>
          <th>服务账号</th>
          <th>状态</th>
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
            <span :class="['badge', app.is_enabled ? 'ok' : 'off']">{{
              app.is_enabled ? '启用' : '停用'
            }}</span>
            <span v-if="app.outbox_dead > 0" class="badge off" title="出站死信，需在明细中 requeue"
              >死信 {{ app.outbox_dead }}</span
            >
            <span v-else-if="app.outbox_pending > 0" class="badge" title="出站待投递积压">{{
              '待发 ' + app.outbox_pending
            }}</span>
          </td>
          <td>{{ app.last_inbound_at || '—' }}</td>
          <td class="actions">
            <a-button size="small" @click="openDetail(app)">明细</a-button>
            <a-button size="small" @click="toggleEnabled(app)">{{
              app.is_enabled ? '停用' : '启用'
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
