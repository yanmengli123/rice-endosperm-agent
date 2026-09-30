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
