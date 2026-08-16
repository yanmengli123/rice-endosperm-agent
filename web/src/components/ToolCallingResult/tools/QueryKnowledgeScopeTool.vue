<template>
  <BaseToolCall :tool-call="toolCall" :hide-params="true">
    <template #header>
      <div class="scope-tool-header">
        <span>统一知识范围检索</span>
        <span v-if="queryText" class="scope-query">{{ queryText }}</span>
      </div>
    </template>
    <template #result="{ resultContent }">
      <div class="scope-debug-panel">
        <template v-if="result(resultContent)">
          <div class="scope-debug-title">本回答知识范围</div>
          <div class="scope-debug-meta">
            <span>{{ scope(resultContent).scope_slug || 'default-qa' }}</span>
            <span>v{{ scope(resultContent).scope_version || '—' }}</span>
            <span>{{ scope(resultContent).retrieval_mode || 'KB_ONLY' }}</span>
            <span :class="scope(resultContent).allow_web ? 'web-on' : 'web-off'">
              Web {{ scope(resultContent).allow_web ? '开启' : '关闭' }}
            </span>
          </div>

          <div class="scope-kbs">
            <span v-for="kbId in scope(resultContent).kb_ids || []" :key="kbId">{{ kbId }}</span>
            <span v-if="!(scope(resultContent).kb_ids || []).length" class="empty">范围为空</span>
          </div>

          <div class="scope-stats">
            <div>
              <strong>{{ summary(resultContent).raw_hits || 0 }}</strong>
              <span>原始命中</span>
            </div>
            <div>
              <strong>{{ summary(resultContent).deduplicated_hits || 0 }}</strong>
              <span>去重证据</span>
            </div>
            <div>
              <strong>{{ statusCount(resultContent, 'STRICT') }}</strong>
              <span>STRICT</span>
            </div>
            <div>
              <strong>{{ statusCount(resultContent, 'CANDIDATE') }}</strong>
              <span>CANDIDATE</span>
            </div>
          </div>

          <div v-if="(result(resultContent).warnings || []).length" class="scope-warnings">
            <div v-for="warning in result(resultContent).warnings" :key="warning">
              {{ warning }}
            </div>
          </div>

          <details v-if="(result(resultContent).evidence || []).length" class="evidence-details">
            <summary>查看 Evidence IDs</summary>
            <div class="evidence-list">
              <div v-for="item in result(resultContent).evidence" :key="item.evidence_id">
                <code>{{ item.evidence_id }}</code>
                <span>{{ item.evidence_status }}</span>
                <span>{{ item.kb_name || item.kb_id }}</span>
              </div>
            </div>
          </details>
        </template>
        <div v-else class="scope-empty">知识范围检索结果不可解析</div>
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import { computed } from 'vue'
import BaseToolCall from '../BaseToolCall.vue'

const props = defineProps({ toolCall: { type: Object, required: true } })

const args = computed(() => {
  const value = props.toolCall.args || props.toolCall.function?.arguments
  if (typeof value === 'object') return value || {}
  try {
    return JSON.parse(value || '{}')
  } catch {
    return {}
  }
})
const queryText = computed(() => args.value.query_text || '')

let lastContent = null
let lastResult = null
const result = (content) => {
  if (content === lastContent) return lastResult
  lastContent = content
  if (typeof content === 'object') {
    lastResult = content
    return lastResult
  }
  try {
    lastResult = JSON.parse(content || 'null')
  } catch {
    lastResult = null
  }
  return lastResult
}
const scope = (content) => result(content)?.knowledge_scope_snapshot || {}
const summary = (content) => result(content)?.retrieval_summary || {}
const statusCount = (content, status) =>
  (result(content)?.evidence || []).filter((item) => item.evidence_status === status).length
</script>

<style scoped lang="less">
.scope-tool-header {
  display: flex;
  min-width: 0;
  gap: 8px;
  color: var(--gray-700);

  .scope-query {
    overflow: hidden;
    color: var(--gray-500);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.scope-debug-panel {
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
}

.scope-debug-title {
  color: var(--gray-900);
  font-size: 13px;
  font-weight: 600;
}

.scope-debug-meta,
.scope-kbs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;

  span {
    padding: 2px 6px;
    border-radius: 4px;
    background: var(--gray-75);
    color: var(--gray-600);
    font-size: 11px;
  }

  .web-off {
    background: var(--color-success-50);
    color: var(--color-success-700);
  }

  .web-on {
    background: var(--color-warning-50);
    color: var(--color-warning-700);
  }
}

.scope-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin-top: 10px;

  div {
    display: flex;
    flex-direction: column;
    padding: 7px;
    border-radius: 6px;
    background: var(--gray-25);
  }

  strong {
    color: var(--gray-900);
    font-size: 15px;
  }

  span {
    color: var(--gray-500);
    font-size: 10px;
  }
}

.scope-warnings {
  margin-top: 9px;
  padding: 7px;
  border-radius: 6px;
  background: var(--color-warning-50);
  color: var(--color-warning-800);
  font-size: 11px;
}

.evidence-details {
  margin-top: 9px;
  color: var(--gray-600);
  font-size: 11px;
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 6px;

  > div {
    display: grid;
    grid-template-columns: minmax(150px, 1fr) auto auto;
    gap: 8px;
  }

  code {
    overflow: hidden;
    text-overflow: ellipsis;
  }
}

.scope-empty {
  color: var(--gray-500);
  font-size: 12px;
}
</style>
