<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { publicSessionApi as api } from '@/apis/public_session_api'
import { useUserStore } from '@/stores/user'
import ChatMessageContent from '@/components/widgets/ChatMessageContent.vue'
import { formatMentionToken } from '@/utils/mention_utils'

const route = useRoute()
const router = useRouter()
const user = useUserStore()
const messagesElement = ref(null), mentionAgents = ref([])
const sidebarPinned = ref(localStorage.getItem(`yuxi:sidebar:${user.uid}`) !== 'false'), sidebarOpen = ref(false)
let restoringPosition = false, followBottom = true, readingTimer
const agents = ref([]),
  projects = ref([]),
  sessions = ref([]),
  selectedAgent = ref(''),
  selectedProject = ref('')
const session = ref(null),
  items = ref([]),
  queue = ref([]),
  collaborators = ref([]),
  files = ref([]),
  attachments = ref([])
const draft = ref(''),
  uploads = ref([]),
  images = ref([]),
  busy = ref(false),
  error = ref(''),
  connection = ref('未连接')
const search = ref(''),
  nextPage = ref(null),
  answers = ref({}),
  otherAnswers = ref({}),
  decisions = ref({})
const mode = ref('follow_up'),
  approval = ref('always_trust'),
  pending = ref(null)
let subscription,
  refreshTimer,
  generation = 0
const sid = computed(() => String(route.params.session_id || ''))
const turn = computed(() => session.value?.yuxi?.current_turn)
const waitpoint = computed(() => turn.value?.waitpoint)
const active = computed(() => ['in_progress', 'requires_action'].includes(session.value?.status))
const draftKey = (id) => `yuxi:session-draft:${user.uid}:${id}`
const cursorKey = (id) => draftKey(id) + ':cursor'
const persistDraft = () => localStorage.setItem(draftKey(sid.value), draft.value)
function pinSidebar() {
  sidebarPinned.value = !sidebarPinned.value
  localStorage.setItem(`yuxi:sidebar:${user.uid}`, String(sidebarPinned.value))
  sidebarOpen.value = true
}
function saveReading(id = sid.value) {
  const element = messagesElement.value
  if (!id || !element || restoringPosition) return
  const top = element.getBoundingClientRect().top
  const anchor = [...element.querySelectorAll('[data-item-id]')].find(item => item.getBoundingClientRect().bottom > top)
  localStorage.setItem(draftKey(id) + ':reading', JSON.stringify({ top: element.scrollTop, bottom: followBottom,
    count: Math.min(items.value.length, 5000), anchor: anchor?.dataset.itemId, offset: anchor ? anchor.getBoundingClientRect().top - top : 0 }))
}
function onReadingScroll() {
  const element = messagesElement.value
  if (!element || restoringPosition) return
  followBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 48
  clearTimeout(readingTimer)
  const id = sid.value
  readingTimer = setTimeout(() => saveReading(id), 120)
}
async function restoreReading(id, version) {
  const stored = localStorage.getItem(draftKey(id) + ':reading')
  let reading
  try { reading = stored ? JSON.parse(stored) : null } catch { localStorage.removeItem(draftKey(id) + ':reading') }
  while (reading?.anchor && items.value.length < Math.min(Number(reading.count) || 0, 5000) && nextPage.value) {
    const page = await api.items(id, nextPage.value)
    if (version !== generation) return
    const known = new Set(items.value.map(item => item.id))
    items.value.unshift(...page.data.reverse().filter(item => !known.has(item.id)))
    nextPage.value = page.has_more ? page.last_id : null
  }
  await nextTick()
  if (version !== generation || !messagesElement.value) return
  const element = messagesElement.value
  followBottom = reading?.bottom ?? true
  if (followBottom) element.scrollTop = element.scrollHeight
  else {
    const anchor = [...element.querySelectorAll('[data-item-id]')].find(item => item.dataset.itemId === reading?.anchor)
    if (anchor) element.scrollTop += anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - reading.offset
    else element.scrollTop = Number(reading?.top) || 0
  }
}
watch(() => [items.value.length, JSON.stringify(items.value.at(-1)?.content)], async () => {
  await nextTick()
  if (followBottom && !restoringPosition && messagesElement.value) messagesElement.value.scrollTop = messagesElement.value.scrollHeight
})
const report = (e) => {
  error.value = e.message || String(e)
}
async function action(fn) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    return await fn()
  } catch (e) {
    report(e)
  } finally {
    busy.value = false
  }
}
async function loadDirectory() {
  const [a, p, s] = await Promise.all([api.agents(), api.projects(), api.sessions()])
  agents.value = a.data
  projects.value = p.data
  sessions.value = s.data
  if (!selectedAgent.value) selectedAgent.value = a.data[0]?.id || ''
}
async function createSession() {
  await action(async () => {
    const created = await api.create(
      {
        agent_id: selectedAgent.value,
        ...(selectedProject.value ? { project_id: selectedProject.value } : {}),
        tool_approval_mode: approval.value
      },
      crypto.randomUUID()
    )
    await loadDirectory()
    await router.push('/sessions/' + created.id)
  })
}
async function createProject() {
  await action(async () => {
    const project = await api.createProject('')
    projects.value.unshift(project)
    selectedProject.value = project.id
  })
}
function mergeItems(data) {
  const byId = new Map(items.value.map((item) => [item.id, item]))
  for (const item of data) byId.set(item.id, item)
  items.value = [...byId.values()]
}
async function refresh(id = sid.value, version = generation) {
  const [s, q, c, a, page] = await Promise.all([
    api.session(id),
    api.queue(id),
    api.cooperation(id),
    api.attachments(id),
    api.items(id)
  ])
  if (id !== sid.value || version !== generation) return
  session.value = s
  queue.value = q.inputs
  collaborators.value = c.sessions
  attachments.value = a.attachments
  const oldPageIds = new Set(page.data.map((item) => item.id))
  const historical = items.value.filter((item) => item.yuxi?.message_id && !oldPageIds.has(item.id))
  const streaming = items.value.filter((item) => !item.yuxi?.message_id && !oldPageIds.has(item.id))
  items.value = [...historical, ...page.data.reverse(), ...streaming]
}
function scheduleRefresh(id, version) {
  if (refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    refresh(id, version).catch(report)
  }, 250)
}
watch(
  sid,
  async (id, previous) => {
    if (previous) saveReading(previous)
    clearTimeout(readingTimer)
    restoringPosition = true
    generation += 1
    const version = generation
    subscription?.abort()
    clearTimeout(refreshTimer)
    refreshTimer = null
    session.value = null
    items.value = []
    queue.value = []
    collaborators.value = []
    attachments.value = []
    files.value = []
    mentionAgents.value = []
    uploads.value = []
    images.value = []
    answers.value = {}
    otherAnswers.value = {}
    decisions.value = {}
    pending.value = null
    error.value = ''
    draft.value = localStorage.getItem(draftKey(id)) || ''
    if (!id) { restoringPosition = false; return }
    subscription = new AbortController()
    const signal = subscription.signal
    try {
      const [history] = await Promise.all([api.items(id), refresh(id, version)])
      if (version !== generation) return
      items.value = history.data.reverse()
      nextPage.value = history.has_more ? history.last_id : null
      await restoreReading(id, version)
      if (version !== generation) return
      restoringPosition = false
      api.mentionAgents(id).then(result => { if (version === generation) mentionAgents.value = result.data })
        .catch(e => { if (version === generation && e.status !== 404) report(e) })
      await api.viewed(id)
      api
        .subscribe(id, {
          signal,
          after: localStorage.getItem(cursorKey(id)),
          onConnection: (value) => {
            if (version === generation) connection.value = value
          },
          onEvent: ({ id: cursor, data }) => {
            if (version !== generation) return
            if (data.type.endsWith('item.done') && data.item) mergeItems([data.item])
            if (data.type.endsWith('output_text.delta')) {
              let item = items.value.find((item) => item.id === data.item_id)
              if (!item) {
                item = {
                  id: data.item_id,
                  type: 'message',
                  role: 'assistant',
                  status: 'in_progress',
                  content: [{ type: 'output_text', text: '' }]
                }
                items.value.push(item)
              }
              // History may already contain this completed item when replaying an older cursor.
              if (item.status !== 'completed') item.content[0].text += data.delta
            }
            if (cursor) localStorage.setItem(cursorKey(id), cursor)
            scheduleRefresh(id, version)
          }
        })
        .catch((e) => {
          if (!signal.aborted) {
            connection.value = '连接已停止'
            report(e)
          }
        })
    } catch (e) {
      restoringPosition = false
      if (version === generation) report(e)
    }
  },
  { immediate: true }
)
async function older() {
  await action(async () => {
    const id = sid.value,
      history = await api.items(id, nextPage.value)
    if (id !== sid.value) return
    const known = new Set(items.value.map((item) => item.id))
    items.value.unshift(...history.data.reverse().filter((item) => !known.has(item.id)))
    nextPage.value = history.has_more ? history.last_id : null
  })
}
async function control(event) {
  await action(async () => {
    await api.event(sid.value, event, crypto.randomUUID())
    await refresh()
  })
}
async function send() {
  await action(async () => {
    const id = sid.value
    if (!pending.value) {
      if (!draft.value.trim() && !images.value.length) return
      pending.value = {
        key: crypto.randomUUID(),
        event: {
          type: 'agent.session.input.message',
          input: [
            {
              role: 'user',
              content: [
                ...(draft.value.trim() ? [{ type: 'input_text', text: draft.value }] : []),
                ...images.value.map((image) => ({ type: 'input_image', image_url: image.url }))
              ]
            }
          ],
          yuxi: {
            mode: mode.value,
            ...(mode.value === 'follow_up'
              ? {
                  attachment_file_ids: uploads.value.map((file) => file.id),
                  tool_approval_mode: approval.value
                }
              : {})
          }
        }
      }
    }
    await api.event(id, pending.value.event, pending.value.key)
    if (id !== sid.value) return
    pending.value = null
    draft.value = ''
    uploads.value = []
    images.value = []
    persistDraft()
    const page = await api.items(id)
    mergeItems(page.data.reverse())
    await refresh()
  })
}
async function upload(event) {
  const chosen = [...event.target.files]
  event.target.value = ''
  await action(async () => {
    for (const file of chosen) {
      if (file.type.startsWith('image/')) {
        if (images.value.length >= 10) throw new Error('每条消息最多 10 张图片')
        const url = await new Promise((resolve, reject) => {
          const reader = new FileReader()
          reader.onload = () => resolve(reader.result)
          reader.onerror = reject
          reader.readAsDataURL(file)
        })
        images.value.push({ name: file.name, url })
      } else uploads.value.push(await api.upload(file))
    }
  })
}
async function resume() {
  const point = waitpoint.value
  const response =
    point.kind === 'approval'
      ? {
          type: 'approval',
          decisions: point.calls.map((call) => ({
            call_id: call.call_id,
            decision: decisions.value[call.call_id] || 'reject'
          }))
        }
      : {
          type: 'answer',
          answers: point.questions.map((question) => ({
            question_id: question.question_id,
            answer: otherAnswers.value[question.question_id]?.trim()
              ? {
                  type: 'other',
                  text: otherAnswers.value[question.question_id],
                  selected: [].concat(answers.value[question.question_id] || [])
                }
              : answers.value[question.question_id] || ''
          }))
        }
  await control({
    type: 'yuxi.session.input.resume',
    turn_id: turn.value.id,
    waitpoint_id: point.id,
    response
  })
}
async function browse(path) {
  await action(async () => {
    files.value = (await api.files(sid.value, path)).files
  })
}
async function download(entry) {
  await action(async () => {
    const blob = await api.download(entry.artifact_url || entry.download_url)
    const url = URL.createObjectURL(blob),
      anchor = document.createElement('a')
    anchor.href = url
    anchor.download = entry.name || entry.filename || entry.file_name
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
}
async function findSessions() {
  await action(async () => {
    sessions.value = search.value.trim()
      ? (await api.search(search.value)).items.map((item) => item.session)
      : (await api.sessions()).data
  })
}
async function folderUpload(event) {
  const chosen = [...event.target.files],
    id = sid.value
  event.target.value = ''
  await action(async () => {
    if (chosen.length > 50) throw new Error('每次最多上传 50 个文件')
    for (const file of chosen) {
      const saved = await api.uploadFolderFile(id, file, file.webkitRelativePath || file.name)
      if (id !== sid.value) return
      draft.value = [draft.value, `项目文件：${saved.path}`].filter(Boolean).join('\n')
      persistDraft()
    }
    files.value = (await api.files(id)).files
  })
}
async function widgetAction(value) {
  if (busy.value || pending.value) throw new Error('当前提交尚未完成')
  if (typeof value === 'string') {
    draft.value = [draft.value, value].filter(Boolean).join('\n')
    persistDraft()
    return true
  }
  if (value?.type !== 'send_message' || !value.text?.trim()) throw new Error('组件动作无效')
  draft.value = value.text
  mode.value = 'follow_up'
  persistDraft()
  await send()
  if (pending.value || error.value) throw new Error(error.value || '提交未确认')
  value.onAccepted?.()
  return true
}
async function widgetImage(path) {
  const blob = await api.download(
    `/api/v1/agents/sessions/${sid.value}/artifacts/${path.replace(/^\/+/, '')}`
  )
  return { blob: async () => blob }
}
onMounted(() => loadDirectory().catch(report))
onBeforeUnmount(() => {
  saveReading()
  clearTimeout(readingTimer)
  generation += 1
  subscription?.abort()
  clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="public-sessions" :class="{ 'floating-navigation': !sidebarPinned }">
    <button v-if="!sidebarPinned" class="navigation-trigger" aria-controls="session-navigation" :aria-expanded="sidebarOpen" @mouseenter="sidebarOpen = true" @focus="sidebarOpen = true" @click="sidebarOpen = !sidebarOpen">会话列表</button>
    <aside id="session-navigation" v-show="sidebarPinned || sidebarOpen" class="session-list" @mouseleave="sidebarOpen = false">
      <button @click="pinSidebar">{{ sidebarPinned ? '取消固定侧栏' : '固定侧栏' }}</button>
      <h2>协作会话</h2>
      <label
        >智能体<select v-model="selectedAgent">
          <option v-for="agent in agents" :key="agent.id" :value="agent.id">
            {{ agent.name }}
          </option>
        </select></label
      >
      <label
        >项目<select v-model="selectedProject">
          <option value="">自动新建项目</option>
          <option v-for="project in projects" :key="project.id" :value="project.id">
            {{ project.name }}
          </option>
        </select></label
      >
      <button :disabled="busy" @click="createProject">新建项目</button>
      <button :disabled="busy || !selectedAgent" @click="createSession">新建会话</button>
      <form @submit.prevent="findSessions">
        <input v-model="search" aria-label="搜索会话" placeholder="搜索会话与消息" /><button
          :disabled="busy"
        >
          搜索
        </button>
      </form>
      <RouterLink
        v-for="entry in sessions"
        :key="entry.id"
        :to="'/sessions/' + entry.id"
        :class="{ selected: entry.id === sid }"
        >{{ entry.yuxi.title || entry.agent.id
        }}<small>{{ entry.status }} {{ entry.yuxi.unread ? '· 未读' : '' }}</small></RouterLink
      >
    </aside>
    <main>
      <p v-if="error" class="error" role="alert">
        {{ error }}<button @click="error = ''">关闭</button>
      </p>
      <template v-if="session">
        <header>
          <strong>{{ session.yuxi.title || session.agent.id }}</strong
          ><span>{{ session.status }} · {{ connection }}</span>
          <button
            :disabled="busy || !active"
            @click="
              control({
                type: 'agent.session.input.cancel',
                yuxi: { turn_id: turn.id, expected_run_id: turn.current_run_id }
              })
            "
          >
            停止本轮
          </button>
          <button :disabled="busy" @click="control({ type: 'yuxi.session.tree.stop' })">
            停止协作组
          </button>
          <button :disabled="busy" @click="control({ type: 'yuxi.session.tree.continue' })">
            继续协作组
          </button>
          <button
            :disabled="busy || active || queue.length > 0"
            @click="
              action(async () => {
                await api.compress(sid, AbortSignal.timeout(100000))
                await refresh()
              })
            "
          >
            压缩上下文
          </button>
        </header>
        <section ref="messagesElement" class="messages" aria-live="polite" @scroll.passive="onReadingScroll">
          <button v-if="nextPage" :disabled="busy" @click="older">加载更早的消息</button>
          <article v-for="item in items" :key="item.id" :class="item.role" :data-item-id="item.id">
            <template v-if="item.type === 'message'"
              ><small>{{ item.role === 'user' ? '你' : '智能体' }} · {{ item.status }}</small>
              <template v-for="(part, index) in item.content" :key="index"
                ><ChatMessageContent
                  v-if="part.text"
                  :content="part.text"
                  :action-handler="widgetAction"
                  :image-loader="widgetImage"
                  :disabled="busy || !!pending" /><img
                  v-else-if="part.type === 'input_image'"
                  :src="part.image_url"
                  alt="用户上传的图片"
              /></template>
            </template>
            <details v-else>
              <summary>
                {{ item.type === 'function_call' ? '调用工具' : '工具结果' }}
                {{ item.name || item.call_id }}
              </summary>
              <pre>{{ item.arguments || item.output }}</pre>
            </details>
          </article>
          <section v-if="waitpoint" class="waiting">
            <h3>{{ waitpoint.kind === 'approval' ? '工具操作审批' : '智能体需要你的回答' }}</h3>
            <label v-for="call in waitpoint.calls || []" :key="call.call_id"
              ><strong>{{ call.name || call.tool_name }}</strong>
              <pre>{{ call.args || call.arguments }}</pre>
              <select v-model="decisions[call.call_id]">
                <option value="reject">拒绝</option>
                <option value="approve">批准</option>
              </select></label
            >
            <label v-for="question in waitpoint.questions || []" :key="question.question_id"
              >{{ question.question || question.title || question.text }}
              <select
                v-if="question.options?.length"
                v-model="answers[question.question_id]"
                :multiple="question.multi_select"
              >
                <option
                  v-for="option in question.options"
                  :key="option.value"
                  :value="option.value"
                >
                  {{ option.label }}
                </option>
              </select>
              <input
                v-if="question.allow_other || !question.options?.length"
                v-model="otherAnswers[question.question_id]"
                placeholder="补充回答"
              />
            </label>
            <button :disabled="busy" @click="resume">提交并继续</button>
          </section>
        </section>
        <section class="composer">
          <p v-if="pending">提交结果尚未确认。点击重试会复用原提交标识，避免重复执行。</p>
          <a-mentions
            v-model:value="draft"
            :options="mentionAgents.map(agent => ({ value: formatMentionToken('subagent', agent.id).slice(1), label: agent.name }))"
            :rows="3"
            :disabled="busy || !!pending"
            aria-label="会话输入"
            placeholder="追加任务或调整运行方向；Enter 换行"
            @change="persistDraft"
          />
          <div>
            <select v-model="mode" :disabled="!!pending">
              <option value="follow_up">追加任务 · 顺序排队</option>
              <option value="steer">优先引导 · 安全边界生效</option>
            </select>
            <select v-model="approval" :disabled="!!pending">
              <option value="always_trust">直接执行工具</option>
              <option value="always_ask">工具操作先审批</option>
            </select>
            <label class="file-picker"
              >添加图片或附件<input
                type="file"
                multiple
                :disabled="busy || !!pending || mode === 'steer'"
                @change="upload"
            /></label>
            <button
              :disabled="
                busy ||
                (!draft.trim() && !images.length && !pending) ||
                (mode === 'steer' && (uploads.length > 0 || images.length > 0))
              "
              @click="send"
            >
              {{ pending ? '重试提交' : active ? '提交到会话' : '发送' }}
            </button>
          </div>
          <span v-for="(file, index) in uploads" :key="file.id"
            >{{ file.filename }}
            <button :disabled="!!pending" @click="uploads.splice(index, 1)">移除</button></span
          >
          <span v-for="(image, index) in images" :key="index"
            >{{ image.name }}
            <button :disabled="!!pending" @click="images.splice(index, 1)">移除</button></span
          >
        </section>
      </template>
      <p v-else>选择智能体和项目，新建会话。项目中的会话与协作成员共享文件。</p>
    </main>
    <aside v-if="session" class="task-panel">
      <h3>待处理输入</h3>
      <button
        v-if="session.yuxi.queue_paused"
        :disabled="busy"
        @click="control({ type: 'yuxi.session.input.continue' })"
      >
        继续队列
      </button>
      <div v-for="input in queue" :key="input.input_id">
        <p>{{ input.kind === 'steer' ? '优先引导' : '排队任务' }} · {{ input.content }}</p>
        <small>{{ input.attachment_status }} {{ input.attachment_error }}</small
        ><button
          :disabled="busy"
          @click="control({ type: 'yuxi.session.input.cancel_input', input_id: input.input_id })"
        >
          取消</button
        ><button
          :disabled="busy || input.kind === 'steer'"
          @click="control({ type: 'yuxi.session.input.promote', input_id: input.input_id })"
        >
          转为引导
        </button>
      </div>
      <h3>协作成员</h3>
      <RouterLink
        v-for="member in collaborators"
        :key="member.session_id"
        :to="'/sessions/' + member.session_id"
        >{{ member.parent_session_id ? '↳' : '' }} {{ member.agent_id }}
        {{ member.tree_paused ? '已暂停' : '' }}</RouterLink
      >
      <h3>项目文件</h3>
      <label
        >上传文件夹<input
          type="file"
          webkitdirectory
          multiple
          :disabled="busy || !!pending"
          @change="folderUpload"
      /></label>
      <button :disabled="busy" @click="browse()">根目录 / 刷新</button>
      <div v-for="file in files" :key="file.path">
        <button :disabled="busy" @click="file.is_dir ? browse(file.path) : download(file)">
          {{ file.is_dir ? '目录' : '下载' }} {{ file.name }}</button
        ><button
          v-if="!file.is_dir"
          :disabled="busy"
          @click="action(() => api.save(sid, file.path))"
        >
          保存到工作区
        </button>
      </div>
      <h3>本会话附件</h3>
      <div v-for="file in attachments" :key="file.id">
        <button :disabled="busy" @click="download(file)">{{ file.filename }}</button
        ><button
          :disabled="busy || active || queue.length > 0"
          @click="
            action(async () => {
              await api.deleteAttachment(sid, file.id)
              await refresh()
            })
          "
        >
          删除
        </button>
      </div>
    </aside>
  </div>
</template>

<style scoped lang="less">
.public-sessions {
  position: relative;
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 260px;
  height: 100%;
  min-height: 0;
  color: var(--color-text-primary);
  background: var(--color-bg-container);
}
.floating-navigation { grid-template-columns: minmax(0, 1fr) 260px;
  .session-list { position: absolute; left: 0; top: 38px; bottom: 0; width: 260px; z-index: 20; box-shadow: 0 8px 24px var(--gray-200); }
  main { padding-top: 42px; }
}
.navigation-trigger { position: absolute; left: 8px; top: 4px; z-index: 21; }
aside {
  padding: 16px;
  overflow: auto;
  background: var(--color-bg-elevated);
}
main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  padding: 16px;
  gap: 12px;
}
label {
  display: block;
  margin: 8px 0;
}
select,
input,
textarea,
button {
  font: inherit;
  color: inherit;
  background: var(--color-bg-container);
  border: 1px solid var(--color-field-border);
  border-radius: 6px;
  padding: 6px 8px;
  max-width: 100%;
}
select,
label > input {
  display: block;
  width: 100%;
}
button {
  cursor: pointer;
  margin: 3px;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
a {
  display: block;
  padding: 10px 6px;
  color: inherit;
  overflow-wrap: anywhere;
}
a.selected {
  background: var(--main-100);
}
small {
  display: block;
  color: var(--color-text-secondary);
}
h2,
h3 {
  margin: 12px 0;
}
header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
header span {
  font-size: 12px;
}
.messages {
  flex: 1;
  overflow: auto;
  min-height: 0;
}
article {
  margin-bottom: 18px;
  padding: 12px;
  border-radius: 8px;
  background: var(--color-bg-elevated);
  overflow-wrap: anywhere;
}
article.user {
  margin-left: 10%;
}
article img {
  max-height: 280px;
  max-width: 100%;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 240px;
  overflow: auto;
}
.composer textarea {
  width: 100%;
  min-height: 100px;
  resize: vertical;
}
.composer > div {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.composer select {
  width: auto;
}
.file-picker input {
  width: 180px;
}
.waiting {
  padding: 12px;
  background: var(--color-bg-elevated);
}
.error {
  padding: 10px;
  color: var(--color-error, #b42318);
  background: var(--color-bg-elevated);
}
@media (max-width: 1100px) {
  .public-sessions {
    grid-template-columns: 190px minmax(0, 1fr);
  }
  .task-panel {
    grid-column: 1 / -1;
    max-height: 260px;
  }
}
@media (max-width: 650px) {
  .public-sessions {
    grid-template-columns: minmax(0, 1fr);
    overflow: auto;
  }
  .session-list {
    max-height: 220px;
  }
  main {
    min-height: 600px;
  }
}
</style>
