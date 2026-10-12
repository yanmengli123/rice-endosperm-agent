<template>
  <div
    ref="panelRef"
    class="floating-panel"
    :class="{ sheet: isMobile, 'reduced-motion': prefersReducedMotion }"
    :style="isMobile ? null : panelStyle"
    role="dialog"
    aria-label="快捷问答"
  >
    <header class="panel-header">
      <span class="panel-agent">
        <span class="panel-agent-dot" aria-hidden="true"></span>
        <span class="panel-agent-name" :title="agentName">{{ agentName }}</span>
      </span>
      <span class="panel-actions">
        <button
          type="button"
          class="lucide-icon-btn panel-action-btn"
          title="新问题（新开快问会话）"
          :disabled="isStreaming || isSending"
          @click.stop="$emit('new-thread')"
        >
          <MessageSquarePlus :size="16" />
        </button>
        <button
          v-if="hasThread"
          type="button"
          class="lucide-icon-btn panel-action-btn"
          title="在主界面打开（保留全部上下文）"
          @click.stop="$emit('open-main')"
        >
          <SquareArrowOutUpRight :size="16" />
        </button>
        <button
          type="button"
          class="lucide-icon-btn panel-action-btn"
          title="收起"
          aria-label="收起快捷问答"
          @click.stop="$emit('close')"
        >
          <X :size="16" />
        </button>
      </span>
    </header>

    <div ref="listRef" class="panel-body">
      <div v-if="!items.length" class="panel-empty">
        <Sparkles :size="20" class="panel-empty-icon" aria-hidden="true" />
        <p class="panel-empty-title">快捷问答</p>
        <p class="panel-empty-hint">任意提问，不打扰当前会话</p>
      </div>

      <template v-for="item in items" :key="item.id">
        <div v-if="item.role === 'user'" class="panel-msg user">
          <div class="panel-msg-bubble-user">{{ item.content }}</div>
        </div>

        <div v-else-if="item.role === 'assistant'" class="panel-msg assistant">
          <div v-if="item.content" class="panel-msg-body">
            <MarkdownPreview :content="item.content" compact />
          </div>
          <div v-if="item.streaming && !item.content && !statusText" class="panel-status">
            <span class="panel-status-dot" aria-hidden="true"></span>思考中…
          </div>
          <div v-if="item.error" class="panel-error">
            <span>回答失败</span>
            <button type="button" class="panel-error-retry" @click="$emit('retry')">
              <RotateCcw :size="13" /> 重试
            </button>
          </div>
        </div>

        <div v-else class="panel-notice">
          <span>{{ item.content }}</span>
          <button
            v-if="item.action === 'open-main' && hasThread"
            type="button"
            class="panel-notice-link"
            @click="$emit('open-main')"
          >
            前往主界面
            <SquareArrowOutUpRight :size="12" />
          </button>
        </div>
      </template>

      <div v-if="statusText && (isStreaming || isSending)" class="panel-status" role="status">
        <span class="panel-status-dot" aria-hidden="true"></span>{{ statusText }}
      </div>
    </div>

    <footer class="panel-footer">
      <MessageInputComponent
        ref="inputRef"
        v-model="draft"
        :is-loading="isStreaming || isSending"
        :disabled="!agentReady"
        :placeholder="agentReady ? '快捷提问…' : '智能体加载中…'"
        :auto-size="{ minRows: 1, maxRows: 4 }"
        class="panel-input"
        @send="handleSendOrStop"
        @keydown="handleKeyDown"
      />
    </footer>
  </div>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import { MessageSquarePlus, RotateCcw, Sparkles, SquareArrowOutUpRight, X } from '@lucide/vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import MessageInputComponent from '@/components/MessageInputComponent.vue'

/**
 * 悬浮球快捷问答面板（纯视图）。
 * 问答引擎在宿主 AgentFloatingBall 的 useFloatingChat 中，面板只消费 props/抛事件，
 * 这样收起面板不会中断进行中的流（球本体保持挂载）。
 */
const props = defineProps({
  items: { type: Array, default: () => [] },
  isStreaming: { type: Boolean, default: false },
  isSending: { type: Boolean, default: false },
  statusText: { type: String, default: '' },
  hasThread: { type: Boolean, default: false },
  agentName: { type: String, default: '智能体' },
  agentReady: { type: Boolean, default: true },
  isMobile: { type: Boolean, default: false },
  prefersReducedMotion: { type: Boolean, default: false },
  panelStyle: { type: Object, default: () => null }
})

const emit = defineEmits(['send', 'stop', 'retry', 'new-thread', 'open-main', 'close'])

const draft = ref('')
const inputRef = ref(null)
const listRef = ref(null)

// 与主聊天同语义：流式中的「发送」即停止（MessageInputComponent 单一 send 事件）
const handleSendOrStop = () => {
  if (props.isStreaming || props.isSending) {
    emit('stop')
    return
  }
  const text = draft.value.trim()
  if (!text) return
  emit('send', text)
  draft.value = ''
}

// MessageInputComponent 的裸 Enter 只向外抛 keydown（AgentInputArea 同款在上层实现发送）
const handleKeyDown = (event) => {
  if (props.isStreaming || props.isSending) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    handleSendOrStop()
  }
}

const focusInput = async () => {
  await nextTick()
  inputRef.value?.focus?.()
}

const scrollToBottom = async () => {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => props.items.map((item) => item.content.length).join(','),
  scrollToBottom
)
watch(
  () => props.items.length,
  scrollToBottom
)

defineExpose({ focusInput })
</script>

<style lang="less" scoped>
@panel-z-index: 990;

.floating-panel {
  position: fixed;
  z-index: @panel-z-index;
  display: flex;
  flex-direction: column;
  width: 380px;
  height: min(540px, calc(100vh - 24px));
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--color-bg-elevated, var(--gray-0));
  box-shadow:
    0 8px 24px rgba(0, 0, 0, 0.12),
    0 2px 8px rgba(0, 0, 0, 0.06);
  overflow: hidden;
  animation: panel-in 0.15s ease-out;
}

.floating-panel.sheet {
  top: auto;
  right: 12px;
  bottom: 12px;
  left: 12px;
  width: auto;
  height: min(70vh, 560px);
}

.floating-panel.reduced-motion,
.floating-panel.reduced-motion .panel-status-dot {
  animation: none;
}

@keyframes panel-in {
  from {
    opacity: 0;
    transform: translateY(6px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-100);
}

.panel-agent {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.panel-agent-dot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--main-600);
}

.panel-agent-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-800);
}

.panel-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.panel-action-btn {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--gray-500);
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.panel-action-btn:hover:not(:disabled) {
  background: var(--gray-50);
  color: var(--gray-800);
}

.panel-action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-empty {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  color: var(--gray-500);
  padding: 24px 12px;
}

.panel-empty-icon {
  color: var(--main-500);
}

.panel-empty-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--gray-700);
}

.panel-empty-hint {
  margin: 0;
  font-size: 12px;
}

.panel-msg.user {
  display: flex;
  justify-content: flex-end;
}

.panel-msg-bubble-user {
  max-width: 86%;
  padding: 7px 10px;
  border-radius: 10px;
  background: var(--main-700);
  color: var(--gray-0);
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.panel-msg.assistant {
  min-width: 0;
}

.panel-msg-body {
  font-size: 13px;
  line-height: 1.6;
  color: var(--gray-900);
}

.panel-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--gray-500);
}

.panel-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--main-500);
  animation: status-pulse 1.2s ease-in-out infinite;
}

@keyframes status-pulse {
  0%,
  100% {
    opacity: 0.35;
  }
  50% {
    opacity: 1;
  }
}

.panel-error {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--color-error-500);
}

.panel-error-retry {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid var(--color-error-100);
  border-radius: 999px;
  background: transparent;
  color: var(--color-error-500);
  font-size: 12px;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.panel-error-retry:hover {
  background: var(--color-error-10);
}

.panel-notice {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 8px;
  border: 1px dashed var(--gray-150);
  border-radius: 8px;
  background: var(--gray-25, var(--gray-50));
  font-size: 12px;
  color: var(--gray-600);
  text-align: center;
}

.panel-notice-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: none;
  background: transparent;
  padding: 0;
  color: var(--main-700);
  font-size: 12px;
  cursor: pointer;
}

.panel-notice-link:hover {
  text-decoration: underline;
}

.panel-footer {
  border-top: 1px solid var(--gray-100);
  padding: 8px 10px;
  background: var(--color-bg-elevated, var(--gray-0));
}

.panel-input {
  :deep(.user-input) {
    min-height: 36px;
    max-height: 120px;
    font-size: 13px;
  }
}
</style>
