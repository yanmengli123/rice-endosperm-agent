<template>
  <div class="cards-panel">
    <div class="card-form">
      <select v-model="cardType" class="kind-select" :disabled="saving">
        <option value="card-qa">问答卡</option>
        <option value="card-summary">总结卡</option>
        <option value="card-note">笔记卡</option>
        <option value="card-translate">翻译卡</option>
      </select>
      <input
        v-model="title"
        class="title-input"
        placeholder="卡片标题（可空）"
        :disabled="saving"
      />
    </div>
    <textarea
      v-model="content"
      :disabled="saving"
      class="content-input"
      rows="3"
      placeholder="卡片内容（问答回写、心得…）"
    ></textarea>
    <button class="btn" :disabled="!content.trim() || saving" @click="save">
      {{ saving ? '保存中…' : editing ? '确认覆盖卡片' : '写卡片' }}
    </button>
    <button v-if="editing" class="btn" :disabled="saving" @click="cancelEdit">取消编辑</button>
    <p v-if="error" role="alert">{{ error }}</p>

    <ul v-if="cards.length" class="card-list">
      <li v-for="card in cards" :key="card.slug" class="card-item">
        <div class="card-head">
          <span class="card-type">{{ card.card_type }}</span>
          <strong>{{ card.title }}</strong>
          <button class="btn" :disabled="saving" @click="edit(card)">编辑</button>
        </div>
        <div class="card-content">{{ card.content }}</div>
      </li>
    </ul>
    <div v-else class="cards-empty">暂无卡片</div>
  </div>
</template>

<script>
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
import { confirmAction } from '@/composables/useLiteratureSessions'
import { useUserStore } from '@/stores/user'

export default {
  name: 'CardsPanel',
  props: {
    rid: { type: String, default: '' }
  },
  emits: ['saved', 'editing-change'],
  setup(props, { emit }) {
    const cards = ref([])
    const cardType = ref('card-qa')
    const title = ref('')
    const content = ref('')
    const saving = ref(false)
    const editing = ref(null)
    const error = ref('')
    const user = useUserStore()
    const owner = computed(() =>
      JSON.stringify([props.rid, user.uid, user.departmentId, user.isLoggedIn])
    )
    let disposed = false
    let loadGeneration = 0
    let editConfirmation = null
    watch([title, content, editing, saving], () =>
      emit('editing-change', saving.value || !!title.value || !!content.value || !!editing.value)
    )
    const edit = async (card) => {
      if (disposed || saving.value || editConfirmation) return
      const key = owner.value
      const token = {}
      editConfirmation = token
      try {
        if (title.value || content.value) {
          if (!(await confirmAction('切换卡片会放弃未保存编辑，继续？'))) return
        }
        if (disposed || owner.value !== key || saving.value) return
        editing.value = card
        cardType.value = card.card_type
        title.value = card.title
        content.value = card.content
      } finally {
        if (editConfirmation === token) editConfirmation = null
      }
    }
    const cancelEdit = () => {
      editing.value = null
      title.value = ''
      content.value = ''
    }

    const enc = (v) => encodeURIComponent(v)
    const endpoint = (rid = props.rid) =>
      rid ? `/api/literature/papers/${enc(rid)}/cards` : '/api/literature/cards'
    const load = async () => {
      const key = owner.value
      const ticket = ++loadGeneration
      try {
        const data = await apiGet(endpoint())
        if (!disposed && key === owner.value && ticket === loadGeneration)
          cards.value = data?.cards || []
      } catch (err) {
        if (!disposed && key === owner.value && ticket === loadGeneration)
          error.value = err?.message || '卡片载入失败'
      }
    }
    const save = async () => {
      if (disposed || saving.value || editConfirmation || !content.value.trim()) return
      const key = owner.value
      const rid = props.rid
      const card = editing.value
      const payload = {
        card_type: cardType.value,
        content: content.value,
        title: title.value,
        ...(card
          ? {
              slug: card.slug,
              overwrite: true,
              expected_digest: card.digest,
              para_ids: card.para_ids,
              tags: card.tags
            }
          : {})
      }
      saving.value = true
      error.value = ''
      try {
        if (
          card &&
          !(await confirmAction('确认覆盖这张卡片？期间若有其他修改，服务端会拒绝保存。'))
        )
          return
        if (
          disposed ||
          owner.value !== key ||
          editing.value !== card ||
          content.value !== payload.content ||
          title.value !== payload.title ||
          cardType.value !== payload.card_type
        )
          return
        await apiPost(endpoint(rid), payload)
        if (disposed || owner.value !== key) return
        content.value = ''
        title.value = ''
        editing.value = null
        emit('saved')
        await load()
      } catch (err) {
        if (!disposed && owner.value === key) error.value = err?.message || '卡片保存失败'
      } finally {
        if (!disposed && owner.value === key) saving.value = false
      }
    }

    watch(
      owner,
      () => {
        loadGeneration++
        cards.value = []
        saving.value = false
        editConfirmation = null
        error.value = ''
        cancelEdit()
        if (user.isLoggedIn !== false) load()
      },
      { flush: 'sync' }
    )
    onMounted(load)
    onBeforeUnmount(() => {
      disposed = true
      loadGeneration++
      emit('editing-change', false)
    })

    return { cards, cardType, title, content, saving, save, editing, error, edit, cancelEdit }
  }
}
</script>

<style lang="less" scoped>
.card-form {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.title-input {
  flex: 1;
  padding: 4px 10px;
}
.content-input {
  width: 100%;
  box-sizing: border-box;
  margin-bottom: 8px;
  padding: 6px 10px;
}
.card-list {
  list-style: none;
  padding: 0;
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.card-item {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 8px 12px;
}
.card-head {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 4px;
}
.card-type {
  font-size: 11px;
  border-radius: 4px;
  padding: 1px 6px;
  background: color-mix(in srgb, var(--main-color) 12%, transparent);
}
.card-content {
  font-size: 13px;
  white-space: pre-wrap;
}
.cards-empty {
  color: var(--color-text-tertiary);
  padding: 12px 0;
}
</style>
