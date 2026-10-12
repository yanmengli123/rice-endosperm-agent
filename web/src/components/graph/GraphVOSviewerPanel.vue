<template>
  <div class="vosviewer-panel">
    <p>使用 VOSviewer 1.6.21 分析当前范围的实体关联、社区和密度。</p>
    <p>导出与本面板同源的实体和去重事实，包含全部已加载类型，不受 Top 类型折叠影响。</p>
    <p class="semantic-note">
      VOSviewer 使用无向连线，强度为不同三元组数量。原始方向、谓词、审核状态和证据定位保存在分析包中。
      算法社区与实体类型分别保留，聚类及密度不作为事实证据。
    </p>
    <label v-if="candidateScope" class="candidate-choice">
      <input v-model="allowCandidates" type="checkbox" :disabled="busy" />
      允许包含候选关系，仅用于探索分析
    </label>
    <p v-if="candidateScope" class="semantic-note">
      正式分析请先将图谱显示过滤切换为“仅已审核”并刷新。
    </p>
    <p v-if="truncated" class="semantic-note">本次仅导出已加载子集，截断信息会保存在清单中。</p>
    <p v-if="!snapshotToken" class="semantic-note">请刷新图谱以获取可导出的快照。</p>
    <p v-if="entityIdentityMissing" class="semantic-note">当前图谱缺少稳定 entity_id，请重建图谱后再导出。</p>
    <p v-else-if="entityCount < 3" class="semantic-note">VOSviewer 至少需要 3 个实体，请扩大图谱范围后再导出。</p>
    <div class="actions">
      <a-button
        type="primary"
        size="small"
        :loading="busy"
        :disabled="pending || !snapshotToken || entityIdentityMissing || entityCount < 3 || (candidateScope && !allowCandidates)"
        @click="download"
      >导出 VOSviewer 分析包</a-button>
      <a-button size="small" :disabled="busy || pending" @click="emit('refresh')">刷新快照</a-button>
    </div>
    <p v-if="errorText" role="alert">{{ errorText }}</p>
    <p v-if="successText" class="semantic-note" role="status">{{ successText }}</p>
    <ol>
      <li>解压后双击 open-with-vosviewer.cmd，或命令行 VOSviewer.exe -json vosviewer.json：打开即显示地图（包内已含确定性种子坐标）。</li>
      <li>种子坐标与连通分量仅作初始视图；如需 VOSviewer 官方布局与聚类，在查看器内重排后另存。</li>
      <li>方向、谓词、审核状态与证据定位以 facts.jsonl 为准；孤立实体与自环见 manifest.json。</li>
      <li>使用稻芯智析客户端时，可在设置的“应用与更新”中选择 VOSviewer 程序，再打开分析包的 manifest.json，由客户端校验并启动。</li>
    </ol>
    <p class="semantic-note">私有图谱保持本地或企业内网使用。VOSviewer 的 Share 会上传并公开分享地图。</p>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { graphApi } from '@/apis/graph_api'
import { useUserStore } from '@/stores/user'
import { isGraphAccessDenied } from '@/utils/graph/graphAccess'

const props = defineProps({
  kbId: { type: String, required: true },
  data: { type: Object, default: null },
  pending: { type: Boolean, default: false },
  truncated: { type: Boolean, default: false }
})
const emit = defineEmits(['refresh', 'access-denied'])
const userStore = useUserStore()
const busy = ref(false)
const errorText = ref('')
const successText = ref('')
const allowCandidates = ref(false)
const snapshotToken = computed(() => props.data?.meta?.vosviewer_snapshot?.token || '')
const entities = computed(() => (props.data?.nodes || [])
  .filter((node) => node.node_kind !== 'chunk' && !(node.labels || []).includes('Chunk')))
const entityIdentityMissing = computed(() => entities.value.some((node) => !node.entity_id && !node.properties?.entity_id))
const entityCount = computed(() => new Set(entities.value
  .map((node) => node.entity_id || node.properties?.entity_id)
  .filter(Boolean)).size)
const candidateScope = computed(() => props.data?.meta?.review_policy !== 'approved_only')
let requestSeq = 0

watch([() => props.kbId, snapshotToken, () => userStore.token], () => {
  requestSeq += 1
  busy.value = false
  allowCandidates.value = false
  errorText.value = ''
  successText.value = ''
})
onUnmounted(() => { requestSeq += 1 })

async function download() {
  if (busy.value || props.pending || !snapshotToken.value || entityIdentityMissing.value || entityCount.value < 3) return
  const seq = ++requestSeq
  busy.value = true
  errorText.value = ''
  successText.value = ''
  try {
    const response = await graphApi.exportVOSviewer(props.kbId, snapshotToken.value, allowCandidates.value)
    const blob = await response.blob()
    if (seq !== requestSeq) return
    const labels = {
      entities: '实体',
      facts: '去重事实',
      links: '无向连线',
      isolated: '孤立实体',
      self_loops: '自环'
    }
    const rawSummary =
      response.headers.get('X-Export-Evidence-Summary') ||
      response.headers.get('x-export-evidence-summary')
    const summary = String(rawSummary || '')
      .split(';')
      .map((token) => token.trim())
      .filter(Boolean)
      .map((token) => {
        const [key, value] = token.split('=').map((item) => (item || '').trim())
        if (key === 'exploration') return value === 'true' ? '含 AI 候选（探索用）' : ''
        const label = labels[key]
        return label && value ? `${label} ${value}` : ''
      })
      .filter(Boolean)
      .join(' · ')
    successText.value = summary ? `分析包已下载（${summary}）。解压后双击 open-with-vosviewer.cmd 打开即显示地图。` : '分析包已下载。解压后双击 open-with-vosviewer.cmd 打开即显示地图。'
    const filename = `vosviewer-${props.data.meta.vosviewer_snapshot.id.slice(0, 16)}.zip`
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    if (seq !== requestSeq) return
    if (isGraphAccessDenied(error)) {
      emit('access-denied')
      return
    }
    errorText.value = error.message || '分析包导出失败，请刷新后重试。'
  } finally {
    if (seq === requestSeq) busy.value = false
  }
}
</script>

<style scoped lang="less">
.vosviewer-panel {
  color: var(--gray-800);
  line-height: 1.7;
  p { margin: 10px 0; }
  ol { padding-left: 20px; }
}
.semantic-note { color: var(--gray-600); font-size: 12px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
.candidate-choice { display: flex; align-items: flex-start; gap: 6px; }
</style>
