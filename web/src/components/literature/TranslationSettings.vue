<template>
  <section aria-label="翻译专项配置" class="translation-settings">
    <h4>翻译模型池（当前用户，服务端保存）</h4>
    <p>未启用专项配置时沿用我的主模型。仅引用 Yuxi 已登记的模型与加密凭据。</p>
    <label
      >活动配置
      <select v-model="activeProfileId" :disabled="busy">
        <option :value="null">我的主模型</option>
        <option v-for="profile in profiles" :key="profile.id" :value="profile.id">
          {{ profile.label || profile.model_spec }}
        </option>
      </select>
    </label>
    <fieldset v-for="profile in profiles" :key="profile.id" class="profile" :disabled="busy">
      <label>名称<input v-model="profile.label" maxlength="100" /></label>
      <label
        >翻译模型<select v-model="profile.model_spec">
          <option v-for="model in models" :key="model.spec" :value="model.spec">
            {{ model.label }} · {{ model.spec }}
          </option>
        </select></label
      >
      <label
        >单批正文字符<input
          v-model.number="profile.batch_chars"
          type="number"
          min="1000"
          max="48000"
          step="100"
      /></label>
      <label
        >推理强度<select v-model="profile.reasoning_effort">
          <option value="default">模型默认</option>
          <option value="none">关闭（供应商须支持）</option>
          <option value="low">低</option>
          <option value="medium">中</option>
          <option value="high">高</option>
        </select></label
      >
      <button class="btn" :disabled="busy" @click="remove(profile.id)">移除配置</button>
    </fieldset>
    <div class="actions">
      <button class="btn" :disabled="busy || !models.length || profiles.length >= 20" @click="add">
        添加翻译模型
      </button>
      <button class="btn" :disabled="busy" @click="save">保存翻译配置</button>
      <button class="btn" :disabled="busy" @click="load">重新加载</button>
    </div>
    <h4>真实语料自适应探测</h4>
    <p>
      3000 → 6000 → 12000 → 24000 → 48000
      字符，首次失败即停止。整段输入；语料不足不会冒充模型通过。建议值是最大通过值的
      60%，需确认保存。
    </p>
    <div class="actions">
      <label
        >探测配置<select v-model="probeProfileId" :disabled="busy">
          <option :value="null">当前活动模型</option>
          <option v-for="profile in profiles" :key="profile.id" :value="profile.id">
            {{ profile.label || profile.model_spec }}
          </option>
        </select></label
      >
      <label
        >真实来源<select v-model="sourceChoice" :disabled="busy">
          <option value="auto">当前有权已解析语料（按大小取最多 30 篇）</option>
          <option
            v-for="item in sources"
            :key="`${item.source_kind}:${item.source_ref}`"
            :value="`${item.source_kind}:${item.source_ref}`"
          >
            {{ item.title }}
          </option>
        </select></label
      >
      <button class="btn" :disabled="busy || running" @click="probe">确认费用并开始探测</button>
    </div>
    <p v-if="task">探测 #{{ task.task_id }}：{{ task.status }} · {{ task.model_spec }}</p>
    <ul v-if="task?.result?.trials">
      <li v-for="trial in task.result.trials" :key="trial.batch_chars">
        {{ trial.batch_chars }} 字符：{{ trial.status }}（实际 {{ trial.input_chars }} 字符）
      </li>
    </ul>
    <button
      v-if="task?.status === 'SUCCEEDED' && task.result?.suggested_batch_chars"
      class="btn"
      :disabled="busy"
      @click="applySuggestion"
    >
      确认将建议 {{ task.result.suggested_batch_chars }} 字符写入探测配置
    </button>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { literatureTranslationApi as api } from '@/apis/literature_api'

const profiles = ref([]),
  models = ref([]),
  sources = ref([])
const activeProfileId = ref(null),
  probeProfileId = ref(null),
  sourceChoice = ref('auto')
const busy = ref(false),
  notice = ref(''),
  task = ref(null)
const running = computed(() => ['PENDING', 'RUNNING'].includes(task.value?.status))
let disposed = false,
  generation = 0,
  pollGeneration = 0,
  timer = null
let controller = new AbortController()
const key = 'yuxi:literature:translation-probe:'
// Task IDs are harmless references; API always revalidates owner and source ACL.
import { useUserStore } from '@/stores/user'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'
const user = useUserStore()
const storageKey = () => key + user.userId
const load = async () => {
  if (disposed || !polling.value) return
  const ticket = ++generation
  busy.value = true
  try {
    const [settings, corpus] = await Promise.all([
      api.settings(controller.signal),
      api.probeSources(controller.signal)
    ])
    if (disposed || !polling.value || ticket !== generation) return
    profiles.value = settings.profiles
    models.value = settings.models
    activeProfileId.value = settings.active_profile_id
    sources.value = corpus.items || []
    notice.value = ''
  } catch (error) {
    if (!disposed && ticket === generation && error.name !== 'AbortError')
      notice.value = error.message
  } finally {
    if (!disposed && ticket === generation) busy.value = false
  }
}
const add = () =>
  profiles.value.push({
    id: `translation-${Date.now()}-${profiles.value.length}`,
    label: '',
    model_spec: models.value[0]?.spec || '',
    batch_chars: 12000,
    reasoning_effort: 'default'
  })
const remove = (id) => {
  profiles.value = profiles.value.filter((profile) => profile.id !== id)
  if (activeProfileId.value === id) activeProfileId.value = null
  if (probeProfileId.value === id) probeProfileId.value = null
}
const save = async () => {
  if (busy.value || disposed) return false
  busy.value = true
  try {
    const result = await api.saveSettings({
      profiles: profiles.value,
      active_profile_id: activeProfileId.value
    })
    if (!disposed) {
      profiles.value = result.profiles
      activeProfileId.value = result.active_profile_id
      notice.value = '已保存；正在执行的任务继续使用提交时冻结的配置。'
    }
    return true
  } catch (error) {
    if (!disposed) notice.value = error.message
    return false
  } finally {
    if (!disposed) busy.value = false
  }
}
const poll = async (id, ticket) => {
  if (disposed || !polling.value) return
  if (ticket === undefined) {
    ticket = ++pollGeneration
    window.clearTimeout(timer)
  }
  try {
    const result = await api.task(id, false, controller.signal)
    if (disposed || !polling.value || ticket !== pollGeneration) return
    task.value = result
    if (running.value) timer = window.setTimeout(() => poll(id, ticket), 5000)
  } catch (error) {
    if (!disposed && polling.value && ticket === pollGeneration && error.name !== 'AbortError') {
      notice.value = error.message
      if ([401, 403, 404, 409, 422].includes(error.response?.status)) {
        task.value = null
        try {
          window.localStorage.removeItem(storageKey())
        } catch {
          /* optional storage */
        }
      } else timer = window.setTimeout(() => poll(id, ticket), 10000)
    }
  }
}
const probe = async () => {
  if (busy.value || running.value || disposed) return
  if (
    !window.confirm('探测会向已配置的模型传输真实论文正文，最多调用 5 次并可能收费。允许这次探测？')
  )
    return
  // Persist edits without changing active selection, so the targeted nonactive
  // profile is the exact one the actor is currently inspecting.
  if (!(await save())) return
  busy.value = true
  try {
    const [source_kind, ...ref] = sourceChoice.value.split(':')
    const result = await api.probe({
      profile_id: probeProfileId.value,
      source_kind,
      source_ref: source_kind === 'auto' ? 'current' : ref.join(':'),
      cost_ack: true
    })
    if (disposed) return
    task.value = result
    try {
      window.localStorage.setItem(storageKey(), String(result.task_id))
    } catch {
      /* private storage may be disabled */
    }
    await poll(result.task_id)
  } catch (error) {
    if (!disposed) notice.value = error.message
  } finally {
    if (!disposed) busy.value = false
  }
}
const applySuggestion = async () => {
  if (
    busy.value ||
    disposed ||
    task.value?.status !== 'SUCCEEDED' ||
    !task.value.result?.suggested_batch_chars
  )
    return
  const id = task.value.options.id
  const profile = profiles.value.find((item) => item.id === id)
  if (!profile) {
    notice.value = '探测使用主模型或该配置已删除；请添加对应翻译配置后手动填写建议值。'
    return
  }
  if (
    profile.model_spec !== task.value.model_spec ||
    profile.reasoning_effort !== task.value.options.reasoning_effort
  ) {
    notice.value = '该配置的模型或推理强度已变更，旧建议不可套用。'
    return
  }
  profile.batch_chars = task.value.result.suggested_batch_chars
  await save()
}
const resume = async () => {
  await load()
  if (disposed || !polling.value) return
  try {
    const id = window.localStorage.getItem(storageKey())
    if (id && /^\d+$/.test(id)) await poll(id)
  } catch {
    /* not an authorization source */
  }
}
const pause = () => {
  generation++
  pollGeneration++
  controller.abort()
  controller = new AbortController()
  window.clearTimeout(timer)
  busy.value = false
}
const polling = useLiteraturePollingScope(pause, resume)
onMounted(resume)
onBeforeUnmount(() => {
  disposed = true
  pause()
  controller.abort()
})
</script>

<style lang="less" scoped>
.translation-settings {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.profile,
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.profile {
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  padding: 12px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
select {
  max-width: 320px;
}
p {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin: 0;
}
</style>
