<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { authApi } from '@/apis/auth_api'
import { useUserStore } from '@/stores/user'
import PageHeader from '@/components/shared/PageHeader.vue'

const loading = ref(false)
const users = ref([])
const quotaModal = reactive({ open: false, uid: '', dailyRunLimit: null, monthlyTokenLimit: null })
const createModal = reactive({ open: false, username: '', password: '', role: 'user' })
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
    message.success('用户已创建')
    // P5：开户卡——随机签发的桌面端密钥明文仅此一次展示，关闭后不可再看
    if (created.api_key_secret) {
      Object.assign(onboardingCard, {
        open: true,
        username: created.username || createModal.username.trim(),
        uid: created.uid,
        password: createModal.password,
        apiKeySecret: created.api_key_secret,
        apiKeyExpiresAt: (created.api_key_expires_at || '').slice(0, 10)
      })
    }
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
  } catch (error) {
    message.error(error.message || '加载会话列表失败')
  } finally {
    qaDrawer.conversationsLoading = false
  }
}

async function selectConversation(conv) {
  qaDrawer.activeThread = conv.thread_id
  qaDrawer.activeTitle = conv.title || conv.thread_id
  qaDrawer.messages = []
  qaDrawer.messagesLoading = true
  try {
    const data = await authApi.listManagedUserMessages(qaDrawer.uid, conv.thread_id)
    qaDrawer.messages = data.messages || []
  } catch (error) {
    message.error(error.message || '加载问答失败')
  } finally {
    qaDrawer.messagesLoading = false
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
          <a-tag v-if="record.is_disabled" color="red">已停用</a-tag>
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
      @cancel="onboardingCard.open = false"
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
</style>


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
  word-break: break-word;
}
