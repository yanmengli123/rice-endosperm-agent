<template>
  <div class="graph-section" v-if="isGraphSupported">
    <template v-if="mode === 'workbench'">
      <!-- 治理工作台：治理头（可点击指标）+ 五个工作区 -->
      <div class="workbench-shell">
        <GovernanceHeader :summary="governance.summary" @navigate="onHeaderNavigate" />
        <div class="workbench-toolbar">
          <a-radio-group v-model:value="activeWorkspace" size="small">
            <a-radio-button value="review">
              审核工作台
              <span v-if="governance.pendingReviewCount" class="tab-badge">
                {{ governance.pendingReviewCount }}
              </span>
            </a-radio-button>
            <a-radio-button value="explorer">图谱探索</a-radio-button>
            <a-radio-button value="quality">质量治理</a-radio-button>
            <a-radio-button value="build">构建与发布</a-radio-button>
            <a-radio-button value="audit">审计日志</a-radio-button>
          </a-radio-group>
          <a-button size="small" type="text" title="切换回经典画布视图（只保留画布与队列抽屉）" @click="setClassic">
            经典视图
          </a-button>
        </div>
        <ResourceEmptyState
          v-if="showConfigEmpty"
          class="workbench-empty"
          title="暂无知识图谱"
          description="配置抽取器后，才能从当前知识库构建实体与关系。"
          :icon="Network"
          full-height
        >
          <template #actions>
            <a-button type="primary" class="lucide-icon-btn" @click="goBuildWorkspace">
              <Settings :size="16" />
              前往「构建与发布」配置抽取器
            </a-button>
          </template>
        </ResourceEmptyState>
        <div v-else class="workbench-body">
          <ReviewWorkbench
            v-show="activeWorkspace === 'review'"
            :kb-id="kbId"
            :summary="governance.summary"
            @reviewed="onChanged"
          />
          <ExplorerWorkbench
            v-show="activeWorkspace === 'explorer'"
            ref="explorerRef"
            :active="props.active && activeWorkspace === 'explorer'"
          />
          <QualityWorkbench
            v-show="activeWorkspace === 'quality'"
            :kb-id="kbId"
            :summary="governance.summary"
          />
          <BuildPublishWorkbench
            v-show="activeWorkspace === 'build'"
            :kb-id="kbId"
            @changed="onChanged"
          />
          <AuditWorkbench v-show="activeWorkspace === 'audit'" :kb-id="kbId" />
        </div>
      </div>
    </template>
    <template v-else>
      <!-- 经典视图：原画布页（保留回退通道） -->
      <div class="classic-toolbar">
        <a-button size="small" type="primary" @click="setWorkbench">
          进入治理工作台
        </a-button>
      </div>
      <div class="classic-body">
        <ExplorerWorkbench ref="classicExplorerRef" :active="props.active" />
      </div>
    </template>
  </div>
  <div class="graph-section" v-else>
    <div class="graph-disabled">
      <div class="disabled-content">
        <h4>知识图谱不可用</h4>
        <p>当前知识库类型 "{{ kbTypeLabel }}" 不支持知识图谱功能。</p>
        <p>只有 Milvus 类型的知识库支持知识图谱。</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { Network, Settings } from '@lucide/vue'
import { useDatabaseStore } from '@/stores/database'
import { useGraphGovernanceStore } from '@/stores/graphGovernance'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import GovernanceHeader from '@/components/graph/GovernanceHeader.vue'
import ReviewWorkbench from '@/components/graph/ReviewWorkbench.vue'
import ExplorerWorkbench from '@/components/graph/ExplorerWorkbench.vue'
import QualityWorkbench from '@/components/graph/QualityWorkbench.vue'
import BuildPublishWorkbench from '@/components/graph/BuildPublishWorkbench.vue'
import AuditWorkbench from '@/components/graph/AuditWorkbench.vue'
import { getKbTypeLabel } from '@/utils/kb_utils'

const MILVUS_KB_TYPE = 'milvus'

const props = defineProps({
  active: {
    type: Boolean,
    default: false
  }
})

const store = useDatabaseStore()
const governance = useGraphGovernanceStore()

const kbId = computed(() => store.kbId)
const kbType = computed(() => store.database.kb_type)
const kbTypeLabel = computed(() => getKbTypeLabel(kbType.value || 'milvus'))
const isGraphSupported = computed(() => (kbType.value?.toLowerCase() || 'milvus') === MILVUS_KB_TYPE)

const activeWorkspace = ref('review')
const explorerRef = ref(null)
const classicExplorerRef = ref(null)

const mode = computed(() => governance.mode)
const setClassic = () => governance.setMode('classic')
const setWorkbench = () => governance.setMode('workbench')

// 未配置抽取器（工作台模式）：只显示引导，不渲染各工作区。
// managed_graph 规范图谱库不走 LLM 抽取（build.configured 恒为 false），
// 不显示该阻断空态，直接进入工作区（审核/质量/发布/审计均可用）。
const isManagedGraph = computed(() => store.database?.contract_key === 'managed_graph')
const showConfigEmpty = computed(() => {
  if (isManagedGraph.value) return false
  const build = governance.summary?.build
  return mode.value === 'workbench' && governance.summary !== null && build && !build.configured
})

const onChanged = () => {
  governance.invalidate()
}

const goBuildWorkspace = () => {
  activeWorkspace.value = 'build'
}

const onHeaderNavigate = (workspace, sub) => {
  activeWorkspace.value = workspace
  if (sub === 'gate' || sub === 'conflict') {
    // 工作区内部按 sub 切队列传参留给后续；至少完成工作区跳转
    activeWorkspace.value = 'review'
  }
}

watch(
  [() => props.active, kbId, isGraphSupported, mode],
  () => {
    if (isGraphSupported.value && props.active && kbId.value) {
      governance.attach(kbId.value, { active: true })
    } else {
      governance.attach(kbId.value || '', { active: false })
    }
  },
  { immediate: true }
)
</script>

<style scoped lang="less">
.graph-section {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
  user-select: none;
}

.workbench-shell {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.workbench-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  border-bottom: 1px solid var(--gray-200);
  background: var(--gray-0);
}

.tab-badge {
  margin-left: 4px;
  padding: 0 6px;
  border-radius: 8px;
  background: var(--main-color);
  color: #fff;
  font-size: 11px;
}

.workbench-body {
  flex: 1;
  min-height: 0;
  position: relative;
}

.workbench-empty {
  position: absolute;
  inset: 0;
  z-index: 30;
  pointer-events: none;

  :deep(.resource-empty-state__actions) {
    pointer-events: auto;
  }
}

.classic-toolbar {
  display: flex;
  justify-content: flex-end;
  padding: 4px 12px;
  border-bottom: 1px solid var(--gray-200);
}

.classic-body {
  flex: 1;
  min-height: 0;
}

.graph-disabled {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
}

.disabled-content {
  text-align: center;
  color: var(--gray-400);

  h4 {
    margin-bottom: 8px;
  }
}
</style>
