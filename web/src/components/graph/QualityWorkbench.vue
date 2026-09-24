<template>
  <div class="quality-workbench">
    <div class="quality-grid">
      <!-- 完整性审计：轻量计数 + 完整重验按钮 -->
      <div class="quality-card">
        <div class="card-header">
          <span class="card-title">完整性审计</span>
          <a-button size="small" :loading="integrityLoading" @click="runIntegrity"
            >逐条重验（完整审计）</a-button
          >
        </div>
        <div v-if="integrityLoading" class="card-hint">正在逐条重验引文…</div>
        <template v-else-if="integrity">
          <div class="integrity-status">
            <a-tag :color="integrity.status === 'OK' ? 'green' : 'red'">
              {{ integrity.status === 'OK' ? 'OK' : 'VIOLATION' }}
            </a-tag>
            <span class="card-hint">
              重验引文 {{ integrity.checked_quotes?.triple ?? 0 }} 条边 /
              {{ integrity.checked_quotes?.entity ?? 0 }} 条节点
            </span>
          </div>
          <div class="violation-list">
            <div
              v-for="(value, key) in integrity.violations"
              :key="key"
              class="violation-row"
              :class="{ 'violation-row--bad': value > 0 }"
            >
              <span class="violation-key">{{ key }}</span>
              <span class="violation-value">{{ value }}</span>
            </div>
          </div>
        </template>
        <div v-else class="card-hint">
          轻量计数见顶部治理条；点击「逐条重验」运行完整审计（I1–I7）。
        </div>
      </div>

      <!-- 死信与过期缓存 -->
      <div class="quality-card">
        <div class="card-header">
          <span class="card-title">数据债务</span>
        </div>
        <div class="debt-row">
          <span>死信 Chunk（累计失败 ≥6 次）</span>
          <span class="debt-value" :class="{ 'debt-value--warn': deadChunks > 0 }">{{
            deadChunks
          }}</span>
        </div>
        <div class="debt-row">
          <span>过期抽取缓存（无指纹）</span>
          <span class="debt-value" :class="{ 'debt-value--warn': staleCache > 0 }">{{
            staleCache
          }}</span>
        </div>
        <div class="card-hint">
          死信在修复模型/配置后可通过索引管理「重试索引」复活；过期缓存会在下次构建时按新指纹重抽。
        </div>
      </div>

      <!-- golden 抽检 -->
      <div class="quality-card quality-card--wide">
        <div class="card-header">
          <span class="card-title">Golden 抽检（晋升门禁标尺）</span>
          <a-space>
            <a-button size="small" :loading="goldenLoading" @click="loadGolden">刷新</a-button>
            <a-button
              size="small"
              type="primary"
              :loading="evaluating"
              :disabled="!goldenItems.length"
              @click="evaluateGolden"
            >
              用锁定配置重抽评测
            </a-button>
          </a-space>
        </div>
        <div v-if="!goldenItems.length" class="card-hint">
          尚未注册 golden 样本。对代表性 chunk
          标注期望三元组后，可度量抽取质量（P/R/F1）并作为晋升导出门禁。
        </div>
        <table v-else class="golden-table">
          <thead>
            <tr>
              <th>chunk</th>
              <th>期望三元组数</th>
              <th>标注人</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in goldenItems" :key="item.chunk_id">
              <td class="mono">{{ item.chunk_id.slice(0, 16) }}…</td>
              <td>{{ item.expected_triples?.length ?? 0 }}</td>
              <td>{{ item.created_by }}</td>
              <td>{{ item.note || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="goldenResult" class="golden-result">
          <a-tag color="blue">P {{ goldenResult.precision ?? '-' }}</a-tag>
          <a-tag color="blue">R {{ goldenResult.recall ?? '-' }}</a-tag>
          <a-tag color="blue">F1 {{ goldenResult.f1 ?? '-' }}</a-tag>
          <span class="card-hint">（重抽不写图谱，仅评测）</span>
        </div>
      </div>

      <!-- 可疑传递边 -->
      <div class="quality-card quality-card--wide">
        <div class="card-header">
          <span class="card-title">可疑传递边（R5c）</span>
          <a-button size="small" :loading="shortcutLoading" @click="loadShortcuts">扫描</a-button>
        </div>
        <div v-if="!shortcutItems.length" class="card-hint">
          A→C 直连与 A→B→C 共存、且 A→C 引文不提及 B——LLM 脑补传递推理的高危信号。
        </div>
        <div v-else class="shortcut-list">
          <div
            v-for="(item, index) in shortcutItems.slice(0, 20)"
            :key="index"
            class="shortcut-row"
          >
            <span class="shortcut-content"
              >{{ item.content || item.subject }} {{ item.via ? `(经 ${item.via})` : '' }}</span
            >
            <span class="shortcut-reason">{{ item.reason || '引文未提及中间实体' }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { graphApi } from '@/apis/graph_api'

const props = defineProps({
  kbId: { type: String, required: true },
  summary: { type: Object, default: null }
})

const integrity = ref(null)
const integrityLoading = ref(false)
const goldenItems = ref([])
const goldenLoading = ref(false)
const goldenResult = ref(null)
const evaluating = ref(false)
const shortcutItems = ref([])
const shortcutLoading = ref(false)

const deadChunks = computed(() => Number(props.summary?.build?.dead_chunks ?? 0))
const staleCache = computed(() => Number(props.summary?.build?.stale_cached_chunks ?? 0))

const runIntegrity = async () => {
  integrityLoading.value = true
  try {
    const res = await graphApi.getIntegrity(props.kbId)
    integrity.value = res?.data || null
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '完整审计失败')
  } finally {
    integrityLoading.value = false
  }
}

const loadGolden = async () => {
  goldenLoading.value = true
  try {
    const res = await graphApi.goldenSamples(props.kbId)
    goldenItems.value = res?.data?.items || []
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || 'golden 样本加载失败')
  } finally {
    goldenLoading.value = false
  }
}

const evaluateGolden = async () => {
  evaluating.value = true
  try {
    const res = await graphApi.evaluateGoldenSamples(props.kbId)
    goldenResult.value = res?.data?.overall || res?.data || null
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || 'golden 评测失败（需先锁定抽取配置）')
  } finally {
    evaluating.value = false
  }
}

const loadShortcuts = async () => {
  shortcutLoading.value = true
  try {
    const res = await graphApi.shortcutSuspects(props.kbId)
    shortcutItems.value = res?.data?.items || []
  } catch (e) {
    message.error(e?.response?.data?.detail || e?.message || '可疑传递边扫描失败')
  } finally {
    shortcutLoading.value = false
  }
}

watch(
  () => props.kbId,
  () => {
    integrity.value = null
    goldenItems.value = []
    goldenResult.value = null
    shortcutItems.value = []
  },
  { immediate: true }
)
</script>

<style scoped lang="less">
.quality-workbench {
  height: 100%;
  overflow-y: auto;
  padding: 12px;
}

.quality-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}

.quality-card {
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

.card-hint {
  font-size: 12px;
  color: var(--gray-500);
  line-height: 1.6;
}

.integrity-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.violation-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 2px 16px;
}

.violation-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--gray-500);
  padding: 2px 0;

  &--bad {
    color: var(--color-error, #cf1322);
    font-weight: 600;
  }
}

.debt-row {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--gray-700);
  padding: 4px 0;
}

.debt-value {
  font-weight: 700;
  font-variant-numeric: tabular-nums;

  &--warn {
    color: var(--color-warning-600, #d97706);
  }
}

.golden-table {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;

  th,
  td {
    text-align: left;
    padding: 4px 8px;
    border-bottom: 1px dashed var(--gray-100);
  }

  th {
    color: var(--gray-500);
    font-weight: 600;
  }
}

.golden-result {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.shortcut-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.shortcut-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
}

.shortcut-content {
  color: var(--gray-800);
}

.shortcut-reason {
  color: var(--color-warning-600, #d97706);
  flex-shrink: 0;
}

.mono {
  font-family: monospace;
}
</style>
