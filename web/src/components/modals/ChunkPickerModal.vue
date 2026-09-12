<template>
  <a-modal
    :open="visible"
    title="选择参考文档块"
    width="860px"
    :footer="null"
    :mask-closable="!loading"
    @cancel="handleClose"
  >
    <div class="picker-body">
      <!-- 左：文件列表 -->
      <div class="picker-files">
        <div class="picker-section-title">文档</div>
        <a-input-search
          v-model:value="fileKeyword"
          size="small"
          placeholder="按文件名过滤"
          style="margin-bottom: 8px"
        />
        <div class="file-list">
          <a-spin v-if="filesLoading" size="small" class="picker-spin" />
          <template v-else>
            <div
              v-for="file in filteredFiles"
              :key="file.file_id"
              class="file-item"
              :class="{ active: file.file_id === selectedFileId }"
              @click="selectFile(file.file_id)"
            >
              <FileText :size="14" />
              <span class="file-name" :title="file.filename">{{ file.filename }}</span>
              <span class="file-chunk-count">{{ file.chunk_count ?? 0 }} 块</span>
            </div>
            <div v-if="!filteredFiles.length" class="picker-empty">没有匹配的文档</div>
          </template>
        </div>
      </div>

      <!-- 右：块列表 -->
      <div class="picker-chunks">
        <div class="picker-section-title">
          文档块
          <span v-if="selectedChunkIds.length" class="picked-count"
            >已选 {{ selectedChunkIds.length }}</span
          >
        </div>
        <a-input-search
          v-model:value="chunkKeyword"
          size="small"
          placeholder="按内容关键词过滤"
          :disabled="!selectedFileId"
          style="margin-bottom: 8px"
          @search="loadChunks(true)"
        />
        <div class="chunk-list">
          <a-spin v-if="chunksLoading" size="small" class="picker-spin" />
          <template v-else-if="selectedFileId">
            <div
              v-for="chunk in chunks"
              :key="chunk.chunk_id"
              class="chunk-item"
              :class="{ selected: selectedChunkIds.includes(chunk.chunk_id) }"
              @click="toggleChunk(chunk.chunk_id)"
            >
              <div class="chunk-head">
                <check-square
                  v-if="selectedChunkIds.includes(chunk.chunk_id)"
                  :size="14"
                  class="chunk-check"
                />
                <square v-else :size="14" class="chunk-check" />
                <span class="chunk-id">#{{ chunk.chunk_index }} · {{ chunk.chunk_id }}</span>
              </div>
              <div class="chunk-content">{{ chunk.content }}</div>
            </div>
            <div v-if="!chunks.length" class="picker-empty">没有匹配的文档块</div>
          </template>
          <div v-else class="picker-empty">先在左侧选择文档</div>
        </div>
        <div v-if="chunksTotal > chunkPageSize" class="chunk-pagination">
          <a-pagination
            size="small"
            :current="chunkPage"
            :page-size="chunkPageSize"
            :total="chunksTotal"
            :show-size-changer="false"
            @change="onChunkPageChange"
          />
        </div>
      </div>
    </div>

    <div class="picker-footer">
      <span class="picked-preview" :title="selectedChunkIds.join(', ')">
        {{ selectedChunkIds.length ? selectedChunkIds.join('、') : '未选择' }}
      </span>
      <div class="picker-footer-actions">
        <a-button size="small" @click="selectedChunkIds = []">清空</a-button>
        <a-button size="small" @click="handleClose">取消</a-button>
        <a-button type="primary" size="small" @click="handleConfirm">确定</a-button>
      </div>
    </div>
  </a-modal>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { CheckSquare, FileText, Square } from '@lucide/vue'
import { evaluationApi } from '@/apis/knowledge_api'
import { documentApi } from '@/apis/knowledge_api'

const props = defineProps({
  visible: { type: Boolean, default: false },
  kbId: { type: String, required: true },
  initialChunkIds: { type: Array, default: () => [] }
})

const emit = defineEmits(['update:visible', 'confirm'])

const files = ref([])
const filesLoading = ref(false)
const fileKeyword = ref('')
const selectedFileId = ref(null)

const chunks = ref([])
const chunksLoading = ref(false)
const chunksTotal = ref(0)
const chunkPage = ref(1)
const chunkPageSize = 30
const chunkKeyword = ref('')

const selectedChunkIds = ref([])
const loading = computed(() => filesLoading.value || chunksLoading.value)

const filteredFiles = computed(() => {
  const keyword = fileKeyword.value.trim().toLowerCase()
  if (!keyword) return files.value
  return files.value.filter((file) => (file.filename || '').toLowerCase().includes(keyword))
})

const loadFiles = async () => {
  filesLoading.value = true
  try {
    const response = await documentApi.listDocuments(props.kbId, { page: 1, page_size: 200 })
    files.value = (response?.data?.items || []).filter((file) => !file.is_folder)
  } catch {
    message.error('文档列表加载失败')
  } finally {
    filesLoading.value = false
  }
}

const loadChunks = async (resetPage = false) => {
  if (!selectedFileId.value) return
  if (resetPage) chunkPage.value = 1
  chunksLoading.value = true
  try {
    const response = await evaluationApi.listKbChunks(props.kbId, {
      fileId: selectedFileId.value,
      keyword: chunkKeyword.value.trim() || undefined,
      page: chunkPage.value,
      pageSize: chunkPageSize
    })
    chunks.value = response?.data?.items || []
    chunksTotal.value = response?.data?.pagination?.total_items || 0
  } catch (error) {
    message.error(error?.response?.data?.detail || '文档块加载失败')
  } finally {
    chunksLoading.value = false
  }
}

const selectFile = (fileId) => {
  if (selectedFileId.value === fileId) return
  selectedFileId.value = fileId
  chunkKeyword.value = ''
  loadChunks(true)
}

const toggleChunk = (chunkId) => {
  const index = selectedChunkIds.value.indexOf(chunkId)
  if (index >= 0) {
    selectedChunkIds.value.splice(index, 1)
  } else if (selectedChunkIds.value.length >= 20) {
    message.warning('参考文档块最多选择 20 个')
  } else {
    selectedChunkIds.value.push(chunkId)
  }
}

const onChunkPageChange = (page) => {
  chunkPage.value = page
  loadChunks()
}

const handleConfirm = () => {
  emit('confirm', [...selectedChunkIds.value])
  emit('update:visible', false)
}

const handleClose = () => {
  emit('update:visible', false)
}

watch(
  () => props.visible,
  (open) => {
    if (open) {
      selectedChunkIds.value = [...props.initialChunkIds]
      selectedFileId.value = null
      chunks.value = []
      chunkKeyword.value = ''
      fileKeyword.value = ''
      loadFiles()
    }
  }
)
</script>

<style lang="less" scoped>
.picker-body {
  display: flex;
  gap: 12px;
  height: 480px;
}

.picker-files {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.picker-chunks {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.picker-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-800);
  margin-bottom: 8px;
  display: flex;
  justify-content: space-between;
  align-items: center;

  .picked-count {
    color: var(--color-primary-700);
    font-weight: 500;
  }
}

.file-list,
.chunk-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  padding: 6px;
}

.picker-spin {
  display: flex;
  justify-content: center;
  margin-top: 32px;
}

.picker-empty {
  text-align: center;
  color: var(--gray-400);
  font-size: 12px;
  padding: 24px 0;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--gray-700);

  &:hover {
    background: var(--gray-50);
  }

  &.active {
    background: var(--main-10, rgba(22, 119, 255, 0.08));
    color: var(--color-primary-700);
  }

  .file-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-chunk-count {
    font-size: 11px;
    color: var(--gray-400);
    flex-shrink: 0;
  }
}

.chunk-item {
  padding: 8px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
  margin-bottom: 6px;

  &:hover {
    background: var(--gray-50);
  }

  &.selected {
    border-color: var(--color-primary-200, #91caff);
    background: var(--main-10, rgba(22, 119, 255, 0.06));
  }

  .chunk-head {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--gray-600);

    .chunk-check {
      color: var(--color-primary-600, #1677ff);
    }
  }

  .chunk-content {
    margin-top: 4px;
    font-size: 12px;
    color: var(--gray-700);
    line-height: 1.5;
    max-height: 66px;
    overflow: hidden;
    word-break: break-all;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
  }
}

.chunk-pagination {
  padding-top: 8px;
  text-align: right;
}

.picker-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-top: 12px;

  .picked-preview {
    flex: 1;
    min-width: 0;
    font-size: 12px;
    color: var(--gray-500);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
  }

  .picker-footer-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }
}
</style>
