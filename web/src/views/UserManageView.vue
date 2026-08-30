<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { authApi } from '@/apis/auth_api'
import { useUserStore } from '@/stores/user'
import PageHeader from '@/components/shared/PageHeader.vue'

const loading = ref(false)
const exportingUid = ref('')
const users = ref([])
const quotaModal = reactive({ open: false, uid: '', dailyRunLimit: null, monthlyTokenLimit: null })
const createModal = reactive({ open: false, username: '', password: '', role: 'user' })
// P5：用户详情抽屉（账户 / 密钥 / 问答 / 监控 四合一）
const detailDrawer = reactive({
  open: false,
  tab: 'account',
  uid: '',
  username: '',
  info: null,
  keys: [],
  keysLoading: false,
  conversations: [],
  conversationsLoading: false,
  activeThread: '',
  messages: [],
  messagesLoading: false,
  stats: null,
  statsLoading: false
})
const onboardingCard = reactive({
  open: false,
  username: '',
  uid: '',
  password: '',
  apiKeySecret: '',
  apiKeyExpiresAt: ''
})

const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin)
const isSuperAdmin = computed(() => userStore.isSuperAdmin)

async function loadUsers() {
  loading.value = true
  try {
    const allUsers = []
    for (let skip = 0; ; skip += 100) {
      const page = await authApi.listManagedUsers(skip, 100)
      allUsers.push(...page)
      if (page.length < 100) break
    }
    users.value = allUsers
  } catch (error) {
    message.error(error.message || '加载用户列表失败')
  } finally {
    loading.value = false
  }
}

async function toggleUser(user) {
  const action = user.is_disabled ? 'enable' : 'disable'
  const verb = user.is_disabled ? '启用' : '停用'
  if (!window.confirm(`确定${verb}用户 ${user.username}（${user.uid}）吗？停用会立即冻结其全部 API Key。`)) return
  try {
    await authApi.setManagedUserEnabled(user.uid, action === 'enable')
    message.success(`${verb}成功`)
    await loadUsers()
  } catch (error) {
    message.error(error.message || `${verb}失败`)
  }
}

async function openQuota(user) {
  quotaModal.uid = user.uid
  quotaModal.dailyRunLimit = null
  quotaModal.monthlyTokenLimit = null
  try {
    const data = await authApi.getManagedUserQuota(user.uid)
    quotaModal.dailyRunLimit = data.daily_run_limit
    quotaModal.monthlyTokenLimit = data.monthly_token_limit
    quotaModal.open = true
  } catch (error) {
    message.error(error.message || '加载配额失败')
  }
}

async function saveQuota() {
  try {
    await authApi.setManagedUserQuota(quotaModal.uid, {
      daily_run_limit: quotaModal.dailyRunLimit,
      monthly_token_limit: quotaModal.monthlyTokenLimit
    })
    message.success('配额已保存')
    quotaModal.open = false
  } catch (error) {
    message.error(error.message || '配额保存失败')
  }
}

async function createUser() {
  if (!createModal.username.trim() || createModal.password.length < 8) {
    message.error('请填写用户名与至少 8 位的密码')
    return
  }
  try {
    const created = await authApi.createManagedUser({
      username: createModal.username.trim(),
      password: createModal.password,
      role: createModal.role
    })
    if (!created.api_key_secret) {
      throw new Error('用户已创建，但服务端未返回一次性 API Key，请在用户详情中重置密钥')
    }
    message.success('用户已创建')
    // P5：开户卡——随机签发的桌面端密钥明文仅此一次展示，关闭后不可再看
    Object.assign(onboardingCard, {
      open: true,
      username: created.username || createModal.username.trim(),
      uid: created.uid,
      password: createModal.password,
      apiKeySecret: created.api_key_secret,
      apiKeyExpiresAt: (created.api_key_expires_at || '').slice(0, 10)
    })
    window.dispatchEvent(new CustomEvent('yuxi:api-keys-changed'))
    createModal.open = false
    createModal.username = ''
    createModal.password = ''
    createModal.role = 'user'
    await loadUsers()
  } catch (error) {
    message.error(error.message || '创建失败')
  }
}

async function copyOnboardingCard() {
  const card = onboardingCard
  const text = [
    `登录显示名：${card.username}`,
    `登录 ID：${card.uid}`,
    `初始密码：${card.password}`,
    `桌面端访问密钥：${card.apiKeySecret}`,
    `有效期至：${card.apiKeyExpiresAt || '90 天后'}`
  ].join('\n')
  try {
    await navigator.clipboard.writeText(text)
    message.success('开箱信息已复制，请通过安全渠道交付')
  } catch {
    message.warning('复制失败，请手动选择文本复制')
  }
}

function clearOnboardingCard() {
  Object.assign(onboardingCard, {
    open: false,
    username: '',
    uid: '',
    password: '',
    apiKeySecret: '',
    apiKeyExpiresAt: ''
  })
}

// P5：管理员查看普通用户的问答记录
const qaDrawer = reactive({
  open: false,
  uid: '',
  username: '',
  conversations: [],
  conversationsLoading: false,
  activeThread: '',
  activeTitle: '',
  messages: [],
  messagesLoading: false
})

async function openQaDrawer(user) {
  qaDrawer.open = true
  qaDrawer.uid = user.uid
  qaDrawer.username = user.username
  qaDrawer.conversations = []
  qaDrawer.activeThread = ''
  qaDrawer.messages = []
  qaDrawer.conversationsLoading = true
  try {
    const data = await authApi.listManagedUserConversations(user.uid)
    qaDrawer.conversations = data.conversations || []
    if (qaDrawer.conversations.length) {
      await loadConversationMessagesInto(qaDrawer, user.uid, qaDrawer.conversations[0])
    }
  } catch (error) {
    message.error(error.message || '加载会话列表失败')
  } finally {
    qaDrawer.conversationsLoading = false
  }
}

async function selectConversation(conv) {
  await loadConversationMessagesInto(qaDrawer, qaDrawer.uid, conv)
}

async function loadConversationMessagesInto(target, uid, conv) {
  target.activeThread = conv.thread_id
  if ('activeTitle' in target) target.activeTitle = conv.title || conv.thread_id
  target.messages = []
  target.messagesLoading = true
  try {
    const data = await authApi.listManagedUserMessages(uid, conv.thread_id)
    target.messages = data.messages || []
  } catch (error) {
    message.error(error.message || '加载问答失败')
  } finally {
    target.messagesLoading = false
  }
}

async function loadConversationsInto(target, uid) {
  target.conversationsLoading = true
  try {
    const data = await authApi.listManagedUserConversations(uid)
    target.conversations = data.conversations || []
    if (target.conversations.length) {
      await loadConversationMessagesInto(target, uid, target.conversations[0])
    }
  } catch (error) {
    target.conversations = []
    message.error(error.message || '加载会话列表失败')
  } finally {
    target.conversationsLoading = false
  }
}

async function openDetailDrawer(user) {
  detailDrawer.open = true
  detailDrawer.tab = 'account'
  detailDrawer.uid = user.uid
  detailDrawer.username = user.username
  detailDrawer.info = { ...user }
  detailDrawer.keys = []
  detailDrawer.conversations = []
  detailDrawer.activeThread = ''
  detailDrawer.messages = []
  detailDrawer.stats = null

  detailDrawer.keysLoading = true
  try {
    detailDrawer.keys = await authApi.listManagedApiKeys(user.uid)
  } catch (error) {
    message.error(error.message || '加载密钥失败')
  } finally {
    detailDrawer.keysLoading = false
  }
  await loadConversationsInto(detailDrawer, user.uid)

  detailDrawer.statsLoading = true
  try {
    detailDrawer.stats = await authApi.getManagedUserStats(user.uid)
  } catch (error) {
    message.error(error.message || '加载监控数据失败')
  } finally {
    detailDrawer.statsLoading = false
  }
}

async function selectConversationInDetail(conv) {
  await loadConversationMessagesInto(detailDrawer, detailDrawer.uid, conv)
}

async function exportUserConversations(uid, username) {
  exportingUid.value = uid
  try {
    const response = await authApi.exportManagedUserConversations(uid)
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    const safeName = String(username || uid).replace(/[\\/:*?"<>|]/g, '_')
    anchor.download = `${safeName}-问答记录.csv`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
    message.success('问答记录已导出')
  } catch (error) {
    message.error(error.message || '导出问答记录失败')
  } finally {
    exportingUid.value = ''
  }
}

async function resetManagedKey(key) {
  try {
    const res = await authApi.resetManagedApiKey(detailDrawer.uid, key.id)
    onboardingCard.open = true
    onboardingCard.username = detailDrawer.username
    onboardingCard.uid = detailDrawer.uid
    onboardingCard.password = ''
    onboardingCard.apiKeySecret = res.secret
    onboardingCard.apiKeyExpiresAt = ''
    await reloadDetailKeys()
    message.success('密钥已重置，新明文仅此一次展示')
  } catch (error) {
    message.error(error.message || '重置失败')
  }
}

async function deleteManagedKey(key) {
  try {
    await authApi.deleteManagedApiKey(detailDrawer.uid, key.id)
    message.success('密钥已删除')
    await reloadDetailKeys()
  } catch (error) {
    message.error(error.message || '删除失败')
  }
}

async function reloadDetailKeys() {
  detailDrawer.keysLoading = true
  try {
    detailDrawer.keys = await authApi.listManagedApiKeys(detailDrawer.uid)
  } finally {
    detailDrawer.keysLoading = false
  }
}

async function resetMemberPassword() {
  try {
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789'
    let generated = ''
    for (let i = 0; i < 12; i += 1) generated += alphabet[Math.floor(Math.random() * alphabet.length)]
    await authApi.resetManagedUserPassword(detailDrawer.uid, generated)
    onboardingCard.open = true
    onboardingCard.username = detailDrawer.username
    onboardingCard.uid = detailDrawer.uid
    onboardingCard.password = generated
    onboardingCard.apiKeySecret = ''
    onboardingCard.apiKeyExpiresAt = ''
    message.success('初始密码已重置，请通过安全渠道交付')
  } catch (error) {
    message.error(error.message || '重置密码失败')
  }
}

onMounted(() => {
  if (isAdmin.value) loadUsers()
})
</script>

<template>
  <div class="user-manage-view">
    <PageHeader title="用户与权限管理" description="企业级成员治理：开户、停用、配额与模型偏好入口。">
      <template #actions>
        <a-button type="primary" @click="createModal.open = true">+ 创建用户</a-button>
      </template>
    </PageHeader>

    <a-table
      :data-source="users"
      :loading="loading"
      row-key="uid"
      :pagination="{ pageSize: 15 }"
    >
      <a-table-column title="用户" data-index="username" />
      <a-table-column title="登录 ID" data-index="uid" />
      <a-table-column title="角色" data-index="role">
        <template #default="{ record }">
          <a-tag :color="record.role === 'superadmin' ? 'red' : record.role === 'admin' ? 'orange' : 'green'">
            {{ record.role }}
          </a-tag>
        </template>
      </a-table-column>
      <a-table-column title="部门" data-index="department_name">
        <template #default="{ record }">{{ record.department_name || '—' }}</template>
      </a-table-column>
      <a-table-column title="状态" key="status">
        <template #default="{ record }">
          <a-tag v-if="record.is_disabled && !record.last_login" color="orange">待审核</a-tag>
          <a-tag v-else-if="record.is_disabled" color="red">已停用</a-tag>
          <a-tag v-else color="green">正常</a-tag>
        </template>
      </a-table-column>
      <a-table-column title="最近登录" data-index="last_login">
        <template #default="{ record }">{{ record.last_login || '从未' }}</template>
      </a-table-column>
      <a-table-column title="操作" key="actions" width="260">
        <template #default="{ record }">
          <a-space>
            <a-button
              size="small"
              :disabled="record.uid === userStore.uid || (!isSuperAdmin && record.role !== 'user')"
              @click="toggleUser(record)"
            >
              {{ record.is_disabled ? '启用' : '停用' }}
            </a-button>
            <a-button
              size="small"
              :disabled="record.uid === userStore.uid || (!isSuperAdmin && record.role !== 'user')"
              @click="openQuota(record)"
            >配额</a-button>
            <a-button
              size="small"
              type="primary"
              ghost
              :disabled="record.uid === userStore.uid || (!isSuperAdmin && record.role !== 'user')"
              @click="openDetailDrawer(record)"
            >详情</a-button>
            <a-button size="small" @click="openQaDrawer(record)">问答</a-button>
          </a-space>
        </template>
      </a-table-column>
    </a-table>

    <a-modal
      :open="quotaModal.open"
      title="设置用户配额"
      @ok="saveQuota"
      @cancel="quotaModal.open = false"
    >
      <p class="quota-hint">留空表示不限制；配额在每次创建运行时预检。</p>
      <label class="quota-field">
        <span>每日运行次数上限</span>
        <a-input-number v-model:value="quotaModal.dailyRunLimit" :min="1" placeholder="不限制" style="width: 100%" />
      </label>
      <label class="quota-field">
        <span>每月 token 用量上限</span>
        <a-input-number v-model:value="quotaModal.monthlyTokenLimit" :min="1" placeholder="不限制" style="width: 100%" />
      </label>
    </a-modal>

    <a-modal
      :open="createModal.open"
      title="创建用户"
      @ok="createUser"
      @cancel="createModal.open = false"
    >
      <label class="quota-field">
        <span>用户名（登录显示名）</span>
        <a-input v-model:value="createModal.username" placeholder="如 zhangsan" />
      </label>
      <label class="quota-field">
        <span>初始密码（至少 8 位）</span>
        <a-input-password v-model:value="createModal.password" />
      </label>
      <label class="quota-field">
        <span>角色</span>
        <a-select v-model:value="createModal.role" style="width: 100%">
          <a-select-option value="user">普通用户</a-select-option>
          <a-select-option v-if="isSuperAdmin" value="admin">部门管理员</a-select-option>
        </a-select>
      </label>
      <p class="quota-hint">创建成功后将随机生成该用户的桌面端访问密钥（90 天有效），明文仅展示一次。</p>
    </a-modal>

    <a-modal
      :open="onboardingCard.open"
      title="开户成功 · 请妥善保存以下信息"
      :footer="null"
      :mask-closable="false"
      @cancel="onboardingCard.open = false"
      @afterClose="clearOnboardingCard"
    >
      <div class="onboarding-card">
        <p><b>登录显示名：</b>{{ onboardingCard.username }}</p>
        <p><b>登录 ID：</b>{{ onboardingCard.uid }}</p>
        <p><b>初始密码：</b>{{ onboardingCard.password }}</p>
        <p class="onboarding-key"><b>桌面端访问密钥（90 天有效）：</b><br /><code>{{ onboardingCard.apiKeySecret }}</code></p>
        <a-button type="primary" @click="copyOnboardingCard">复制全部信息</a-button>
        <p class="quota-hint">明文仅此一次展示，关闭后无法再次查看。请通过安全渠道交付给用户；到期后可在列表中重新签发。</p>
      </div>
    </a-modal>

    <a-drawer
      v-model:open="qaDrawer.open"
      :title="`问答记录 · ${qaDrawer.username}`"
      width="720"
      destroy-on-close
    >
      <template #extra>
        <a-button
          size="small"
          :loading="exportingUid === qaDrawer.uid"
          @click="exportUserConversations(qaDrawer.uid, qaDrawer.username)"
        >导出全部问答</a-button>
      </template>
      <div class="qa-layout">
        <div class="qa-conversations">
          <a-spin :spinning="qaDrawer.conversationsLoading">
            <a-empty v-if="!qaDrawer.conversations.length && !qaDrawer.conversationsLoading" description="该用户暂无会话" />
            <ul class="qa-list">
              <li
                v-for="conv in qaDrawer.conversations"
                :key="conv.thread_id"
                :class="{ active: conv.thread_id === qaDrawer.activeThread }"
                @click="selectConversation(conv)"
              >
                <span class="qa-title">{{ conv.title || '未命名会话' }}</span>
                <small>{{ (conv.updated_at || '').slice(0, 16).replace('T', ' ') }}</small>
              </li>
            </ul>
          </a-spin>
        </div>
        <div class="qa-messages">
          <a-spin :spinning="qaDrawer.messagesLoading">
            <a-empty v-if="!qaDrawer.messages.length && !qaDrawer.messagesLoading" description="选择左侧会话查看问答" />
            <div v-for="(msg, index) in qaDrawer.messages" :key="index" class="qa-message" :class="msg.role">
              <span class="qa-role">{{ msg.role === 'user' ? '用户' : '助手' }}</span>
              <div class="qa-content">{{ msg.content }}</div>
            </div>
          </a-spin>
        </div>
      </div>
    </a-drawer>
    <a-drawer
      v-model:open="detailDrawer.open"
      :title="`用户详情 · ${detailDrawer.username}`"
      width="860"
      destroy-on-close
    >
      <a-tabs v-model:activeKey="detailDrawer.tab">
        <a-tab-pane key="account" tab="账户信息">
          <a-descriptions v-if="detailDrawer.info" :column="1" bordered size="small">
            <a-descriptions-item label="登录 ID">{{ detailDrawer.info.uid }}</a-descriptions-item>
            <a-descriptions-item label="显示名">{{ detailDrawer.info.username }}</a-descriptions-item>
            <a-descriptions-item label="角色">
              <a-tag :color="detailDrawer.info.role === 'superadmin' ? 'red' : detailDrawer.info.role === 'admin' ? 'orange' : 'green'">{{ detailDrawer.info.role }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="部门">{{ detailDrawer.info.department_name || '—' }}</a-descriptions-item>
            <a-descriptions-item label="状态">
              <a-tag :color="detailDrawer.info.is_disabled ? 'red' : 'green'">{{ detailDrawer.info.is_disabled ? '已停用' : '正常' }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="创建时间">{{ (detailDrawer.info.created_at || '').slice(0, 19).replace('T', ' ') }}</a-descriptions-item>
            <a-descriptions-item label="最近登录">{{ detailDrawer.info.last_login || "从未" }}</a-descriptions-item>
          </a-descriptions>
          <a-space style="margin-top: 16px">
            <a-button @click="resetMemberPassword">重置初始密码（生成随机密码）</a-button>
          </a-space>
        </a-tab-pane>
        <a-tab-pane key="keys" tab="API Keys">
          <a-spin :spinning="detailDrawer.keysLoading">
            <a-empty v-if="!detailDrawer.keys.length && !detailDrawer.keysLoading" description="暂无密钥" />
            <a-table v-if="detailDrawer.keys.length" :data-source="detailDrawer.keys" row-key="id" :pagination="false" size="small">
              <a-table-column title="前缀" data-index="key_prefix" />
              <a-table-column title="名称" data-index="name" />
              <a-table-column title="用途" data-index="purpose" />
              <a-table-column title="状态" key="status">
                <template #default="{ record }">
                  <a-tag :color="record.status === 'enabled' ? 'green' : 'red'">{{ record.status === 'enabled' ? '启用' : '禁用' }}</a-tag>
                </template>
              </a-table-column>
              <a-table-column title="过期时间" key="expires_at">
                <template #default="{ record }">{{ record.expires_at ? record.expires_at.slice(0, 10) : "永久" }}</template>
              </a-table-column>
              <a-table-column title="操作" key="ops" width="200">
                <template #default="{ record }">
                  <a-space>
                    <a-popconfirm title="重置后将签发新密钥，旧密钥立即失效？" @confirm="resetManagedKey(record)">
                      <a-button size="small">重置</a-button>
                    </a-popconfirm>
                    <a-popconfirm title="确定物理删除该密钥？设备码会话关联将被断开。" @confirm="deleteManagedKey(record)">
                      <a-button size="small" danger>删除</a-button>
                    </a-popconfirm>
                  </a-space>
                </template>
              </a-table-column>
            </a-table>
          </a-spin>
        </a-tab-pane>
        <a-tab-pane key="qa" tab="问答记录（表格）">
          <div class="qa-table-toolbar">
            <a-select
              v-model:value="detailDrawer.activeThread"
              placeholder="选择会话"
              @change="selectConversationInDetail({ thread_id: $event })"
            >
              <a-select-option v-for="conv in detailDrawer.conversations" :key="conv.thread_id" :value="conv.thread_id">
                {{ conv.title || conv.thread_id }}
              </a-select-option>
            </a-select>
            <a-button
              :loading="exportingUid === detailDrawer.uid"
              @click="exportUserConversations(detailDrawer.uid, detailDrawer.username)"
            >导出全部问答</a-button>
          </div>
          <a-empty
            v-if="!detailDrawer.conversationsLoading && !detailDrawer.conversations.length"
            description="该用户暂无会话"
          />
          <a-table
            v-else
            :data-source="detailDrawer.messages"
            :loading="detailDrawer.messagesLoading"
            row-key="id"
            :pagination="{ pageSize: 20 }"
            :scroll="{ x: 760 }"
            size="small"
            table-layout="fixed"
          >
            <a-table-column title="时间" data-index="created_at" :width="180">
              <template #default="{ record }">{{ (record.created_at || '').slice(0, 19).replace('T', ' ') }}</template>
            </a-table-column>
            <a-table-column title="角色" data-index="role" :width="90">
              <template #default="{ record }">
                <a-tag :color="record.role === 'user' ? 'blue' : 'green'">{{ record.role === 'user' ? '提问' : '回答' }}</a-tag>
              </template>
            </a-table-column>
            <a-table-column title="内容" data-index="content">
              <template #default="{ record }">
                <div class="qa-table-content">{{ record.content }}</div>
              </template>
            </a-table-column>
          </a-table>
        </a-tab-pane>
        <a-tab-pane key="monitor" tab="监控面板">
          <a-spin :spinning="detailDrawer.statsLoading">
            <template v-if="detailDrawer.stats">
              <a-row :gutter="12" class="monitor-cards">
                <a-col :span="6"><a-card size="small"><a-statistic title="总运行次数" :value="detailDrawer.stats.total_runs" /></a-card></a-col>
                <a-col :span="6"><a-card size="small"><a-statistic title="总 Token 用量" :value="detailDrawer.stats.total_tokens" /></a-card></a-col>
                <a-col :span="6"><a-card size="small"><a-statistic title="BYOK 自费 Token" :value="detailDrawer.stats.byok_tokens" /></a-card></a-col>
                <a-col :span="6"><a-card size="small"><a-statistic title="接入策略" :value="detailDrawer.stats.entitlement.credential_policy" /></a-card></a-col>
              </a-row>
              <h4 style="margin: 12px 0 8px; font-weight: 600">按日趋势</h4>
              <a-table :data-source="detailDrawer.stats.daily" row-key="date" :pagination="false" size="small">
                <a-table-column title="日期" data-index="date" />
                <a-table-column title="运行次数" data-index="runs" />
                <a-table-column title="Token 用量" data-index="tokens" />
              </a-table>
            </template>
          </a-spin>
        </a-tab-pane>
      </a-tabs>
    </a-drawer>

  </div>
</template>

<style scoped>
.user-manage-view {
  padding: 4px 2px;
}

.quota-hint {
  font-size: 12px;
  color: var(--gray-500);
  margin-bottom: 12px;
}

.quota-field {
  display: block;
  margin-bottom: 14px;
  font-size: 13px;
}

.quota-field span {
  display: block;
  margin-bottom: 6px;
  color: var(--gray-600);
}

/* P5 开户卡与问答抽屉 */
.onboarding-card p {
  margin: 6px 0;
}

.onboarding-key code {
  display: block;
  margin-top: 4px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.04);
  word-break: break-all;
  user-select: all;
}

.qa-layout {
  display: flex;
  gap: 12px;
  min-height: 60vh;
}

.qa-conversations {
  width: 260px;
  flex-shrink: 0;
  border-right: 1px solid rgba(0, 0, 0, 0.06);
  overflow-y: auto;
  max-height: 70vh;
}

.qa-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.qa-list li {
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.qa-list li:hover {
  background: rgba(0, 0, 0, 0.04);
}

.qa-list li.active {
  background: rgba(24, 144, 255, 0.1);
}

.qa-title {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qa-messages {
  flex: 1;
  overflow-y: auto;
  max-height: 70vh;
  padding-right: 4px;
}

.qa-message {
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 10px;
}

.qa-message.user {
  background: rgba(24, 144, 255, 0.08);
}

.qa-message.assistant {
  background: rgba(0, 0, 0, 0.03);
}

.qa-role {
  font-size: 12px;
  opacity: 0.65;
  display: block;
  margin-bottom: 2px;
}

.qa-content {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.65;
}

.qa-table-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}

.qa-table-toolbar :deep(.ant-select) {
  flex: 1;
  min-width: 0;
}

.qa-table-content {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  line-height: 1.6;
}
/* P5 用户详情：监控卡片 */
.monitor-cards {
  margin-bottom: 12px;
}

.monitor-subtitle {
  margin: 12px 0 8px;
  font-weight: 600;
}
</style>
