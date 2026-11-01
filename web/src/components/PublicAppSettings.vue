<template>
  <section class="public-app-settings">
    <AppUsagePanel ref="usagePanel" :apps="apps" />
    <div class="app-heading">
      <h3>公开 API 应用</h3>
      <a-button @click="edit()">创建应用</a-button>
    </div>
    <p>为外部服务单独授权智能体与知识库。终端用户按 X-End-User-Id 隔离，共享你的用量额度。</p>
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <a-spin :spinning="busy">
      <a-empty v-if="!apps.length && !busy" description="尚未创建应用" />
      <article v-for="app in apps" :key="app.id" class="app-row">
        <div>
          <strong>{{ app.name }}</strong> <a-tag>{{ app.enabled ? '已启用' : '已停用' }}</a-tag>
        </div>
        <small>{{ app.agent_slugs.length }} 个智能体 · {{ app.kb_ids.length }} 个知识库</small>
        <a-space
          ><a-button size="small" @click="edit(app)">授权设置</a-button>
          <a-button size="small" @click="manage(app)">凭据与终端用户</a-button></a-space
        >
      </article>
    </a-spin>
    <a-modal v-model:open="editing" title="应用授权" :confirm-loading="busy" @ok="save">
      <a-form layout="vertical">
        <a-form-item label="应用名称" required
          ><a-input v-model:value="form.name" :maxlength="100"
        /></a-form-item>
        <a-form-item label="智能体"
          ><a-select
            v-model:value="form.agent_slugs"
            mode="multiple"
            :options="agentOptions"
            placeholder="选择允许调用的智能体"
        /></a-form-item>
        <a-form-item label="知识库"
          ><a-select
            v-model:value="form.kb_ids"
            mode="multiple"
            :options="kbOptions"
            placeholder="选择允许读取的知识库"
        /></a-form-item>
        <a-form-item label="终端用户数量上限"
          ><a-input-number v-model:value="form.max_end_users" :min="1" :max="100000"
        /></a-form-item>
        <a-form-item label="允许使用我的模型凭据"
          ><a-switch v-model:checked="form.allow_owner_model_credentials" />
          <p>启用后，应用可使用你配置的模型密钥调用已授权智能体。</p></a-form-item
        >
        <a-form-item label="应用启用"><a-switch v-model:checked="form.enabled" /></a-form-item>
      </a-form>
    </a-modal>
    <a-modal
      v-model:open="managing"
      :title="selected?.name"
      :footer="null"
      @after-close="secret = ''"
    >
      <a-alert v-if="secret" type="success" message="新密钥仅显示本次，请立即保存">
        <template #description
          ><code class="secret">{{ secret }}</code></template
        >
      </a-alert>
      <h4>应用凭据</h4>
      <a-space
        ><a-input v-model:value="keyName" placeholder="凭据名称" :maxlength="100" />
        <a-button :loading="busy" @click="issue">生成密钥（90 天）</a-button></a-space
      >
      <div v-for="key in keys" :key="key.id" class="credential-row">
        <span>{{ key.name }} · {{ key.key_prefix }}…</span>
        <a-button size="small" :loading="busy" @click="toggleKey(key)">{{
          key.enabled ? '停用' : '启用'
        }}</a-button>
      </div>
      <h4>终端用户（最近列表，最多 50 个）</h4>
      <div v-for="user in endUsers" :key="user.uid" class="credential-row">
        <span>{{ user.external_id }}</span
        ><a-button size="small" :loading="busy" @click="toggleUser(user)">
          {{ user.enabled ? '停用' : '启用' }}</a-button
        >
      </div>
      <a-empty v-if="!endUsers.length" description="尚无终端用户" />
    </a-modal>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { publicAppApi } from '@/apis/public_app_api'
import AppUsagePanel from '@/components/AppUsagePanel.vue'
const usagePanel = ref(null)

const apps = ref([]),
  keys = ref([]),
  endUsers = ref([]),
  agentOptions = ref([]),
  kbOptions = ref([])
const busy = ref(false),
  error = ref(''),
  editing = ref(false),
  managing = ref(false)
const selected = ref(null),
  secret = ref(''),
  keyName = ref('')
const defaults = () => ({
  name: '',
  enabled: true,
  agent_slugs: [],
  kb_ids: [],
  allow_owner_model_credentials: false,
  max_end_users: 1000
})
const form = reactive(defaults())
const action = async (fn) => {
  busy.value = true
  error.value = ''
  try {
    await fn()
    await usagePanel.value?.refresh()
  } catch (e) {
    error.value = e.message || '应用操作失败'
  } finally {
    busy.value = false
  }
}
const load = async () => {
  apps.value = (await publicAppApi.list()).data
}
const edit = (app) => {
  selected.value = app || null
  const value = defaults()
  if (app)
    Object.keys(value).forEach((key) => {
      value[key] = app[key]
    })
  Object.assign(form, JSON.parse(JSON.stringify(value)))
  editing.value = true
}
const save = () =>
  action(async () => {
    if (!form.name.trim()) throw new Error('请输入应用名称')
    if (selected.value) await publicAppApi.update(selected.value.id, { ...form })
    else await publicAppApi.create({ ...form })
    editing.value = false
    await load()
  })
const details = async () => {
  const [credentials, users] = await Promise.all([
    publicAppApi.keys(selected.value.id),
    publicAppApi.endUsers(selected.value.id)
  ])
  keys.value = credentials.data
  endUsers.value = users.data
}
const manage = (app) =>
  action(async () => {
    selected.value = app
    secret.value = ''
    keyName.value = ''
    keys.value = []
    endUsers.value = []
    await details()
    managing.value = true
  })
const issue = () =>
  action(async () => {
    if (!keyName.value.trim()) throw new Error('请输入凭据名称')
    secret.value = (
      await publicAppApi.issueKey(selected.value.id, { name: keyName.value, expires_days: 90 })
    ).key
    await details()
  })
const toggleKey = (key) =>
  action(async () => {
    await publicAppApi.setKey(selected.value.id, key.id, !key.enabled)
    await details()
  })
const toggleUser = (user) =>
  action(async () => {
    await publicAppApi.setEndUser(selected.value.id, user.uid, !user.enabled)
    await details()
  })
onMounted(() =>
  action(async () => {
    const [, options] = await Promise.all([load(), publicAppApi.options()])
    agentOptions.value = options.agents.map((item) => ({ value: item.id, label: item.name }))
    kbOptions.value = options.knowledge_bases.map((item) => ({ value: item.id, label: item.name }))
  })
)
</script>

<style scoped lang="less">
.public-app-settings {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--gray-150);
  p,
  small {
    color: var(--color-text-secondary);
  }
  .app-heading,
  .app-row,
  .credential-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .app-row {
    padding: 12px 0;
    border-bottom: 1px solid var(--gray-100);
    flex-wrap: wrap;
  }
}
.credential-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
}
.secret {
  overflow-wrap: anywhere;
  user-select: all;
}
</style>
