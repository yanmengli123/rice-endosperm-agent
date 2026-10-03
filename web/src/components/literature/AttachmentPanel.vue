<template>
  <div class="attachment-panel">
    <div class="upload-row">
      <select v-model="kind" class="kind-select">
        <option value="si">支撑信息 (SI)</option>
        <option value="review">审稿意见</option>
        <option value="data">数据</option>
        <option value="note">笔记</option>
      </select>
      <input type="file" @change="onFileChange" />
      <button class="btn" :disabled="!file || uploading" @click="doUpload">
        {{ uploading ? '上传中…' : '上传（0 API 抽文本）' }}
      </button>
    </div>

    <ul v-if="attachments.length" class="attachment-list">
      <li v-for="item in attachments" :key="item.uri" class="attachment-item">
        <span class="att-kind">{{ item.kind }}</span>
        <span class="att-name">{{ item.filename }}</span>
        <a v-if="item.uri" :href="assetUrl(item.uri)" target="_blank" rel="noopener">原件</a>
        <a v-if="item.text_uri" :href="assetUrl(item.text_uri)" target="_blank" rel="noopener">抽取文本</a>
      </li>
    </ul>
    <div v-else class="attachment-empty">暂无依附资料</div>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { listLiteratureAttachments, uploadLiteratureAttachment, buildLiteratureAssetUrl } from '@/apis/literature_api'

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

    const assetUrl = (uri) => buildLiteratureAssetUrl(uri)

    const load = async () => {
      const data = await listLiteratureAttachments(props.rid)
      attachments.value = data?.attachments || []
    }
    const onFileChange = (event) => {
      file.value = event?.target?.files?.[0] || null
    }
    const doUpload = async () => {
      if (!file.value) return
      uploading.value = true
      try {
        await uploadLiteratureAttachment(props.rid, file.value, kind.value)
        file.value = null
        await load()
      } finally {
        uploading.value = false
      }
    }

    onMounted(load)

    return { attachments, kind, file, uploading, assetUrl, onFileChange, doUpload }
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
