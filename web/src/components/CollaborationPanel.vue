<template>
  <section class="collaboration-panel">
    <div class="panel-actions">
      <strong>协作状态</strong>
      <a-button size="small" :loading="loading" @click="load">刷新</a-button>
      <a-popconfirm title="停止整组协作？已完成的外部操作不会撤销。" @confirm="stopAll">
        <a-button size="small" danger :loading="stopping" :disabled="!activeMembers.length"
          >停止整组</a-button
        >
      </a-popconfirm>
    </div>
    <a-alert v-if="error" type="warning" :message="error" show-icon />
    <div
      v-for="member in visibleMembers"
      :key="member.run_id"
      class="member"
      :style="{ paddingLeft: `${depth(member) * 12}px` }"
    >
      <button
        type="button"
        class="member-link"
        :disabled="!member.is_subagent_member"
        @click="openMember(member)"
      >
        {{ member.agent_slug }}
      </button>
      <span>{{ statusLabel(member.effective_status || member.status) }}</span>
      <a-tag v-if="member.waiting_for_input" color="orange">待人工处理</a-tag>
      <a-button
        v-if="member.waiting_for_input && member.is_subagent_member"
        size="small"
        @click="openWaitpoint(member)"
        >处理问题</a-button
      >
    </div>
    <a-form v-if="activeMembers.length" layout="vertical" @finish="send">
      <a-select
        v-model:value="target"
        placeholder="选择接收引导的成员"
        :options="
          activeMembers.map((member) => ({
            value: member.run_id,
            label: `${member.agent_slug} · ${member.run_id.slice(0, 8)}`
          }))
        "
      />
      <a-textarea
        v-model:value="draft"
        :maxlength="8192"
        :auto-size="{ minRows: 2, maxRows: 6 }"
        placeholder="引导将在下一次模型执行边界送达"
      />
      <a-button
        size="small"
        html-type="submit"
        :loading="sending"
        :disabled="!target || !draft.trim()"
        >发送引导</a-button
      >
    </a-form>
    <details v-if="messages.length">
      <summary>协作消息（{{ messages.length }}）</summary>
      <div v-for="item in messages" :key="item.id" class="guidance">
        <small
          >{{ item.sender_kind === 'user' ? '用户引导' : '成员消息' }} →
          {{ memberName(item.target_run_id) }} · {{ messageStatus(item) }}</small
        >
        <p>{{ item.body }}</p>
      </div>
    </details>
    <a-modal
      v-model:open="waitpointOpen"
      title="成员需要你的输入"
      :footer="null"
      :destroy-on-close="true"
      @cancel="waitpointOpen = false"
    >
      <HumanApprovalModal
        v-if="waitpoint"
        ref="waitpointForm"
        :visible="true"
        :questions="waitpoint.questions"
        @submit="resumeWaitpoint"
        @cancel="resumeWaitpoint('reject')"
      />
    </a-modal>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { agentApi } from '@/apis/agent_api'
import HumanApprovalModal from '@/components/HumanApprovalModal.vue'

const props = defineProps({ runId: { type: String, required: true } })
const emit = defineEmits(['open-member'])
const members = ref([]),
  messages = ref([]),
  error = ref('')
const loading = ref(false),
  sending = ref(false),
  stopping = ref(false)
const target = ref(),
  draft = ref('')
const waitpoint = ref(null),
  waitpointOpen = ref(false),
  waitpointForm = ref(null)
let pendingAnswer
const visibleMembers = computed(() =>
  members.value.filter(
    (member) => !member.current_run_id || member.current_run_id === member.run_id
  )
)
const activeMembers = computed(() =>
  visibleMembers.value.filter(
    (member) => !member.stopped && ['pending', 'running', 'interrupted'].includes(member.status)
  )
)
function messageStatus(item) {
  if (item.delivered_at) return '已进入执行边界'
  const receiver = members.value.find((member) => member.run_id === item.target_run_id)
  if (receiver?.stopped) return '成员已停止'
  if (receiver?.status === 'interrupted') return '待恢复后接收'
  if (receiver && !['pending', 'running'].includes(receiver.status)) return '本轮已结束'
  return '等待接收'
}
let generation = 0,
  timer,
  pendingMessage
const statusLabel = (status) =>
  ({
    pending: '排队',
    running: '执行中',
    interrupted: '等待输入',
    completed: '已完成',
    failed: '失败',
    cancelled: '已停止',
    cancel_requested: '正在停止'
  })[status] || status
const memberName = (runId) =>
  members.value.find((member) => member.run_id === runId)?.agent_slug || runId
function depth(member) {
  const seen = new Set()
  let count = 0,
    current = member
  while (current?.parent_run_id && count < 16 && !seen.has(current.run_id)) {
    seen.add(current.run_id)
    if (current.run_type === 'subagent') count++
    current = members.value.find((item) => item.run_id === current.parent_run_id)
  }
  return count
}
function openMember(member) {
  emit('open-member', {
    ...member,
    child_thread_id: member.thread_id,
    subagent_slug: member.agent_slug
  })
}
async function load() {
  if (!props.runId || loading.value) return
  const ticket = generation,
    runId = props.runId
  loading.value = true
  try {
    const result = await agentApi.getCollaboration(runId)
    if (ticket !== generation || runId !== props.runId) return
    members.value = result.members || []
    messages.value = result.messages || []
    error.value = ''
    if (!activeMembers.value.some((item) => item.run_id === target.value)) target.value = undefined
  } catch (cause) {
    if (ticket === generation) error.value = cause.message || '读取协作状态失败'
  } finally {
    loading.value = false
  }
}
async function send() {
  const runId = props.runId,
    body = draft.value.trim(),
    receiver = target.value
  if (!runId || !body || !receiver) return
  sending.value = true
  try {
    const key = JSON.stringify([runId, receiver, body])
    if (pendingMessage?.key !== key) pendingMessage = { key, id: crypto.randomUUID() }
    await agentApi.sendCollaborationMessage(runId, {
      target_run_id: receiver,
      message: body,
      request_id: pendingMessage.id
    })
    pendingMessage = null
    if (runId === props.runId) {
      if (draft.value.trim() === body && target.value === receiver) draft.value = ''
      await load()
    }
  } catch (cause) {
    message.error(cause.message || '引导发送失败')
  } finally {
    sending.value = false
  }
}
async function stopAll() {
  stopping.value = true
  try {
    await agentApi.stopCollaboration(props.runId)
    await load()
  } catch (cause) {
    message.error(cause.message || '停止失败')
  } finally {
    stopping.value = false
  }
}
async function openWaitpoint(member) {
  try {
    const point = await agentApi.getCollaborationWaitpoint(member.run_id)
    waitpoint.value = { ...point, member }
    pendingAnswer = null
    waitpointOpen.value = true
  } catch (cause) {
    message.error(cause.message || '读取问题失败')
    await load()
  }
}
async function resumeWaitpoint(answer) {
  if (!waitpoint.value) return
  const point = waitpoint.value
  try {
    const key = JSON.stringify([point.run_id, point.revision, answer])
    if (pendingAnswer?.key !== key) pendingAnswer = { key, id: crypto.randomUUID() }
    const receipt = await agentApi.resumeCollaborationWaitpoint(point.run_id, {
      revision: point.revision,
      request_id: pendingAnswer.id,
      answer
    })
    waitpointOpen.value = false
    pendingAnswer = null
    await load()
    openMember({
      ...point.member,
      run_id: receipt.run_id,
      thread_id: point.thread_id,
      status: 'pending'
    })
  } catch (cause) {
    message.error(cause.message || '提交失败，回答已保留，可重试')
    waitpointForm.value?.resetSubmission()
    await load()
  }
}
watch(
  () => props.runId,
  () => {
    generation++
    waitpointOpen.value = false
    members.value = []
    messages.value = []
    draft.value = ''
    target.value = undefined
    loading.value = false
    void load()
  },
  { immediate: true }
)
onMounted(() => {
  timer = setInterval(() => {
    if (!document.hidden) void load()
  }, 5000)
})
onBeforeUnmount(() => {
  generation++
  clearInterval(timer)
})
</script>

<style scoped lang="less">
.collaboration-panel {
  padding: 12px 0;
}
.panel-actions,
.member {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.member-link {
  border: 0;
  background: transparent;
  color: var(--main-color);
  cursor: pointer;
}
.member-link:disabled {
  color: inherit;
  cursor: default;
}
.ant-select {
  width: 100%;
}
textarea {
  margin: 8px 0;
}
.guidance {
  border-top: 1px solid var(--gray-150);
  padding: 8px 0;
}
.guidance p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
