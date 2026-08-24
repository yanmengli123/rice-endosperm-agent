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
    await authApi.createManagedUser({
      username: createModal.username.trim(),
      password: createModal.password,
      role: createModal.role
    })
    message.success('用户已创建')
    createModal.open = false
    createModal.username = ''
    createModal.password = ''
    createModal.role = 'user'
    await loadUsers()
  } catch (error) {
    message.error(error.message || '创建失败')
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
    </a-modal>
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
