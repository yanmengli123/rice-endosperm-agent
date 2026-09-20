<template>
  <div class="build-publish-workbench">
    <div class="bp-grid">
      <!-- 抽取器配置与构建（配置抽取器/开始索引/重建入口；managed_graph 契约下分流为说明卡） -->
      <div class="bp-card">
        <div class="card-header">
          <span class="card-title">抽取器配置与构建</span>
          <a-space>
            <a-tag v-if="isManagedGraph" color="default" size="small">契约禁止 LLM 抽取</a-tag>
            <template v-else>
              <a-tag v-if="!buildModel.configured" color="orange" size="small">未配置</a-tag>
              <a-tag v-else-if="buildModel.active" color="processing" size="small">{{ buildStatusLabel }}</a-tag>
              <a-tag v-else-if="buildModel.taskStatus === 'failed'" color="red" size="small">上次未完成</a-tag>
              <a-tag v-else-if="buildModel.pending > 0" color="gold" size="small">
                待索引 {{ buildModel.pending.toLocaleString('zh-CN') }} 段
              </a-tag>
              <a-tag v-else color="green" size="small">已就绪</a-tag>
            </template>
          </a-space>
        </div>
        <div v-if="isManagedGraph" class="form-hint">
          当前知识库为规范图谱契约（managed_graph）：规范事实只来自 Canonical 导入，
          禁止 LLM 自动抽取（llm_graph_config / llm_graph_build）。
          图谱内容请使用「图谱探索」工具栏的<b>托管导入</b>上传节点/关系 CSV 维护。
        </div>
        <template v-else>
          <div v-if="buildModel.configured" class="form-hint build-config-line">
            当前抽取器：{{ extractorLabel(buildStatus?.config?.extractor_type) }}
            <template v-if="buildStatus?.config?.extractor_options?.model_spec">
              · 模型 {{ buildStatus.config.extractor_options.model_spec }}
            </template>
          </div>
          <a-space wrap>
            <a-button size="small" type="primary" :loading="buildStatusLoading" @click="openConfigModal">
              {{ buildModel.configured ? '修改抽取配置' : '配置抽取器' }}
            </a-button>
            <a-button
              v-if="buildModel.configured"
              size="small"
              :loading="startingIndex"
              :disabled="buildActive"
              @click="startBuild"
            >
              开始索引
            </a-button>
            <a-button v-if="buildModel.configured" size="small" danger @click="confirmReset">清空并重建</a-button>
          </a-space>
          <div v-if="buildModel.configured" class="form-hint build-summary-line">
            {{ buildStatusSummary(buildModel) }}
          </div>
          <a-progress
            v-if="buildModel.configured && (buildModel.active || buildModel.taskStatus === 'failed')"
            :percent="buildModel.active ? buildModel.taskProgress : buildModel.percent"
            :status="buildModel.taskStatus === 'failed' ? 'exception' : 'active'"
            size="small"
          />
          <div
            v-if="buildModel.taskStatus === 'failed'"
            class="form-hint build-fail-line"
          >
            {{ buildModel.taskMessage || buildModel.taskError || '存在待重试段落，可再次点击「开始索引」续跑' }}
          </div>
          <div class="form-hint">
            抽取器类型（LLM 开放 Schema / 科研闭集）、模型与并发数决定构建产出的质量与成本；修改仅影响后续构建。
          </div>
        </template>
      </div>

      <!-- 生产治理策略（review_policy 治理化：变更写审计） -->
      <div class="bp-card">
        <div class="card-header">
          <span class="card-title">生产治理策略</span>
          <a-tag v-if="governanceUnchanged" color="green" size="small">与线上一致</a-tag>
        </div>
        <a-form layout="vertical" size="small">
          <a-form-item label="生产检索策略（review_policy，影响 Graph-RAG 消费的关系范围）">
            <a-radio-group v-model:value="governanceForm.review_policy" size="small">
              <a-radio-button value="candidates_visible">候选可见</a-radio-button>
              <a-radio-button value="approved_only">仅人工批准 + 规范层</a-radio-button>
            </a-radio-group>
            <div class="form-hint">
              「仅人工批准」下主图与 Graph-RAG 检索只含人工批准与规范层关系；变更将写入审计账本。
            </div>
          </a-form-item>
          <a-form-item label="批量批准准入档位">
            <a-radio-group v-model:value="governanceForm.batch_admission" size="small">
              <a-radio-button value="strict">严格</a-radio-button>
              <a-radio-button value="standard">标准</a-radio-button>
              <a-radio-button value="relaxed">宽松</a-radio-button>
            </a-radio-group>
            <div class="form-hint">{{ admissionHint }}</div>
          </a-form-item>
          <div class="form-row">
            <a-form-item label="maker-checker（审核与发布分离）">
              <a-switch v-model:checked="governanceForm.maker_checker" size="small" />
            </a-form-item>
            <a-form-item label="审核 SLA（小时，留空不限）">
              <a-input-number
                v-model:value="governanceForm.review_sla_hours"
                :min="1"
                :max="720"
                size="small"
                style="width: 120px"
              />
            </a-form-item>
          </div>
          <a-form-item>
            <a-button type="primary" size="small" :loading="governanceSaving" @click="saveGovernance">
              保存（写审计）
            </a-button>
          </a-form-item>
        </a-form>
      </div>

      <!-- 发布门禁（go/no-go） -->
      <div class="bp-card">
        <div class="card-header">
          <span class="card-title">发布门禁（go / no-go）</span>
          <a-button size="small" :loading="gatesLoading" @click="loadGates">重新评估</a-button>
        </div>
        <template v-if="gates">
          <a-tag :color="gates.go ? 'green' : 'red'" size="large">
            {{ gates.go ? 'GO' : 'NO-GO' }}
          </a-tag>
          <div v-for="blocker in gates.blockers" :key="blocker.code" class="gate-row gate-row--bad">
            <a-tag color="red" size="small">阻断</a-tag>{{ gateLabel(blocker.code) }}
            <span class="gate-detail">{{ blockerDetail(blocker) }}</span>
          </div>
          <div v-for="warning in gates.warnings" :key="warning.code" class="gate-row gate-row--warn">
            <a-tag color="orange" size="small">披露</a-tag>{{ gateLabel(warning.code) }}
            <span class="gate-detail">{{ blockerDetail(warning) }}</span>
          </div>
          <div v-if="!gates.blockers.length && !gates.warnings.length" class="form-hint">
            无阻断与披露项，可发布。
          </div>
        </template>
        <div v-else class="form-hint">点击「重新评估」运行发布门禁（完整性/maker-checker 阻断，死信/门禁/冲突披露）。</div>
      </div>

      <!-- Release 清单 -->
      <div class="bp-card bp-card--wide">
        <div class="card-header">
          <span class="card-title">发布清单（Release）</span>
          <a-space>
            <a-button size="small" :loading="releasesLoading" @click="loadReleases">刷新</a-button>
            <a-button size="small" type="primary" :loading="building" @click="buildRelease">构建新清单</a-button>
          </a-space>
        </div>
        <div v-if="!releases.length" class="form-hint">
          尚无发布清单。构建时将冻结文件修订与图谱决策清单（人工决策版本、水位哈希、治理策略）。
        </div>
        <table v-else class="release-table">
          <thead>
            <tr>
              <th>Release</th>
              <th>状态</th>
              <th>图谱决策冻结</th>
              <th>策略</th>
              <th>创建 / 发布时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="release in releases" :key="release.release_id">
              <td class="mono" :title="release.release_id">{{ release.release_id.slice(0, 14) }}…</td>
              <td>
                <a-tag :color="releaseStatusColor(release.status)" size="small">{{ release.status }}</a-tag>
              </td>
              <td>
                <span v-if="release.graph">
                  {{ release.graph.decisions_watermark?.total ?? 0 }} 条 · v≤{{ release.graph.decisions_watermark?.max_version ?? 0 }}
                  <a-tag v-if="release.graph.review_policy === 'approved_only'" size="small" color="green">仅批准</a-tag>
                </span>
                <span v-else class="form-hint">未冻结</span>
              </td>
              <td>
                <a-tag v-if="release.retrieval_policy_revision_id" size="small" color="blue">已挂接</a-tag>
                <span v-else class="form-hint">—</span>
              </td>
              <td class="time-cell">
                {{ formatTime(release.created_at) }}
                <template v-if="release.published_at"> / {{ formatTime(release.published_at) }}</template>
              </td>
              <td>
                <a-space size="small">
                  <a-button
                    v-if="release.status === 'STAGED'"
                    size="small"
                    type="primary"
                    :loading="publishingId === release.release_id"
                    @click="publishRelease(release)"
                  >
                    发布
                  </a-button>
                  <a-button
                    v-if="release.status === 'STAGED' && gates && !gates.go"
                    size="small"
                    danger
                    :loading="publishingId === release.release_id"
                    @click="publishRelease(release, true)"
                  >
                    强制发布
                  </a-button>
                </a-space>
              </td>
            </tr>
          </tbody>
        </table>
        <a-button
          v-if="hasActiveRelease"
          size="small"
          danger
          style="margin-top: 10px"
          @click="confirmRollback"
        >
          回滚到上一版
        </a-button>
      </div>

      <!-- KB 协作成员（viewer/reviewer/publisher） -->
      <div class="bp-card bp-card--wide">
        <div class="card-header">
          <span class="card-title">协作成员（KB 级能力）</span>
        </div>
        <div class="form-hint" style="margin-bottom: 8px">
          share_config 决定「能否看见」，这里决定「能做什么」：viewer 只读治理视图，reviewer 可裁决，publisher
          可发布。管理员默认具备全部能力。
        </div>
        <div class="member-add-row">
          <a-input v-model:value="memberForm.uid" placeholder="用户 uid" size="small" style="width: 220px" />
          <a-select
            v-model:value="memberForm.capability"
            size="small"
            style="width: 140px"
            :options="capabilityOptions"
          />
          <a-button size="small" type="primary" :loading="memberSaving" @click="addMember">授予 / 更新</a-button>
        </div>
        <table v-if="members.length" class="release-table">
          <thead>
            <tr>
              <th>成员</th>
              <th>能力</th>
              <th>授予人</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="member in members" :key="member.uid">
              <td class="mono">{{ member.uid }}</td>
              <td>
                <a-tag :color="capabilityColor(member.capability)" size="small">{{ member.capability }}</a-tag>
              </td>
              <td>{{ member.created_by }}</td>
              <td>
                <a-button size="small" danger @click="removeMember(member)">移除</a-button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <GraphExtractorConfigModal
      v-model:open="showConfigModal"
      :kb-id="props.kbId"
      :status="buildStatus"
      @saved="onConfigSaved"
    />
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'
import { graphBuildApi, kbReleaseApi } from '@/apis/knowledge_api'
import { BATCH_ADMISSION_META, normalizeBuildStatus, buildStatusSummary, buildTaskLabel } from '@/utils/graph/reviewMeta'
import { useDatabaseStore } from '@/stores/database'
import GraphExtractorConfigModal from '@/components/graph/GraphExtractorConfigModal.vue'

const props = defineProps({
  kbId: { type: String, required: true }
})

const emit = defineEmits(['changed'])

// 契约感知：managed_graph 规范图谱库禁止 LLM 抽取命令，抽取器卡分流为说明
const dbStore = useDatabaseStore()
const isManagedGraph = computed(() => dbStore.database?.contract_key === 'managed_graph')

// ---- 抽取器配置与构建 ----
const buildStatus = ref(null)
const buildStatusLoading = ref(false)
const showConfigModal = ref(false)
const startingIndex = ref(false)
let buildPollTimer = null

// 构建进度展示模型（归一化，杜绝「已开始索引  个段落」这类空数字）
const buildModel = computed(() => normalizeBuildStatus(buildStatus.value))
const buildStatusLabel = computed(() => buildTaskLabel(buildModel.value))
const buildActive = computed(() => buildModel.value.active)
const pendingChunks = computed(() => buildModel.value.pending)

const extractorLabel = (type) => ({ llm: 'LLM（开放 Schema）', llm_scientific: '科研闭集' })[type] || type || '—'

const loadBuildStatus = async () => {
  if (!props.kbId) return
  buildStatusLoading.value = true
  try {
    buildStatus.value = await graphBuildApi.getStatus(props.kbId)
  } catch (e) {
    console.error('Failed to load graph build status:', e)
  } finally {
    buildStatusLoading.value = false
  }
}

const openConfigModal = async () => {
  await loadBuildStatus()
  showConfigModal.value = true
}

const onConfigSaved = async () => {
  await loadBuildStatus()
  emit('changed')
}

const startBuild = async () => {
  if (!props.kbId || startingIndex.value || buildActive.value) return
  startingIndex.value = true
  try {
    const data = await graphBuildApi.startIndex(props.kbId, 20)
    if (data?.status === 'already_running') {
      message.info('图谱索引正在运行中')
    } else if (data?.status === 'failed') {
      message.error(data?.message || '提交失败，请稍后重试')
    } else {
      const queued = Number(data?.queued_count ?? 0)
      if (queued > 0) {
        message.success(`图谱索引任务已提交：本次待构建 ${queued.toLocaleString('zh-CN')} 段`)
      } else {
        message.info(data?.message || '没有待构建段落，无需索引')
      }
    }
    await loadBuildStatus()
    startBuildStatusPoll()
    emit('changed')
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '启动图谱索引失败')
  } finally {
    startingIndex.value = false
  }
}

const resetPollTimer = () => {
  if (buildPollTimer) {
    clearInterval(buildPollTimer)
    buildPollTimer = null
  }
}
const startBuildStatusPoll = () => {
  resetPollTimer()
  buildPollTimer = setInterval(() => {
    loadBuildStatus()
  }, 5000)
}
watch(
  () => buildModel.value.active,
  (active) => {
    if (active) startBuildStatusPoll()
    else resetPollTimer()
  },
  { immediate: true }
)

const confirmReset = () => {
  Modal.confirm({
    title: '清空并重建图谱',
    content: '将清空当前图谱并重抽所有段落；人工审核决策与已固定证据会在重建后按决策恢复。确定继续？',
    okText: '清空并重建',
    okButtonProps: { danger: true },
    cancelText: '取消',
    async onOk() {
      try {
        await graphBuildApi.reset(props.kbId, { confirm: true })
        message.success('图谱已清空，开始重建')
        await loadBuildStatus()
        emit('changed')
      } catch (e) {
        message.error(e?.response?.data?.detail || e?.message || '重建图谱失败')
      }
    }
  })
}

const governanceForm = reactive({
  review_policy: 'candidates_visible',
  batch_admission: 'strict',
  maker_checker: false,
  review_sla_hours: null
})
const governanceSaving = ref(false)
const governanceUnchanged = ref(true)
const gates = ref(null)
const gatesLoading = ref(false)
const releases = ref([])
const releasesLoading = ref(false)
const building = ref(false)
const publishingId = ref(null)
const members = ref([])
const memberSaving = ref(false)
const memberForm = reactive({ uid: '', capability: 'reviewer' })

const capabilityOptions = [
  { value: 'viewer', label: 'viewer（只读）' },
  { value: 'reviewer', label: 'reviewer（审核）' },
  { value: 'publisher', label: 'publisher（发布）' }
]

const admissionHint = computed(() => BATCH_ADMISSION_META[governanceForm.batch_admission]?.hint || '')
const hasActiveRelease = computed(() => releases.value.some((release) => release.status === 'ACTIVE'))

const GATE_LABELS = {
  integrity_violation: '完整性违规',
  maker_checker_violation: 'maker-checker 冲突',
  gate_reviews_pending: '门禁送审未清零',
  conflicts_open: '开放冲突未清零',
  dead_chunks: '存在死信 Chunk'
}

const gateLabel = (code) => GATE_LABELS[code] || code
const blockerDetail = (item) => {
  const detail = item.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    if (detail.count != null) return `${detail.count} 项`
    return Object.entries(detail)
      .map(([key, value]) => `${key}=${value}`)
      .join(', ')
  }
  return ''
}

const releaseStatusColor = (status) =>
  ({ ACTIVE: 'green', STAGED: 'blue', SUPERSEDED: 'default', ARCHIVED: 'default' })[status] || 'default'

const capabilityColor = (capability) =>
  ({ viewer: 'default', reviewer: 'blue', publisher: 'purple' })[capability] || 'default'

const formatTime = (iso) => (iso ? new Date(iso).toLocaleString() : '—')

const loadGovernance = async () => {
  try {
    const res = await graphApi.governanceSettings(props.kbId)
    const settings = res?.data || {}
    governanceForm.review_policy = settings.review_policy || 'candidates_visible'
    governanceForm.batch_admission = settings.batch_admission || 'strict'
    governanceForm.maker_checker = Boolean(settings.maker_checker)
    governanceForm.review_sla_hours = settings.review_sla_hours ?? null
    governanceUnchanged.value = true
  } catch {
    /* 读取失败保留默认值 */
  }
}

const saveGovernance = async () => {
  governanceSaving.value = true
  try {
    const payload = { kb_id: props.kbId }
    for (const key of ['review_policy', 'batch_admission', 'maker_checker', 'review_sla_hours']) {
      if (governanceForm[key] !== null && governanceForm[key] !== undefined) {
        payload[key] = governanceForm[key]
      }
    }
    if (governanceForm.review_sla_hours === null) {
      payload.review_sla_hours = null // 显式清除
    }
    const res = await graphApi.updateGovernanceSettings(payload)
    governanceUnchanged.value = true
    message.success(res?.data?.unchanged ? '设置无变化' : '治理设置已更新（已写入审计账本）')
    emit('changed')
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '治理设置保存失败（需要管理员权限）')
  } finally {
    governanceSaving.value = false
  }
}

const loadGates = async () => {
  gatesLoading.value = true
  try {
    const res = await graphApi.governancePublishGates(props.kbId)
    gates.value = res?.data || null
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '发布门禁评估失败')
  } finally {
    gatesLoading.value = false
  }
}

const loadReleases = async () => {
  releasesLoading.value = true
  try {
    const res = await kbReleaseApi.list(props.kbId)
    releases.value = res?.releases || []
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '发布清单加载失败')
  } finally {
    releasesLoading.value = false
  }
}

const buildRelease = async () => {
  building.value = true
  try {
    const res = await kbReleaseApi.build(props.kbId)
    message.success(`发布清单已构建：${res?.release_id || ''}（图谱决策冻结 ${res?.graph_decisions_frozen ?? 0} 条）`)
    emit('changed')
    await loadReleases()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '发布清单构建失败')
  } finally {
    building.value = false
  }
}

const publishRelease = async (release, force = false) => {
  publishingId.value = release.release_id
  try {
    await kbReleaseApi.publish(props.kbId, release.release_id, { force })
    message.success(`已发布 ${release.release_id}（生产指针已原子切换）`)
    emit('changed')
    await Promise.all([loadReleases(), loadGates()])
  } catch (e) {
    const detail = e?.response?.data?.detail
    if (detail && typeof detail === 'object' && detail.blockers) {
      Modal.warning({
        title: '发布被治理门禁阻断',
        content: `${detail.message || ''}：${detail.blockers.map((item) => gateLabel(item.code)).join('、')}`
      })
    } else {
      message.error(typeof detail === 'string' ? detail : e?.message || '发布失败')
    }
  } finally {
    publishingId.value = null
  }
}

const confirmRollback = () => {
  Modal.confirm({
    title: '回滚发布指针',
    content: '将把 active_release_id 原子切回上一个发布清单。',
    okText: '确认回滚',
    cancelText: '取消',
    onOk: async () => {
      try {
        const res = await kbReleaseApi.rollback(props.kbId)
        message.success(`已回滚至 ${res?.rolled_back_to || ''}`)
        emit('changed')
        await loadReleases()
      } catch (e) {
        message.error(e?.response?.data?.detail || e?.message || '回滚失败')
      }
    }
  })
}

const loadMembers = async () => {
  try {
    const res = await graphApi.governanceMembers(props.kbId)
    members.value = res?.data?.items || []
  } catch {
    members.value = []
  }
}

const addMember = async () => {
  if (!memberForm.uid.trim()) {
    message.warning('请填写用户 uid')
    return
  }
  memberSaving.value = true
  try {
    await graphApi.updateGovernanceMember({
      kb_id: props.kbId,
      uid: memberForm.uid.trim(),
      capability: memberForm.capability
    })
    message.success('成员能力已更新')
    memberForm.uid = ''
    await loadMembers()
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '成员更新失败（需要管理员权限）')
  } finally {
    memberSaving.value = false
  }
}

const removeMember = (member) => {
  Modal.confirm({
    title: `移除成员 ${member.uid}`,
    okText: '确认移除',
    cancelText: '取消',
    onOk: async () => {
      try {
        await graphApi.deleteGovernanceMember(props.kbId, member.uid)
        await loadMembers()
      } catch (e) {
        message.error(e?.response?.data?.detail || e?.message || '移除失败')
      }
    }
  })
}

watch(
  () => props.kbId,
  () => {
    gates.value = null
    releases.value = []
    members.value = []
    buildStatus.value = null
    loadGovernance()
    loadReleases()
    loadMembers()
    loadBuildStatus()
  },
  { immediate: true }
)
</script>

<style scoped lang="less">
.build-publish-workbench {
  height: 100%;
  overflow-y: auto;
  padding: 12px;
}

.bp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 12px;
}

.bp-card {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--gray-0);

  &--wide {
    grid-column: 1 / -1;
  }
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.card-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
}

.form-hint {
  font-size: 12px;
  color: var(--gray-500);
  line-height: 1.6;
  margin-top: 4px;
}

.build-summary-line {
  margin-top: 8px;
  color: var(--gray-700);
  font-weight: 500;
}

.build-fail-line {
  color: var(--color-warning-500);
}

.form-row {
  display: flex;
  gap: 24px;
}

.gate-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  padding: 3px 0;

  .gate-detail {
    color: var(--gray-500);
  }
}

.release-table {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;

  th,
  td {
    text-align: left;
    padding: 6px 8px;
    border-bottom: 1px dashed var(--gray-100);
  }

  th {
    color: var(--gray-500);
    font-weight: 600;
  }
}

.time-cell {
  color: var(--gray-500);
  white-space: nowrap;
}

.member-add-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.mono {
  font-family: monospace;
}
</style>
