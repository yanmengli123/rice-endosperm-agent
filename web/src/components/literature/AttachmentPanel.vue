<template>
  <div class="attachment-panel">
    <div class="upload-row">
      <select v-model="kind" class="kind-select">
        <option value="si">支撑信息 (SI)</option>
        <option value="review">审稿意见</option>
        <option value="data">数据</option>
        <option value="note">笔记</option>
      </select>
      <input ref="fileInput" type="file" :disabled="uploading" @change="onFileChange" />
      <button class="btn" :disabled="!file || uploading" @click="doUpload">
        {{ uploading ? '上传中…' : '上传（0 API 抽文本）' }}
      </button>
    </div>

    <ul v-if="attachments.length" class="attachment-list">
      <li v-for="item in attachments" :key="item.uri" class="attachment-item">
        <span class="att-kind">{{ item.kind }}</span>
        <span class="att-name">{{ item.filename }}</span>
        <button v-if="item.uri" class="btn" :disabled="!!downloading" @click="downloadAttachment(item)">
          {{ downloading === item.uri ? '下载中…' : '原件' }}
        </button>
        <button
          v-if="item.text_uri"
          class="btn"
          :disabled="!!downloading"
          @click="downloadAttachment(item, true)"
        >
          {{ downloading === item.text_uri ? '下载中…' : '抽取文本' }}
        </button>
      </li>
    </ul>
    <div v-else class="attachment-empty">暂无依附资料</div>
    <p v-if="notice" role="status">{{ notice }}</p>
  </div>
</template>

<script>
import { ref, watch, onBeforeUnmount } from 'vue'
import { listLiteratureAttachments, uploadLiteratureAttachment, fetchLiteratureAssetBlob } from '@/apis/literature_api'
import { useUserStore } from '@/stores/user'

export default {
  name: 'AttachmentPanel',
  props: {
    rid: { type: String, required: true }
  },
  setup(props) {
    const attachments = ref([])
    const kind = ref('si')
    const file = ref(null)
    const uploading = ref(false)
    const downloading = ref('')
    const notice = ref('')
    const fileInput = ref(null)
    const userStore = useUserStore()
    let generation = 0,
      listGeneration = 0,
      disposed = false,
      loadController = null,
      downloadController = null
    const current = (ticket, rid) => !disposed && generation === ticket && props.rid === rid

    const load = async () => {
      const ticket = generation,
        rid = props.rid,
        request = ++listGeneration
      loadController?.abort()
      const controller = new AbortController()
      loadController = controller
      try {
        const data = await listLiteratureAttachments(rid, controller.signal)
        if (current(ticket, rid) && request === listGeneration && !controller.signal.aborted)
          attachments.value = data?.attachments || []
      } catch (error) {
        if (current(ticket, rid) && request === listGeneration && !controller.signal.aborted)
          notice.value = error?.message || '附件清单载入失败'
      } finally {
        if (loadController === controller) loadController = null
      }
    }
    const downloadAttachment = async (item, text = false) => {
      const uri = text ? item.text_uri : item.uri
      if (downloading.value || !uri || !attachments.value.some((entry) => entry.uri === item.uri)) return
      const ticket = generation,
        rid = props.rid,
        controller = new AbortController()
      downloadController = controller
      downloading.value = uri
      notice.value = ''
      try {
        const blob = await fetchLiteratureAssetBlob(uri, controller.signal)
        if (!current(ticket, rid) || controller.signal.aborted) return
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        try {
          link.href = url
          link.download = `${item.filename || '文献附件'}${text ? '.txt' : ''}`
          document.body.appendChild(link)
          link.click()
        } finally {
          link.remove()
          URL.revokeObjectURL(url)
        }
      } catch (error) {
        if (current(ticket, rid) && !controller.signal.aborted)
          notice.value = error?.message || '附件下载失败'
      } finally {
        if (downloadController === controller) downloadController = null
        if (current(ticket, rid)) downloading.value = ''
      }
    }
    const onFileChange = (event) => {
      file.value = event?.target?.files?.[0] || null
    }
    const doUpload = async () => {
      if (!file.value || uploading.value) return
      const ticket = generation,
        rid = props.rid,
        selectedFile = file.value,
        selectedKind = kind.value
      uploading.value = true
      notice.value = ''
      try {
        await uploadLiteratureAttachment(rid, selectedFile, selectedKind)
        if (!current(ticket, rid)) return
        file.value = null
        if (fileInput.value) fileInput.value.value = ''
        notice.value = '附件已上传'
        await load()
      } catch (error) {
        if (current(ticket, rid)) notice.value = error?.message || '附件上传失败'
      } finally {
        if (current(ticket, rid)) uploading.value = false
      }
    }

    const invalidate = () => {
      generation++
      listGeneration++
      loadController?.abort()
      downloadController?.abort()
      attachments.value = []
      file.value = null
      uploading.value = false
      downloading.value = ''
      notice.value = ''
      if (fileInput.value) fileInput.value.value = ''
    }
    watch(
      () => [props.rid, userStore.uid, userStore.departmentId, userStore.userRole, userStore.isLoggedIn],
      () => { invalidate(); load() },
      { immediate: true, flush: 'sync' }
    )
    onBeforeUnmount(() => { disposed = true; invalidate() })

    return { attachments, kind, file, fileInput, uploading, downloading, notice, onFileChange, doUpload, downloadAttachment }
  }
}
</script>

<style lang="less" scoped>
.upload-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}
.kind-select {
  padding: 4px 8px;
}
.attachment-list {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.attachment-item {
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 13px;
}
.att-kind {
  font-size: 12px;
  border-radius: 4px;
  padding: 1px 6px;
  background: color-mix(in srgb, var(--main-color) 12%, transparent);
}
.attachment-empty {
  color: var(--color-text-tertiary);
  padding: 16px 0;
}
</style>
