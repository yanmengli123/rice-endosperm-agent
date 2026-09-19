<template>
  <div class="dataset-import-panel">
    <div class="panel-header">
      <div class="panel-title">
        <Table :size="18" />
        <div>
          <h3>数据集导入（Canonical Commit）</h3>
          <p>
            上传 CSV 原件 → 预检与列映射确认 → 行级规范记录落库 → 确定性投影 + separator
            索引。分块由契约托管，一块一条记录。
          </p>
        </div>
      </div>
    </div>

    <!-- 第一步：选择文件 -->
    <div class="step-card">
      <div class="step-title"><span class="step-no">1</span>选择数据集原件</div>
      <label class="file-pick" :class="{ disabled: busy }">
        <input type="file" accept=".csv,.tsv" :disabled="busy" @change="onFileChange" />
        <FileUp :size="18" />
        <span v-if="selectedFile"
          >{{ selectedFile.name }}（{{ formatFileSize(selectedFile.size) }}）</span
        >
        <span v-else class="pick-hint"
          >点击选择 .csv / .tsv 文件（UTF-8 优先，预检会检测编码）</span
        >
      </label>
      <div class="step-actions">
        <a-button
          type="primary"
          :disabled="!selectedFile || busy"
          :loading="previewing"
          @click="runPreview"
        >
          开始预检
        </a-button>
        <a-button v-if="preview" :disabled="busy" @click="reset">重置</a-button>
      </div>
    </div>

    <!-- 第二步：预检结果与映射确认 -->
    <div v-if="preview" class="step-card">
      <div class="step-title"><span class="step-no">2</span>预检结果与列映射确认</div>

      <div class="meta-grid">
        <div class="meta-item">
          <span class="meta-label">文件</span><span>{{ preview.filename }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">编码</span><span>{{ preview.encoding }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">分隔符</span><span>{{ previewDelimiterLabel }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">数据行数</span><span>{{ preview.row_count }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">Schema Hash</span
          ><span class="mono">{{ preview.schema_hash }}</span>
        </div>
      </div>

      <div class="columns-table-wrap">
        <table class="columns-table">
          <thead>
            <tr>
              <th style="width: 48px">#</th>
              <th>列名</th>
              <th style="width: 90px">推断类型</th>
              <th style="width: 110px">非空 / 填充率</th>
              <th>样本值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="col in preview.columns" :key="col.index">
              <td class="mono">{{ col.index }}</td>
              <td class="col-name">{{ col.name }}</td>
              <td>
                <span class="type-tag" :class="col.inferred_type">{{ col.inferred_type }}</span>
              </td>
              <td>{{ col.non_empty }} / {{ formatFillRate(col.fill_rate) }}</td>
              <td class="samples">{{ (col.samples || []).join(' · ') }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="mapping-form">
        <template v-if="contractKey === 'csv_qa'">
          <div class="mapping-field">
            <div class="mapping-label">问题列（question_col）<em class="required">*</em></div>
            <a-select
              v-model:value="mapping.question_col"
              :options="columnOptions"
              placeholder="必须显式确认问题列，系统绝不默认取前两列"
              allow-clear
            />
          </div>
          <div class="mapping-field">
            <div class="mapping-label">答案列（answer_col）<em class="required">*</em></div>
            <a-select
              v-model:value="mapping.answer_col"
              :options="columnOptions"
              placeholder="必须显式确认答案列"
              allow-clear
            />
          </div>
        </template>
        <template v-else>
          <div class="mapping-field">
            <div class="mapping-label">业务主键列（identity_column，可选）</div>
            <a-select
              v-model:value="mapping.identity_column"
              :options="columnOptions"
              placeholder="建议选择跨版本稳定的主键列（如缩写 / 编号）"
              allow-clear
            />
            <div v-if="identityNote" class="mapping-note">{{ identityNote }}</div>
            <div v-else class="mapping-note">
              未选择主键时按 dataset_revision_id + row_number
              生成记录标识，不可跨数据集版本稳定引用。
            </div>
          </div>
        </template>
        <div v-if="validationIssues.length > 0" class="validation-issues">
          <AlertTriangle :size="14" />
          <ul>
            <li v-for="(issue, index) in validationIssues" :key="index">{{ issue }}</li>
          </ul>
        </div>
      </div>

      <div class="step-actions">
        <a-button
          type="primary"
          :disabled="!canImport || busy"
          :loading="importing"
          @click="runImport"
        >
          确认映射并导入（Canonical Commit）
        </a-button>
      </div>
    </div>

    <!-- 第三步：导入结果 -->
    <div v-if="importResult" class="step-card success-card">
      <div class="step-title">
        <span class="step-no done"><Check :size="14" /></span>
        导入完成
      </div>
      <div class="meta-grid">
        <div class="meta-item">
          <span class="meta-label">Dataset Revision</span
          ><span class="mono">{{ importResult.dataset_revision_id }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">状态</span><span>{{ importResult.status }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">总行数</span><span>{{ importResult.row_count }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">有效记录</span><span>{{ importResult.valid_record_count }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">无效行</span><span>{{ importResult.invalid_row_count }}</span>
        </div>
        <div class="meta-item">
          <span class="meta-label">索引状态</span>
          <span :class="{ 'index-failed': isIndexFailed }">{{
            importResult.index_status || 'indexed'
          }}</span>
        </div>
      </div>
      <div class="step-actions">
        <a-button @click="reset">继续导入下一个数据集</a-button>
      </div>
    </div>

    <div v-if="errorMessage" class="error-alert">
      <AlertTriangle :size="16" />
      <span>{{ errorMessage }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { AlertTriangle, Check, FileUp, Table } from '@lucide/vue'
import { datasetApi } from '@/apis/knowledge_api'
import { formatFileSize } from '@/utils/file_utils'

const props = defineProps({
  kbId: { type: String, required: true },
  contractKey: { type: String, required: true }
})

const emit = defineEmits(['imported'])

const selectedFile = ref(null)
const previewing = ref(false)
const preview = ref(null)
const importing = ref(false)
const importResult = ref(null)
const errorMessage = ref('')
const mapping = reactive({
  identity_column: null,
  question_col: null,
  answer_col: null
})

const busy = computed(() => previewing.value || importing.value)
const isQaContract = computed(() => props.contractKey === 'csv_qa')

const previewDelimiterLabel = computed(() => {
  const delimiter = preview.value?.delimiter
  if (delimiter === '\t') return 'Tab（\\t）'
  if (delimiter === ',') return '逗号（,）'
  return JSON.stringify(delimiter)
})

const columnOptions = computed(() =>
  (preview.value?.columns || []).map((col) => ({ value: col.name, label: col.name }))
)

const identityNote = computed(() => preview.value?.suggested_mapping?.identity_note || '')

const validationIssues = computed(() => {
  const validation = preview.value?.qa_validation || preview.value?.record_validation || {}
  const issues = validation.issues || []
  return validation.fatal ? ['存在致命数据问题，无法导入：', ...issues] : issues
})

const canImport = computed(() => {
  if (!preview.value) return false
  if (isQaContract.value) {
    return Boolean(mapping.question_col && mapping.answer_col)
  }
  return true
})

const isIndexFailed = computed(() =>
  String(importResult.value?.index_status || '').startsWith('index_failed')
)

function onFileChange(event) {
  const file = event.target.files?.[0] || null
  selectedFile.value = file
  preview.value = null
  importResult.value = null
  errorMessage.value = ''
  if (file) {
    runPreview()
  }
}

function applySuggestedMapping(payload) {
  const suggestion = payload?.suggested_mapping || {}
  mapping.identity_column = suggestion.identity_column || null
  mapping.question_col = suggestion.question_col || null
  mapping.answer_col = suggestion.answer_col || null
}

async function runPreview() {
  if (!selectedFile.value || previewing.value) return
  previewing.value = true
  errorMessage.value = ''
  preview.value = null
  importResult.value = null
  try {
    const payload = await datasetApi.previewCsvDataset(props.kbId, selectedFile.value)
    preview.value = payload
    applySuggestedMapping(payload)
  } catch (error) {
    errorMessage.value = error?.message || '数据集预检失败'
  } finally {
    previewing.value = false
  }
}

async function runImport() {
  if (!canImport.value || importing.value) return
  importing.value = true
  errorMessage.value = ''
  try {
    const payload = await datasetApi.importCsvDataset(props.kbId, selectedFile.value, {
      ...(isQaContract.value
        ? { question_col: mapping.question_col, answer_col: mapping.answer_col }
        : { identity_column: mapping.identity_column || undefined })
    })
    importResult.value = payload
    message.success(`数据集导入完成：${payload.valid_record_count ?? payload.row_count} 条有效记录`)
    emit('imported', payload)
  } catch (error) {
    errorMessage.value = error?.message || '数据集导入失败'
  } finally {
    importing.value = false
  }
}

function reset() {
  selectedFile.value = null
  preview.value = null
  importResult.value = null
  errorMessage.value = ''
  mapping.identity_column = null
  mapping.question_col = null
  mapping.answer_col = null
}

function formatFillRate(rate) {
  const percent = Math.round(Number(rate || 0) * 100)
  return `${percent}%`
}
</script>

<style scoped>
.dataset-import-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 960px;
}

.panel-header {
  .panel-title {
    display: flex;
    align-items: flex-start;
    gap: 10px;

    svg {
      margin-top: 4px;
      color: var(--main-color);
    }

    h3 {
      margin: 0;
      font-size: 16px;
      font-weight: 600;
    }

    p {
      margin: 4px 0 0;
      font-size: 13px;
      color: var(--gray-500);
      line-height: 1.6;
    }
  }
}

.step-card {
  padding: 16px;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: var(--gray-0);
}

.success-card {
  border-color: var(--color-success-200, #b7e2c4);
  background: var(--color-success-50, #f0faf3);
}

.step-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;

  .step-no {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--main-color);
    color: #fff;
    font-size: 12px;
    flex-shrink: 0;

    &.done {
      background: var(--color-success-500, #389e6d);
    }
  }
}

.file-pick {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border: 1px dashed var(--gray-300);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.2s;
  font-size: 13px;

  &:hover:not(.disabled) {
    border-color: var(--main-color);
    background: var(--main-50, #f5f8ff);
  }

  &.disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  input[type='file'] {
    display: none;
  }

  .pick-hint {
    color: var(--gray-500);
  }
}

.step-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px 16px;
  margin-bottom: 12px;

  .meta-item {
    display: flex;
    gap: 8px;
    font-size: 13px;
    align-items: baseline;
    min-width: 0;

    .meta-label {
      color: var(--gray-500);
      flex-shrink: 0;
    }

    > span:last-child {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }
}

.mono {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
}

.columns-table-wrap {
  overflow-x: auto;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
}

.columns-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;

  th,
  td {
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid var(--gray-100, #f0f0f0);
  }

  th {
    background: var(--gray-50);
    color: var(--gray-600, #595959);
    font-weight: 500;
    white-space: nowrap;
  }

  .col-name {
    font-weight: 600;
    white-space: nowrap;
  }

  .samples {
    color: var(--gray-500);
    max-width: 360px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.type-tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11.5px;

  &.numeric {
    background: var(--main-50, #f0f5ff);
    color: var(--main-color);
  }

  &.text {
    background: var(--gray-100, #f5f5f5);
    color: var(--gray-600, #595959);
  }

  &.empty {
    background: var(--color-warning-50, #fffbe6);
    color: var(--color-warning-700, #d48806);
  }
}

.mapping-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}

.mapping-field {
  .mapping-label {
    font-size: 13px;
    font-weight: 500;
    margin-bottom: 6px;

    .required {
      color: #ff4d4f;
      font-style: normal;
      margin-left: 2px;
    }
  }

  .mapping-note {
    margin-top: 6px;
    font-size: 12px;
    color: var(--gray-500);
    line-height: 1.6;
  }
}

.validation-issues {
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--color-warning-50, #fffbe6);
  border: 1px solid var(--color-warning-200, #ffe58f);
  color: var(--color-warning-700, #d48806);
  font-size: 12.5px;

  svg {
    flex-shrink: 0;
    margin-top: 2px;
  }

  ul {
    margin: 0;
    padding-left: 16px;
  }
}

.index-failed {
  color: #ff4d4f;
}

.error-alert {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: #fff1f0;
  border: 1px solid #ffa39e;
  color: #cf1322;
  font-size: 13px;
  line-height: 1.6;

  svg {
    flex-shrink: 0;
    margin-top: 2px;
  }
}
</style>
