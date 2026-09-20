<template>
  <div class="graph-source-assets">
    <div class="assets-header">
      <div class="assets-title">
        <span>规范图谱源资产</span>
        <span class="assets-subtitle">
          上传的节点/关系/审计源文件按导入批次登记；源资产不单独删除，随导入批次回滚
        </span>
      </div>
      <a-button size="small" :loading="loading" @click="loadAssets">刷新</a-button>
    </div>

    <a-alert
      v-if="backfilledCount > 0"
      type="info"
      show-icon
      class="backfill-note"
      :message="`${backfilledCount} 项为存量回填登记（原始文件名/大小未知，以批次名近似）`"
    />

    <a-table
      v-if="assets.length > 0"
      :columns="columns"
      :data-source="assets"
      row-key="asset_id"
      size="small"
      :pagination="{ pageSize: 20, showTotal: (total) => `共 ${total} 项` }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'role'">
          <a-tag :color="roleColor(record.role)">{{ roleLabel(record.role) }}</a-tag>
        </template>
        <template v-else-if="column.key === 'sha256'">
          <span class="sha-text" :title="record.sha256">{{ shortSha(record.sha256) }}</span>
        </template>
        <template v-else-if="column.key === 'size_bytes'">
          {{ record.size_bytes != null ? formatSize(record.size_bytes) : '—' }}
        </template>
        <template v-else-if="column.key === 'lifecycle_status'">
          <a-tag :color="record.lifecycle_status === 'ACTIVE' ? 'green' : 'default'">
            {{ record.lifecycle_status === 'ACTIVE' ? '有效' : '已回滚' }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'original_filename'">
          <span
            :title="record.backfilled ? '存量回填（原始文件名未知）' : record.original_filename"
          >
            {{ record.original_filename }}
            <a-tag v-if="record.backfilled" class="backfill-tag">回填</a-tag>
          </span>
        </template>
      </template>
    </a-table>

    <a-empty
      v-else-if="!loading"
      description="该知识库还没有登记的源资产（上传图谱导入文件后自动登记）"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { graphImportApi } from '@/apis/knowledge_api'

const props = defineProps({
  kbId: { type: String, required: true }
})

const loading = ref(false)
const assets = ref([])

const backfilledCount = computed(() => assets.value.filter((item) => item.backfilled).length)

const columns = [
  { title: '文件', key: 'original_filename', ellipsis: true },
  { title: '类型', key: 'role', width: 110 },
  { title: '大小', key: 'size_bytes', width: 100 },
  { title: 'SHA256', key: 'sha256', width: 150 },
  { title: '导入批次', key: 'import_id', width: 180, ellipsis: true },
  { title: '状态', key: 'lifecycle_status', width: 90 },
  { title: '登记时间', key: 'created_at', width: 170 }
]

const roleLabel = (role) =>
  ({ nodes: '节点 CSV', relationships: '关系 CSV', audit: '审计说明' })[role] || role

const roleColor = (role) =>
  ({ nodes: 'blue', relationships: 'cyan', audit: 'purple' })[role] || 'default'

const shortSha = (sha) => (sha ? `${sha.slice(0, 12)}…` : '—')

const formatSize = (bytes) => {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const loadAssets = async () => {
  loading.value = true
  try {
    const response = await graphImportApi.listSourceAssets(props.kbId)
    assets.value = response.items || []
  } catch (error) {
    console.error('加载源资产失败:', error)
    message.error(error.message || '加载源资产失败')
  } finally {
    loading.value = false
  }
}

onMounted(loadAssets)
</script>

<style scoped lang="less">
.graph-source-assets {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.assets-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.assets-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-weight: 600;

  .assets-subtitle {
    font-weight: 400;
    font-size: 12px;
    color: var(--gray-600);
  }
}

.sha-text {
  font-family: monospace;
  font-size: 12px;
}

.backfill-tag {
  margin-left: 6px;
  font-size: 11px;
}
</style>
