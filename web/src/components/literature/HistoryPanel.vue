<template>
  <section>
    <h4>当前业务备份</h4>
    <p>
      导出当前有权文献、有效正文/笔记/译文、图片/PDF、概念页快照、卡片、附件、报告，以及自己的日记和文献会话。
      可通过 ZIP 预览确认重新导入；冲突不覆盖，来源重新验证。不是整机灾备，不包含密钥、权限、计费、
      缓存、活跃任务、回收站、已删除会话或先前导入原件（后者使用下方封存归档）。
    </p>
    <button class="btn" :disabled="busy" @click="exportBusiness">导出当前业务 ZIP</button>
    <h4>我的迁移历史</h4>
    <p>
      已映射数据在对应工作区使用；冲突或无法验证来源的内容保留原件，不作为当前证据。映射重试追加回执，不覆盖人工内容，也不重放任务或计费。
    </p>
    <button class="btn" :disabled="busy" @click="load(0)">刷新迁移回执</button>
    <p v-if="notice" role="status">{{ notice }}</p>
    <h5>已封存归档</h5>
    <p>
      重新导出导入时的业务记录和原件，不包含当前工作区修改、密钥或可执行任务。旧导入需用原 ZIP
      重新预览确认后封存。
    </p>
    <ul>
      <li v-for="archive in archives" :key="archive.archive_sha256">
        {{ archive.archive_sha256.slice(0, 12) }} · {{ archive.created_at }}
        <button class="btn" :disabled="busy" @click="exportArchive(archive)">
          导出可重新导入的 ZIP
        </button>
      </li>
    </ul>
    <ul>
      <li v-for="item in items" :key="item.id">
        {{ item.category }} · {{ item.source_key }} · {{ item.status }} — {{ item.detail }}
        <button class="btn" :disabled="busy" @click="download(item)">下载原件</button>
        <button v-if="item.can_retry" class="btn" :disabled="busy" @click="retryMapping(item)">
          重试原生映射
        </button>
      </li>
    </ul>
    <button class="btn" :disabled="busy || offset === 0" @click="load(offset - 100)">上一页</button>
    <button class="btn" :disabled="busy || !hasMore" @click="load(offset + 100)">下一页</button>
  </section>
</template>
<script setup>
import { onMounted, ref } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
import { literatureWorkspaceApi } from '@/apis/literature_api'
const items = ref([]),
  archives = ref([]),
  busy = ref(false),
  offset = ref(0),
  hasMore = ref(false),
  notice = ref('')
const mappingRequests = new Map()
const exportBusiness = async () => {
  if (busy.value) return
  busy.value = true
  try {
    const response = await literatureWorkspaceApi.exportBusinessBackup()
    const url = URL.createObjectURL(await response.blob())
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'literature_business_backup.zip'
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    notice.value = '当前业务快照已导出；归档内包含范围、排除项和摘要清单。'
  } catch (error) {
    notice.value = error.message || '备份失败；未交付不完整文件'
  } finally {
    busy.value = false
  }
}
const retryMapping = async (item) => {
  if (busy.value) return
  if (!window.confirm('重新验证并尝试映射这个历史原件？原回执不变，已有内容不覆盖。')) return
  busy.value = true
  const requestId = mappingRequests.get(item.id) || crypto.randomUUID()
  mappingRequests.set(item.id, requestId)
  try {
    const result = await apiPost(`/api/literature/history/${item.id}/retry-mapping`, {
      expected_digest: item.content_digest,
      request_id: requestId
    })
    mappingRequests.delete(item.id)
    notice.value = `新回执 #${result.attempt_id} · ${result.status}：${result.detail}`
  } catch (error) {
    notice.value = error.message || '映射重试失败；再次点击会沿用同一请求身份'
  } finally {
    busy.value = false
  }
}
const load = async (page) => {
  if (busy.value) return
  busy.value = true
  try {
    const [data, sealed] = await Promise.all([
      apiGet(`/api/literature/history?offset=${page}`),
      literatureWorkspaceApi.historyArchives()
    ])
    items.value = data.items || []
    archives.value = sealed.items || []
    offset.value = page
    hasMore.value = data.has_more
    notice.value = ''
  } catch (error) {
    notice.value = error.message || '读取迁移历史失败'
  } finally {
    busy.value = false
  }
}
const download = async (item) => {
  if (busy.value) return
  busy.value = true
  try {
    const response = await apiGet(`/api/literature/history/${item.id}/download`, {}, true, 'blob')
    const url = URL.createObjectURL(await response.blob())
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = item.source_key.split('/').pop() || `history-${item.id}.bin`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    notice.value = error.message || '下载失败'
  } finally {
    busy.value = false
  }
}
const exportArchive = async (archive) => {
  if (busy.value) return
  busy.value = true
  try {
    const response = await literatureWorkspaceApi.exportHistoryArchive(archive.archive_sha256)
    const url = URL.createObjectURL(await response.blob())
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `paperagent-history-${archive.archive_sha256.slice(0, 12)}.zip`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    notice.value = error.message || '归档校验或导出失败，未提供不完整文件'
  } finally {
    busy.value = false
  }
}
onMounted(() => load(0))
</script>
