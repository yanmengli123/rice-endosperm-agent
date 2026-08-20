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

          <div
            v-if="(result(resultContent).knowledge_source_status || []).length"
            class="source-status-list"
          >
            <div
              v-for="item in result(resultContent).knowledge_source_status"
              :key="`${item.kb_id}-${item.source || 'legacy'}`"
            >
              <strong>{{ item.kb_name }}</strong>
              <span v-if="item.source">{{ item.source }}</span>
              <span>{{ item.capability_status || item.document_status }}</span>
              <span>{{ item.query_status || item.graph_status }}</span>
              <span v-if="item.hit_count !== null && item.hit_count !== undefined">
                {{ item.hit_count }} hits
              </span>
              <span
                v-if="
                  item.eligible_evidence_count !== null &&
                  item.eligible_evidence_count !== undefined
                "
              >
                {{ item.eligible_evidence_count }} 条合格证据
              </span>
              <span v-else-if="item.structured_status">{{ item.structured_status }}</span>
            </div>
          </div>

          <div v-if="(result(resultContent).sources_used || []).length" class="sources-used">
            <span>本次实际使用</span>
            <span v-for="item in result(resultContent).sources_used" :key="item.kb_id">
              {{ item.kb_id }} · {{ (item.source_types || []).join(' + ') }} · {{ item.hits }} hits
            </span>
          </div>

          <div class="scope-stats">
            <div>
              <strong>{{
                completeness(resultContent).exact_relation_count ??
                summary(resultContent).raw_hits ??
                0
              }}</strong>
              <span>精确关系</span>
            </div>
            <div>
              <strong>{{
                completeness(resultContent).eligible_claim_count ??
                summary(resultContent).claim_count ??
                0
              }}</strong>
              <span>合格 Claim</span>
            </div>
            <div>
              <strong>{{
                completeness(resultContent).eligible_evidence_count ??
                summary(resultContent).evidence_count ??
                0
              }}</strong>
              <span>合格 Evidence</span>
            </div>
            <div>
              <strong
                :class="`completeness-${String(completeness(resultContent).status || '').toLowerCase()}`"
              >
                {{ completeness(resultContent).status || 'N/A' }}
              </strong>
              <span>完整性</span>
            </div>
          </div>

          <div v-if="hasValidation(resultContent)" class="contract-validation">
            <span>
              Claim Validator
              <strong
                :class="`validator-${String(validation(resultContent).claims?.status || '').toLowerCase()}`"
              >
                {{ validation(resultContent).claims?.status || 'N/A' }}
              </strong>
            </span>
            <span>
              Citation Validator
              <strong
                :class="`validator-${String(validation(resultContent).citations?.status || '').toLowerCase()}`"
              >
                {{ validation(resultContent).citations?.status || 'N/A' }}
              </strong>
            </span>
            <span v-if="completeness(resultContent).uncited_exact_relation_count">
              {{
                completeness(resultContent).uncited_exact_relation_count
              }}
              条关系仅完成扫描，未形成可引用 Claim
            </span>
          </div>

          <details v-if="structuredRows(resultContent).length" class="structured-results" open>
            <summary>规范科研结果（{{ structuredRows(resultContent).length }}）</summary>
            <div class="structured-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>实体</th>
                    <th>关系语义</th>
                    <th>对象</th>
                    <th>证据</th>
                    <th>PMID / DOI</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in displayedStructuredRows(resultContent)" :key="row.claim_id">
                    <td>
                      <strong>{{ row.subject }}</strong>
                    </td>
                    <td>
                      <span class="relation-group">{{
                        relationGroupLabel(row.relation_group)
                      }}</span
                      ><small>{{ row.relation }}</small>
                    </td>
                    <td>{{ row.object }}</td>
                    <td>
                      <code v-for="id in row.evidence_ids || []" :key="id">{{ id }}</code>
                    </td>
                    <td>
                      <span v-for="pmid in row.pmids || []" :key="`pmid-${pmid}`"
                        >PMID {{ pmid }}</span
                      >
                      <span v-for="doi in row.dois || []" :key="`doi-${doi}`">DOI {{ doi }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <button
              v-if="structuredRows(resultContent).length > displayLimit"
              type="button"
              class="expand-results"
              @click="showAllStructured = !showAllStructured"
            >
              {{
                showAllStructured
                  ? '收起到前 20 条'
                  : `展开全部 ${structuredRows(resultContent).length} 条`
              }}
            </button>
          </details>

          <div v-if="(result(resultContent).warnings || []).length" class="scope-warnings">
            <div v-for="warning in result(resultContent).warnings" :key="warning">
              {{ warning }}
            </div>
          </div>

          <details v-if="(result(resultContent).evidence || []).length" class="evidence-details">
            <summary>
              查看科研证据卡片（{{ (result(resultContent).evidence || []).length }}）
            </summary>
            <div class="evidence-list">
              <article
                v-for="item in result(resultContent).evidence"
                :key="item.evidence_id"
                class="evidence-card"
              >
                <header>
                  <strong>{{ item.subject?.name || '未命名实体' }}</strong>
                  <span>{{ outcomeLabel(item.outcome_class) }}</span>
                  <span :class="`status-${(item.evidence_status || '').toLowerCase()}`">
                    {{ item.evidence_status }}
                  </span>
                </header>
                <div class="evidence-grid">
                  <span
                    ><small>观察效应</small
                    >{{ item.observed_effect || item.direction || '未记录' }}</span
                  >
                  <span><small>实验材料</small>{{ materialLabel(item) }}</span>
                  <span><small>实验条件</small>{{ conditionLabel(item.condition) }}</span>
                  <span><small>证据等级</small>{{ item.evidence_level || '未分级' }}</span>
                  <span><small>PMID</small>{{ item.pmid || '无精确 PMID' }}</span>
                  <span
                    ><small>Evidence ID</small><code>{{ item.evidence_id }}</code></span
                  >
                </div>
                <details class="evidence-audit">
                  <summary>审计详情</summary>
                  <dl>
                    <dt>关系</dt>
                    <dd>{{ item.observed_relation || item.predicate || '未记录' }}</dd>
                    <dt>DOI</dt>
                    <dd>{{ item.doi || '未记录' }}</dd>
                    <dt>知识库</dt>
                    <dd>{{ item.kb_name || item.kb_id }}</dd>
                    <dt>可支撑结论</dt>
                    <dd>{{ item.claim_eligible ? '是' : '否（仅作上下文）' }}</dd>
                    <dt>原始证据</dt>
                    <dd>{{ item.evidence_quote || item.content || '未记录' }}</dd>
                  </dl>
                </details>
              </article>
            </div>
          </details>
        </template>
        <div v-else class="scope-empty">知识范围检索结果不可解析</div>
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import { computed, ref } from 'vue'
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
const completeness = (content) => result(content)?.completeness || {}
const validation = (content) => result(content)?.validation || {}
const hasValidation = (content) =>
  ['claims', 'citations'].some((key) => {
    const status = validation(content)?.[key]?.status
    return Boolean(status && status !== 'NOT_APPLICABLE')
  })
const structuredRows = (content) => result(content)?.structured_result || []
const displayLimit = 20
const showAllStructured = ref(false)
const displayedStructuredRows = (content) =>
  showAllStructured.value ? structuredRows(content) : structuredRows(content).slice(0, displayLimit)

const relationGroupLabels = {
  FUNCTIONAL_REGULATION: '功能调控',
  PERTURBATION_EVIDENCE: '扰动证据',
  ASSOCIATION_OR_CONTEXT: '关联/背景'
}
const relationGroupLabel = (value) => relationGroupLabels[value] || value || '未分组'

const outcomeLabels = {
  DIRECT_YIELD: '直接产量',
  CONDITION_SPECIFIC_YIELD: '条件特异产量',
  YIELD_COMPONENT: '产量构成',
  GRAIN_FILLING: '灌浆',
  GRAIN_MORPHOLOGY: '粒型',
  QUALITY: '品质',
  OTHER: '其他证据'
}
const conditionLabels = {
  HIGH_TEMPERATURE: '高温',
  DROUGHT: '干旱',
  SALT_STRESS: '盐胁迫',
  LOW_NITROGEN: '低氮'
}
const outcomeLabel = (value) => outcomeLabels[value] || value || '其他证据'
const conditionLabel = (value) => conditionLabels[value] || value || '未记录/常规条件'
const materialLabel = (item) =>
  [item.experimental_subject_type, item.subject_material].filter(Boolean).join(' · ') || '未记录'
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

.source-status-list,
.sources-used {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 9px;
  padding: 7px;
  border-radius: 6px;
  background: var(--gray-25);
  color: var(--gray-600);
  font-size: 11px;
}

.source-status-list > div {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;

  strong {
    margin-right: auto;
    color: var(--gray-800);
  }

  span {
    padding: 1px 5px;
    border-radius: 4px;
    background: var(--gray-75);
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

.contract-validation {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 9px;
  color: var(--gray-600);
  font-size: 11px;

  span {
    padding: 4px 7px;
    border-radius: 5px;
    background: var(--gray-25);
  }

  strong {
    margin-left: 4px;
  }

  .validator-pass {
    color: var(--color-success-700);
  }

  .validator-fail {
    color: var(--color-error-700);
  }
}

.structured-results {
  margin-top: 10px;

  summary {
    cursor: pointer;
    color: var(--gray-800);
    font-size: 12px;
    font-weight: 600;
  }
}

.structured-table-wrap {
  max-height: 420px;
  margin-top: 8px;
  overflow: auto;
  border: 1px solid var(--gray-100);
  border-radius: 6px;

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }

  th,
  td {
    padding: 7px;
    border-bottom: 1px solid var(--gray-100);
    text-align: left;
    vertical-align: top;
  }

  th {
    position: sticky;
    top: 0;
    background: var(--gray-25);
    color: var(--gray-600);
  }

  td span,
  td code,
  td small {
    display: block;
    margin-top: 2px;
  }

  td code {
    overflow-wrap: anywhere;
    color: var(--gray-600);
  }
}

.relation-group {
  color: var(--color-primary-700);
  font-weight: 600;
}

.expand-results {
  margin-top: 8px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-primary-600);
  cursor: pointer;
  font-size: 12px;
}

.completeness-pass {
  color: var(--color-success-700) !important;
}

.completeness-fail,
.completeness-unverified {
  color: var(--color-warning-700) !important;
}

.evidence-details {
  margin-top: 9px;
  color: var(--gray-600);
  font-size: 11px;
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 6px;
}

.evidence-card {
  padding: 9px;
  border: 1px solid var(--gray-150);
  border-radius: 7px;
  background: var(--gray-0);

  > header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;

    strong {
      margin-right: auto;
      color: var(--gray-900);
      font-size: 13px;
    }

    span {
      padding: 2px 5px;
      border-radius: 4px;
      background: var(--gray-75);
    }
  }
}

.evidence-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
  margin-top: 8px;

  > span {
    min-width: 0;
    color: var(--gray-800);
  }

  small {
    display: block;
    margin-bottom: 2px;
    color: var(--gray-500);
  }

  code {
    display: block;
    overflow: hidden;
    color: var(--color-primary-600);
    text-overflow: ellipsis;
  }
}

.evidence-audit {
  margin-top: 8px;

  dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 4px 8px;
    margin: 6px 0 0;
  }

  dt {
    color: var(--gray-500);
  }

  dd {
    margin: 0;
    color: var(--gray-700);
    overflow-wrap: anywhere;
  }
}

@media (max-width: 720px) {
  .evidence-grid {
    grid-template-columns: 1fr;
  }
}

.scope-empty {
  color: var(--gray-500);
  font-size: 12px;
}
</style>
