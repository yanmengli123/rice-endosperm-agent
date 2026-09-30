<template>
  <div class="browser-connection-panel">
    <a-spin :spinning="loading">
      <div class="browser-panel-inner">
        <!-- 连接状态卡 -->
        <div class="browser-status-card" :class="`browser-status-card--${statusKind}`">
          <div class="browser-status-head">
            <span class="browser-status-dot"></span>
            <span class="browser-status-title">{{ statusTitle }}</span>
            <Monitor :size="16" class="browser-status-icon" />
          </div>
          <p class="browser-status-desc">{{ statusDesc }}</p>
          <div v-if="statusDevice" class="browser-status-meta">
            <span>设备名：{{ statusDevice.device_name || '未知设备' }}</span>
            <span v-if="isOnline">扩展版本：{{ statusDevice.extension_version || '-' }}</span>
            <span>
              {{ isOnline ? '最后心跳时间' : '最后在线时间' }}：
              {{ formatFullDateTime(statusDevice.last_seen_at) }}
            </span>
            <span v-if="!isOnline && status.diagnostic?.code" class="browser-status-diagnostic">
              诊断：{{ status.diagnostic.code }}
            </span>
          </div>
          <div class="browser-status-actions">
            <template v-if="managedExternally">
              <span class="browser-managed-label">由 BrowserSkill 管理，无需再次配对</span>
              <a-button size="small" :loading="refreshing" @click="refreshStatus"
                >重新检测</a-button
              >
            </template>
            <a-button
              v-else-if="!isPaired"
              type="primary"
              :loading="creatingLink"
              @click="handleCreatePairingLink"
            >
              生成配对链接
            </a-button>
            <template v-else>
              <a-button @click="openDeviceList">查看设备列表</a-button>
              <a-button danger @click="confirmRevokeCurrent">撤销配对</a-button>
            </template>
          </div>
        </div>

        <p class="browser-panel-hint">
          配对后，在智能推理输入框开启「本机浏览器」开关，智能体即可访问你本机浏览器中打开的页面。
        </p>

        <!-- 域名策略卡（仅管理员可见） -->
        <div v-if="userStore.isAdmin" class="browser-policy-card">
          <div class="browser-card-head">
            <span class="browser-card-title">域名策略</span>
            <Shield :size="16" class="browser-card-icon" />
          </div>
          <a-spin :spinning="policyLoading">
            <a-radio-group
              v-model:value="policyForm.mode"
              class="browser-policy-mode"
              :disabled="policySaving"
            >
              <a-radio value="off">关闭</a-radio>
              <a-radio value="allowlist">白名单</a-radio>
              <a-radio value="denylist">黑名单</a-radio>
            </a-radio-group>
            <a-textarea
              v-model:value="policyForm.domainsText"
              :rows="5"
              :disabled="policySaving || policyForm.mode === 'off'"
              placeholder="每行一个域名，例如：&#10;example.com&#10;internal.example.org"
            />
            <p class="browser-policy-hint">
              白名单：仅允许导航到匹配域名（含子域）；黑名单：禁止导航到匹配域名。
            </p>
            <div class="browser-policy-actions">
              <a-button type="primary" :loading="policySaving" @click="handleSavePolicy">
                保存策略
              </a-button>
            </div>
          </a-spin>
        </div>

        <!-- 实时预览卡 -->
        <div class="browser-preview-card">
          <div class="browser-card-head">
            <span class="browser-card-title">实时预览</span>
            <Eye :size="16" class="browser-card-icon" />
          </div>
          <p class="browser-preview-hint">
            粘贴运行 ID（run_id）观看智能体操作本机浏览器的实时画面，每个运行最多支持 2 人同时观看。
          </p>
          <div class="browser-preview-controls">
            <a-input
              v-model:value="previewRunIdInput"
              class="browser-preview-run-input"
              placeholder="输入运行 ID，例如：run_xxx"
              allow-clear
              :disabled="!!previewRunId"
              @press-enter="handleStartPreview"
            />
            <a-button
              v-if="!previewRunId"
              type="primary"
              :loading="previewStarting"
              @click="handleStartPreview"
            >
              开始预览
            </a-button>
            <a-button v-else @click="handleClosePreview">关闭预览</a-button>
          </div>
          <div class="browser-preview-stage">
            <img
              v-if="previewFrame"
              :src="previewFrame"
              class="browser-preview-frame"
              alt="本机浏览器实时画面"
            />
            <div v-else class="browser-preview-placeholder">
              {{ previewPlaceholderText }}
            </div>
            <div v-if="previewStatusText" class="browser-preview-status">
              {{ previewStatusText }}
            </div>
          </div>
          <div v-if="previewRunId" class="browser-preview-actions">
            <a-button
              size="small"
              :disabled="previewControlsDisabled"
              :loading="previewControlPending === 'pause'"
              @click="handlePreviewControl('pause')"
            >
              暂停
            </a-button>
            <a-button
              size="small"
              :disabled="previewControlsDisabled"
              :loading="previewControlPending === 'resume'"
              @click="handlePreviewControl('resume')"
            >
              继续
            </a-button>
            <a-button
              size="small"
              danger
              :disabled="previewControlsDisabled"
              :loading="previewControlPending === 'end'"
              @click="handlePreviewControl('end')"
            >
              结束会话
            </a-button>
          </div>
        </div>
      </div>
    </a-spin>

    <!-- 配对链接弹窗 -->
    <a-modal
      v-model:open="pairingModalVisible"
      title="本机浏览器配对"
      :footer="null"
      width="560px"
      :destroy-on-close="true"
      @cancel="closePairingModal"
    >
      <div class="browser-pairing-panel">
        <div class="browser-pairing-link-row">
          <span class="browser-pairing-link" :title="pairingUrl">{{ pairingUrl }}</span>
          <a-button size="small" @click="copyPairingUrl">
            <template #icon><Copy :size="14" /></template>
            复制
          </a-button>
        </div>
        <p class="browser-pairing-countdown" :class="{ expired: countdownSeconds <= 0 }">
          <template v-if="countdownSeconds > 0">链接有效期剩余 {{ countdownText }}</template>
          <template v-else>链接已过期，请重新生成</template>
        </p>
        <ol class="browser-pairing-steps">
          <li>复制上方配对链接；</li>
          <li>在浏览器扩展「连接设置 → 远程连接」中粘贴并保存；</li>
          <li>在智能推理输入框开启「本机浏览器」即可。</li>
        </ol>
      </div>
    </a-modal>

    <!-- 设备列表弹窗 -->
    <a-modal
      v-model:open="deviceModalVisible"
      title="已授权浏览器设备"
      :footer="null"
      width="760px"
      :destroy-on-close="true"
      @cancel="closeDeviceModal"
    >
      <div class="browser-device-panel">
        <a-table
          v-if="devices.length > 0"
          :columns="deviceColumns"
          :data-source="devices"
          row-key="device_id"
          size="small"
          :pagination="false"
          :loading="devicesLoading"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'device_name'">
              <span class="browser-device-name">
                <span
                  class="browser-device-online-dot"
                  :class="{ on: record.online }"
                  :title="record.online ? '在线' : '离线'"
                ></span>
                {{ record.device_name || '未知设备' }}
              </span>
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tag :color="record.status === 'active' ? 'green' : 'default'">
                {{ record.status === 'active' ? '已授权' : '已撤销' }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'authorized_at'">
              {{ formatDateTime(record.authorized_at) }}
            </template>
            <template v-else-if="column.key === 'last_seen_at'">
              {{ formatDateTime(record.last_seen_at) }}
            </template>
            <template v-else-if="column.key === 'action'">
              <a-button
                v-if="record.status === 'active'"
                type="link"
                danger
                size="small"
                :loading="revokingDeviceId === record.device_id"
                @click="confirmRevokeDevice(record)"
              >
                撤销
              </a-button>
            </template>
          </template>
        </a-table>
        <a-empty v-else-if="!devicesLoading" description="暂无已授权的浏览器设备" />
      </div>
    </a-modal>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { Copy, Eye, Monitor, Shield } from '@lucide/vue'
import { browserApi } from '@/apis/browser_api'
import { useUserStore } from '@/stores/user'
import { useBrowserPreview } from '@/composables/useBrowserPreview'
import dayjs, { formatDateTime, formatFullDateTime } from '@/utils/time'

const userStore = useUserStore()

// =============================================================================
// === 连接状态 ===
// =============================================================================

const loading = ref(true)
const status = ref({ paired: false, online: false, device: null, diagnostic: null })
const creatingLink = ref(false)
const refreshing = ref(false)

const isPaired = computed(() => !!status.value.paired)
const isOnline = computed(() => !!status.value.online)
const statusDevice = computed(() => status.value.device)
const managedExternally = computed(() => !!status.value.managed_externally)

const statusKind = computed(() => {
  if (!isPaired.value) return 'unpaired'
  return isOnline.value ? 'online' : 'offline'
})

const statusTitle = computed(() => {
  if (managedExternally.value) {
    return isOnline.value ? 'BrowserSkill 已连接' : 'BrowserSkill 离线'
  }
  return { unpaired: '未配对', offline: '已配对 · 离线', online: '已连接' }[statusKind.value]
})

const statusDesc = computed(() => {
  if (managedExternally.value) {
    return isOnline.value
      ? '本机 BrowserSkill 扩展与语析已连通，可在智能推理中直接使用本机浏览器。'
      : '语析已启用 BrowserSkill 接入，但当前未检测到 Chrome 扩展连接，请确认 Chrome 与本机守护进程正在运行。'
  }
  return {
    unpaired:
      '尚未配对本机浏览器。生成配对链接并在浏览器扩展中完成配对后，即可在对话中使用本机浏览器。',
    offline: '浏览器扩展当前不在线，请确认浏览器已打开且扩展处于启用状态。',
    online: '本机浏览器扩展已连接，可在智能推理输入框开启「本机浏览器」让智能体访问。'
  }[statusKind.value]
})

const fetchStatus = async () => {
  try {
    const data = await browserApi.getBrowserStatus()
    status.value = {
      paired: !!data?.paired,
      online: !!data?.online,
      device: data?.device || null,
      backend: data?.backend || null,
      managed_externally: !!data?.managed_externally,
      diagnostic: data?.diagnostic || null,
      bridge: data?.bridge || null
    }
  } catch (err) {
    console.error('获取浏览器连接状态失败:', err)
    status.value = {
      ...status.value,
      online: false,
      diagnostic: { code: 'STATUS_REQUEST_FAILED', message: err?.message || '状态请求失败' }
    }
  } finally {
    loading.value = false
    scheduleStatusPoll()
  }
}

const refreshStatus = async () => {
  refreshing.value = true
  try {
    await fetchStatus()
  } finally {
    refreshing.value = false
  }
}

// =============================================================================
// === 配对链接 ===
// =============================================================================

const pairingModalVisible = ref(false)
const pairingInfo = ref(null)
const countdownSeconds = ref(0)
let countdownTimer = null
let statusTimer = null
let mounted = false

const scheduleStatusPoll = () => {
  if (statusTimer) clearTimeout(statusTimer)
  if (!mounted) return
  // 离线时快速发现恢复；在线时降低后台探测频率。
  statusTimer = setTimeout(fetchStatus, isOnline.value ? 30000 : 5000)
}

const handleVisibilityChange = () => {
  if (document.visibilityState === 'visible') fetchStatus()
}

const pairingUrl = computed(() => pairingInfo.value?.pairing_url || '')

const countdownText = computed(() => {
  const total = Math.max(0, countdownSeconds.value)
  const minutes = String(Math.floor(total / 60)).padStart(2, '0')
  const seconds = String(total % 60).padStart(2, '0')
  return `${minutes}:${seconds}`
})

const stopCountdown = () => {
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
}

const startCountdown = (expiresAt) => {
  stopCountdown()
  const tick = () => {
    const remaining = dayjs(expiresAt).diff(dayjs(), 'second')
    countdownSeconds.value = Math.max(0, remaining)
    if (countdownSeconds.value <= 0) {
      stopCountdown()
      // 链接过期后立即刷新一次连接状态
      fetchStatus()
    }
  }
  tick()
  countdownTimer = setInterval(tick, 1000)
}

const handleCreatePairingLink = async () => {
  creatingLink.value = true
  try {
    const data = await browserApi.createPairingLink()
    pairingInfo.value = data
    pairingModalVisible.value = true
    if (data?.expires_at) {
      startCountdown(data.expires_at)
    } else {
      countdownSeconds.value = 5 * 60
    }
  } catch (err) {
    message.error(err.message || '生成配对链接失败')
  } finally {
    creatingLink.value = false
  }
}

const closePairingModal = () => {
  pairingModalVisible.value = false
  stopCountdown()
}

const copyPairingUrl = async () => {
  try {
    await navigator.clipboard.writeText(pairingUrl.value)
    message.success('配对链接已复制')
  } catch {
    message.error('复制失败，请手动复制')
  }
}

// 已配对后自动收起配对弹窗并停止倒计时
watch(isPaired, (paired) => {
  if (paired) {
    stopCountdown()
    if (pairingModalVisible.value) {
      pairingModalVisible.value = false
    }
  }
})

// =============================================================================
// === 设备列表与撤销 ===
// =============================================================================

const deviceModalVisible = ref(false)
const devices = ref([])
const devicesLoading = ref(false)
const revokingDeviceId = ref('')

const deviceColumns = [
  { title: '设备名', key: 'device_name', ellipsis: true },
  { title: '扩展版本', dataIndex: 'extension_version', key: 'extension_version', width: 110 },
  { title: '状态', key: 'status', width: 100 },
  { title: '授权时间', key: 'authorized_at', width: 170 },
  { title: '最后在线', key: 'last_seen_at', width: 170 },
  { title: '操作', key: 'action', width: 80 }
]

const fetchDevices = async () => {
  devicesLoading.value = true
  try {
    const data = await browserApi.listBrowserDevices()
    devices.value = data?.devices || []
  } catch (err) {
    message.error(err.message || '获取设备列表失败')
  } finally {
    devicesLoading.value = false
  }
}

const openDeviceList = () => {
  deviceModalVisible.value = true
  fetchDevices()
}

const closeDeviceModal = () => {
  deviceModalVisible.value = false
}

const confirmRevokeCurrent = () => {
  if (statusDevice.value?.device_id) {
    confirmRevokeDevice(statusDevice.value)
  }
}

const confirmRevokeDevice = (device) => {
  Modal.confirm({
    title: '确认撤销设备',
    content: `确定要撤销设备「${device.device_name || '未知设备'}」吗？撤销后该浏览器扩展需要重新配对才能使用。`,
    okText: '撤销',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      revokingDeviceId.value = device.device_id
      try {
        await browserApi.revokeBrowserDevice(device.device_id)
        message.success('设备已撤销')
        if (deviceModalVisible.value) {
          await fetchDevices()
        }
        await fetchStatus()
      } catch (err) {
        message.error(err.message || '撤销失败')
      } finally {
        revokingDeviceId.value = ''
      }
    }
  })
}

// =============================================================================
// === 域名策略（仅管理员） ===
// =============================================================================

const POLICY_MODES = ['off', 'allowlist', 'denylist']

const policyLoading = ref(false)
const policySaving = ref(false)
const policyForm = reactive({ mode: 'off', domainsText: '' })

const normalizePolicyResponse = (data) => {
  return {
    mode: POLICY_MODES.includes(data?.mode) ? data.mode : 'off',
    domainsText: Array.isArray(data?.domains) ? data.domains.join('\n') : ''
  }
}

// 每行一个域名：去空白、去重、忽略空行；统一小写便于后端匹配
const parseDomainLines = (text) => {
  const seen = new Set()
  const domains = []
  for (const rawLine of String(text || '').split(/\r?\n/)) {
    const domain = rawLine.trim().toLowerCase()
    if (!domain || seen.has(domain)) continue
    seen.add(domain)
    domains.push(domain)
  }
  return domains
}

const fetchPolicy = async () => {
  if (!userStore.isAdmin) return
  policyLoading.value = true
  try {
    const data = await browserApi.getBrowserPolicy()
    const normalized = normalizePolicyResponse(data)
    policyForm.mode = normalized.mode
    policyForm.domainsText = normalized.domainsText
  } catch (err) {
    console.error('获取浏览器域名策略失败:', err)
    message.error(err.message || '获取域名策略失败')
  } finally {
    policyLoading.value = false
  }
}

const handleSavePolicy = async () => {
  policySaving.value = true
  try {
    const domains = policyForm.mode === 'off' ? [] : parseDomainLines(policyForm.domainsText)
    const data = await browserApi.updateBrowserPolicy({ mode: policyForm.mode, domains })
    const normalized = normalizePolicyResponse(data)
    policyForm.mode = normalized.mode
    policyForm.domainsText = normalized.domainsText
    message.success('域名策略已保存')
  } catch (err) {
    message.error(err.message || '保存域名策略失败')
  } finally {
    policySaving.value = false
  }
}

// =============================================================================
// === 实时预览 ===
// =============================================================================

const previewRunIdInput = ref('')
const {
  activeRunId: previewRunId,
  starting: previewStarting,
  ended: previewEnded,
  endReasonText: previewEndReasonText,
  error: previewError,
  frameDataUrl: previewFrame,
  controlPending: previewControlPending,
  startPreview,
  closePreview,
  sendControl: sendPreviewControl
} = useBrowserPreview()

const previewPlaceholderText = computed(() => {
  if (previewStarting.value) return '正在建立预览连接…'
  if (previewError.value) return previewError.value
  if (previewRunId.value) return '已连接，等待浏览器操作画面…'
  return '输入运行 ID 并开始预览后，这里将显示本机浏览器的实时画面'
})

const previewStatusText = computed(() => {
  if (previewError.value) return previewError.value
  if (previewEnded.value) {
    return previewEndReasonText.value ? `预览已结束：${previewEndReasonText.value}` : '预览已结束'
  }
  return ''
})

const previewControlsDisabled = computed(() => previewEnded.value)

const handleStartPreview = () => {
  const runId = previewRunIdInput.value.trim()
  if (!runId) {
    message.warning('请先输入运行 ID（run_id）')
    return
  }
  void startPreview(runId)
}

const handleClosePreview = () => {
  closePreview()
  previewRunIdInput.value = ''
}

const handlePreviewControl = async (action) => {
  try {
    await sendPreviewControl(action)
    const successTexts = {
      pause: '已请求暂停浏览器操作',
      resume: '已请求继续浏览器操作',
      end: '已请求结束浏览器会话'
    }
    message.success(successTexts[action] || '操作已发送')
  } catch (err) {
    message.error(err.message || '控制请求失败')
  }
}

// =============================================================================
// === 生命周期 ===
// =============================================================================

onMounted(() => {
  mounted = true
  fetchStatus()
  fetchPolicy()
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  mounted = false
  if (statusTimer) {
    clearTimeout(statusTimer)
    statusTimer = null
  }
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  stopCountdown()
})

defineExpose({
  loading
})
</script>

<style scoped lang="less">
.browser-connection-panel {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  background-color: var(--gray-0);
}

.browser-panel-inner {
  padding: 20px var(--page-padding);
}

.browser-status-card {
  max-width: 640px;
  padding: 20px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
  box-shadow: 0 1px 4px var(--shadow-0);

  .browser-status-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .browser-status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--gray-300);
  }

  .browser-status-title {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-900);
  }

  .browser-status-icon {
    margin-left: auto;
    color: var(--gray-300);
  }

  .browser-status-desc {
    margin: 10px 0 0;
    color: var(--gray-500);
    font-size: 13px;
    line-height: 1.6;
  }

  .browser-status-meta {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 12px;
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--gray-10);
    color: var(--gray-700);
    font-size: 13px;
  }

  .browser-status-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 16px;
  }

  .browser-managed-label {
    color: var(--gray-500);
    font-size: 13px;
  }

  .browser-status-diagnostic {
    color: var(--color-error-500);
    font-family: 'Monaco', 'Consolas', monospace;
    font-size: 12px;
  }

  &--online {
    .browser-status-dot {
      background: var(--color-success-500);
    }

    .browser-status-icon {
      color: var(--color-success-500);
    }
  }

  &--offline {
    .browser-status-dot {
      background: var(--color-warning-500);
    }

    .browser-status-icon {
      color: var(--color-warning-500);
    }
  }
}

.browser-panel-hint {
  margin: 12px 0 0;
  color: var(--gray-400);
  font-size: 12px;
}

.browser-policy-card,
.browser-preview-card {
  max-width: 640px;
  margin-top: 16px;
  padding: 20px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
  box-shadow: 0 1px 4px var(--shadow-0);

  .browser-card-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .browser-card-title {
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-900);
  }

  .browser-card-icon {
    margin-left: auto;
    color: var(--gray-300);
  }
}

.browser-policy-mode {
  display: flex;
  margin: 14px 0 10px;
}

.browser-policy-hint {
  margin: 10px 0 0;
  color: var(--gray-400);
  font-size: 12px;
  line-height: 1.6;
}

.browser-policy-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.browser-preview-hint {
  margin: 10px 0 0;
  color: var(--gray-500);
  font-size: 13px;
  line-height: 1.6;
}

.browser-preview-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;

  .browser-preview-run-input {
    flex: 1;
    min-width: 0;
  }
}

.browser-preview-stage {
  position: relative;
  margin-top: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-10);
  overflow: hidden;

  .browser-preview-frame {
    display: block;
    width: 100%;
    height: auto;
  }

  .browser-preview-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 180px;
    padding: 16px;
    color: var(--gray-400);
    font-size: 13px;
    text-align: center;
    line-height: 1.6;
  }

  .browser-preview-status {
    padding: 8px 12px;
    border-top: 1px solid var(--gray-150);
    background: var(--gray-50);
    color: var(--gray-500);
    font-size: 12px;
  }
}

.browser-preview-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.browser-pairing-panel {
  .browser-pairing-link-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    background: var(--gray-50);
  }

  .browser-pairing-link {
    flex: 1;
    min-width: 0;
    font-family: 'Monaco', 'Consolas', monospace;
    font-size: 12px;
    color: var(--gray-700);
    word-break: break-all;
  }

  .browser-pairing-countdown {
    margin: 10px 0 0;
    font-size: 13px;
    color: var(--color-warning-700);

    &.expired {
      color: var(--color-error-500);
    }
  }

  .browser-pairing-steps {
    margin: 16px 0 0;
    padding-left: 20px;
    color: var(--gray-600);
    font-size: 13px;
    line-height: 1.9;

    li + li {
      margin-top: 2px;
    }
  }
}

.browser-device-panel {
  .browser-device-name {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .browser-device-online-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--gray-300);

    &.on {
      background: var(--color-success-500);
    }
  }
}
</style>
