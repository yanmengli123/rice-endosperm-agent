<template>
  <MessageInputComponent
    ref="inputRef"
    :model-value="modelValue"
    @update:modelValue="updateValue"
    :is-loading="isLoading"
    :disabled="disabled"
    :send-button-disabled="sendButtonDisabled"
    :placeholder="placeholder"
    :mention="mention"
    :thread-id="threadId"
    :file-upload-enabled="supportsFileUpload"
    @send="handleSend"
    @keydown="handleKeyDown"
    @paste-image="handlePastedImage"
    @drop-files="handleDroppedFiles"
  >
    <template #top>
      <div v-if="currentImage || previewAttachments.length" class="input-top-stack">
        <ImagePreviewComponent
          v-if="currentImage"
          :image-data="currentImage"
          @remove="handleImageRemoved"
          class="image-preview-wrapper"
        />

        <div v-if="previewAttachments.length" class="attachment-preview-list">
          <div
            v-for="attachment in previewAttachments"
            :key="attachment.fileId"
            class="attachment-file-card"
          >
            <div class="attachment-file-icon">
              <FileTypeIcon :name="attachment.name" :size="18" />
            </div>
            <div class="attachment-file-body">
              <div class="attachment-file-name" :title="attachment.name">{{ attachment.name }}</div>
              <div class="attachment-file-meta">{{ attachment.meta }}</div>
            </div>
            <button
              class="attachment-remove-btn"
              type="button"
              :aria-label="`移除附件 ${attachment.name}`"
              @click.stop="handleAttachmentRemoved(attachment)"
            >
              <X :size="14" />
            </button>
          </div>
        </div>
      </div>

      <!-- 意图提示：用户写了要操作本机浏览器但开关没开——模型这一轮不会有该工具，
           若不提示会直接表现为「功能坏了」（真实事故，见 docs/local-browser.md 排障）。 -->
      <div v-if="showBrowserIntentHint" class="browser-intent-hint" role="status">
        <span class="browser-intent-hint-text">
          检测到你要用本机浏览器操作网页，但本轮开关未开启；开启后模型才能真正驱动你的 Chrome。
        </span>
        <button
          type="button"
          class="browser-intent-hint-btn"
          :disabled="disabled"
          @click.stop="toggleBrowserChip"
        >
          开启本机浏览器
        </button>
        <button
          type="button"
          class="browser-intent-hint-dismiss"
          aria-label="忽略本提示"
          @click.stop="browserIntentDismissed = true"
        >
          <X :size="14" />
        </button>
      </div>
    </template>
    <template #options-left>
      <AttachmentOptionsComponent
        v-if="supportsFileUpload"
        :disabled="disabled"
        @upload="handleAttachmentUpload"
        @upload-image="handleImageUpload"
        @upload-image-success="handleImageUploadSuccess"
      />
    </template>
    <template #actions-left>
      <div class="input-actions-left">
        <button
          type="button"
          class="input-action-btn browser-toggle-chip"
          :class="{ active: browserEnabled, offline: browserPaired && browserOnline === false }"
          :title="browserToggleTitle"
          :aria-pressed="browserEnabled"
          @click.stop="toggleBrowserChip"
          @mousedown.stop
        >
          <Monitor :size="15" />
          <span class="hide-text">本机浏览器</span>
          <span class="browser-health-dot" :class="browserHealthKind"></span>
        </button>
        <slot name="actions-left-extra"></slot>
      </div>
    </template>
    <template #actions-right>
      <div class="input-actions-right">
        <slot name="actions-right-extra"></slot>
      </div>
    </template>
  </MessageInputComponent>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import MessageInputComponent from '@/components/MessageInputComponent.vue'
import ImagePreviewComponent from '@/components/ImagePreviewComponent.vue'
import AttachmentOptionsComponent from '@/components/AttachmentOptionsComponent.vue'
import { Monitor, X } from '@lucide/vue'
import { normalizeAttachmentPreviews } from '@/utils/file_utils'
import { uploadMultimodalImage } from '@/utils/multimodal_image_upload'
import FileTypeIcon from '@/components/common/FileTypeIcon.vue'
import { browserApi } from '@/apis/browser_api'

const props = defineProps({
  modelValue: { type: String, default: '' },
  isLoading: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  sendButtonDisabled: { type: Boolean, default: false },
  mention: { type: Object, default: () => null },
  threadId: { type: String, default: '' },
  supportsFileUpload: { type: Boolean, default: false },
  attachments: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits([
  'update:modelValue',
  'send',
  'keydown',
  'upload-attachment',
  'remove-attachment'
])

const inputRef = ref(null)
const currentImage = ref(null)
const placeholder = '问点什么？使用 @ 可以提及哦~'

// 本机浏览器开关：默认关闭；在线状态持续刷新，run 创建时冻结用户选择。
const browserEnabled = ref(false)
const browserPaired = ref(null)
const browserOnline = ref(null)
let browserStatusTimer = null
let browserStatusMounted = false

const browserHealthKind = computed(() => {
  if (browserOnline.value === true) return 'online'
  if (browserPaired.value === false || browserOnline.value === false) return 'offline'
  return 'unknown'
})

const browserToggleTitle = computed(() => {
  if (browserEnabled.value) return '本机浏览器：本轮已开启'
  if (browserOnline.value === true) return '本机浏览器：已连接，点击为本轮开启'
  if (browserPaired.value === false) return '本机浏览器：尚未配置'
  return '本机浏览器：当前离线'
})

const scheduleBrowserStatus = () => {
  if (browserStatusTimer) clearTimeout(browserStatusTimer)
  if (!browserStatusMounted) return
  browserStatusTimer = setTimeout(refreshBrowserStatus, browserOnline.value ? 60000 : 10000)
}

const refreshBrowserStatus = async () => {
  try {
    const status = await browserApi.getBrowserStatus()
    browserPaired.value = !!status?.paired
    browserOnline.value = !!status?.online
  } catch (error) {
    console.error('查询本机浏览器配对状态失败:', error)
    browserPaired.value = null
    browserOnline.value = null
  } finally {
    scheduleBrowserStatus()
  }
}

const handleBrowserVisibility = () => {
  if (document.visibilityState === 'visible') refreshBrowserStatus()
}

onMounted(() => {
  browserStatusMounted = true
  refreshBrowserStatus()
  document.addEventListener('visibilitychange', handleBrowserVisibility)
})

onUnmounted(() => {
  browserStatusMounted = false
  if (browserStatusTimer) clearTimeout(browserStatusTimer)
  document.removeEventListener('visibilitychange', handleBrowserVisibility)
})

const toggleBrowserChip = () => {
  if (props.disabled) return
  if (!browserEnabled.value && browserPaired.value === false) {
    message.warning('本机浏览器尚未配对，请前往「智能体扩展 → 浏览器连接」生成配对链接')
    return
  }
  if (!browserEnabled.value && browserOnline.value === false) {
    message.warning('BrowserSkill 当前离线，请先打开 Chrome 并确认扩展已连接')
    refreshBrowserStatus()
    return
  }
  browserEnabled.value = !browserEnabled.value
}

// ---- 本机浏览器意图提示：发现用户要操作浏览器但开关没开，主动引导 ----
// 与后端 yuxi.agents.toolkits.browser.prompt.detect_local_browser_intent 保持同一口径
// （提示是条件引导，误判只多一条可忽略的提示条，代价极低；漏判才是事故）。
const browserIntentDismissed = ref(false)

const FEATURE_PATTERN =
  /(本机浏览器|本地浏览器|本机的浏览器|本机\s*chrome|local browser|my browser)/i
const TOOL_PATTERN = /browser_(get_status|navigate|read_page|click|type|screenshot|request_help)/i
const DOMAIN_PATTERN = /(浏览器|网页|网站|网址|首页|官网|域名|chromium|chrome|edge|登录态)/i
const ACTION_PATTERN =
  /(打开|访问|浏览|导航|跳转|抓取|爬取|读取|查看|截图|截屏|截个图|点击|输入|填写|提交|下载|搜索|查询|查找|查一下|搜一下|看一下|翻一下|browse|navigate|visit|screenshot)/i
const URL_PATTERN = /(https?:\/\/|www\.)\S+/i

const hasLocalBrowserIntent = (text) => {
  if (!text) return false
  if (FEATURE_PATTERN.test(text) || TOOL_PATTERN.test(text)) return true
  // 域词内含动作词（「浏览」⊂「浏览器」），先剥离域词再判动作，避免自触发
  const stripped = text.replace(new RegExp(DOMAIN_PATTERN.source, 'gi'), ' ')
  if (!ACTION_PATTERN.test(stripped)) return false
  if (URL_PATTERN.test(text)) return true
  return DOMAIN_PATTERN.test(text)
}

const showBrowserIntentHint = computed(() => {
  if (browserEnabled.value || browserIntentDismissed.value) return false
  return hasLocalBrowserIntent(props.modelValue)
})

const previewAttachments = computed(() => normalizeAttachmentPreviews(props.attachments))

const updateValue = (val) => {
  emit('update:modelValue', val)
}

const handleAttachmentUpload = (files = []) => {
  emit('upload-attachment', files)
}

const handleImageUpload = (imageData) => {
  if (imageData && imageData.success) {
    currentImage.value = imageData
  }
}

const handlePastedImage = async (file) => {
  if (props.disabled || !props.supportsFileUpload) return

  try {
    const imageData = await uploadMultimodalImage(file)
    handleImageUpload(imageData)
  } catch (error) {
    console.error('图片上传失败:', error)
  }
}

const handleDroppedFiles = (files = []) => {
  if (props.disabled || !props.supportsFileUpload || !files.length) return
  handleAttachmentUpload(files)
}

const handleImageUploadSuccess = () => {
  if (inputRef.value) {
    inputRef.value.closeOptions()
  }
}

const handleImageRemoved = () => {
  currentImage.value = null
}

const handleAttachmentRemoved = (attachment) => {
  emit('remove-attachment', attachment.raw)
}

const handleSend = () => {
  // 开关状态随 send 事件传出，发送后保持用户选择（不自动复位）
  emit('send', { image: currentImage.value, browserEnabled: browserEnabled.value })
  currentImage.value = null
  // 提示条按轮复位：下一条仍带浏览器意图且开关未开时应再次引导
  browserIntentDismissed.value = false
}

const handleKeyDown = (e) => {
  if (props.sendButtonDisabled) {
    return
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  } else {
    emit('keydown', e)
  }
}

defineExpose({
  focus: () => inputRef.value?.focus(),
  closeOptions: () => inputRef.value?.closeOptions(),
  prependText: (text) => inputRef.value?.prependText(text)
})
</script>

<style lang="less" scoped>
.input-actions-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.input-actions-right {
  display: flex;
  align-items: center;
  margin-right: 8px;
  gap: 2px;
}

.browser-intent-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  margin-bottom: 8px;
  padding: 6px 10px;
  border: 1px dashed var(--main-700);
  border-radius: 10px;
  background: var(--main-30);
}

.browser-intent-hint-text {
  flex: 1;
  min-width: 0;
  color: var(--gray-700);
  font-size: 12px;
  line-height: 1.5;
}

.browser-intent-hint-btn {
  flex-shrink: 0;
  padding: 3px 10px;
  border: none;
  border-radius: 8px;
  background: var(--main-700);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
  transition: opacity 0.15s ease;
}

.browser-intent-hint-btn:hover:not(:disabled) {
  opacity: 0.85;
}

.browser-intent-hint-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.browser-intent-hint-dismiss {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px;
  border: none;
  background: transparent;
  color: var(--gray-500);
  cursor: pointer;
}

.browser-intent-hint-dismiss:hover {
  color: var(--gray-900);
}

.input-top-stack {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 10px;
}

.attachment-preview-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.attachment-file-card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  width: 220px;
  min-width: 0;
  padding: 10px 34px 10px 12px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
  box-shadow: 0 1px 4px var(--shadow-0);
}

.attachment-file-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--main-700);
  background: var(--main-30);
}

.attachment-file-body {
  min-width: 0;
}

.attachment-file-name {
  overflow: hidden;
  color: var(--gray-900);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-file-meta {
  margin-top: 2px;
  color: var(--gray-500);
  font-size: 12px;
  line-height: 1.3;
}

.attachment-remove-btn {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  color: var(--gray-0);
  background: var(--gray-900);
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    transform 0.15s ease;

  &:hover {
    background: var(--gray-700);
  }

  &:active {
    transform: scale(0.96);
  }
}

// 输入框操作按钮通用样式（穿透到 slot 内容）
:deep(.input-action-btn) {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  height: 30px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--gray-600);
  cursor: pointer;
  transition: all 0.2s ease;
  user-select: none;
  background: transparent;
  border: none;

  &:hover {
    color: var(--gray-900);
    background: var(--gray-50);
  }

  &.active {
    color: var(--gray-900);
    background: var(--gray-100);
    font-weight: 500;
  }

  &.disabled {
    opacity: 0.5;
    cursor: not-allowed;
    pointer-events: none;
  }

  span {
    line-height: 1;
  }
}

// slot 内容的 hide-text 响应式样式
:deep(.hide-text) {
  @media (max-width: 768px) {
    display: none;
  }
}

.browser-health-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--gray-300);

  &.online {
    background: var(--color-success-500);
  }

  &.offline {
    background: var(--color-warning-500);
  }
}

@media (max-width: 768px) {
  .input-top-stack {
    gap: 8px;
    margin-bottom: 10px;
  }

  .attachment-file-card {
    width: min(220px, 100%);
  }
}
</style>
