<template>
  <div class="cards-panel">
    <div class="card-form">
      <select v-model="cardType" class="kind-select">
        <option value="card-qa">问答卡</option>
        <option value="card-summary">总结卡</option>
        <option value="card-note">笔记卡</option>
        <option value="card-translate">翻译卡</option>
      </select>
      <input v-model="title" class="title-input" placeholder="卡片标题（可空）" />
    </div>
    <textarea v-model="content" class="content-input" rows="3" placeholder="卡片内容（问答回写、心得…）"></textarea>
    <button class="btn" :disabled="!content.trim() || saving" @click="save">
      {{ saving ? '保存中…' : '写卡片' }}
    </button>

    <ul v-if="cards.length" class="card-list">
      <li v-for="card in cards" :key="card.slug" class="card-item">
        <div class="card-head">
          <span class="card-type">{{ card.card_type }}</span>
          <strong>{{ card.title }}</strong>
        </div>
        <div class="card-content">{{ card.content }}</div>
      </li>
    </ul>
    <div v-else class="cards-empty">暂无卡片</div>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '@/apis/base'

export default {
  name: 'CardsPanel',
  props: {
    rid: { type: String, required: true }
  },
  setup(props) {
    const cards = ref([])
    const cardType = ref('card-qa')
    const title = ref('')
    const content = ref('')
    const saving = ref(false)

    const enc = (v) => encodeURIComponent(v)
    const load = async () => {
      const data = await apiGet(`/api/literature/papers/${enc(props.rid)}/cards`)
      cards.value = data?.cards || []
    }
    const save = async () => {
      saving.value = true
      try {
        await apiPost(`/api/literature/papers/${enc(props.rid)}/cards`, {
          card_type: cardType.value,
          content: content.value,
          title: title.value
        })
        content.value = ''
        title.value = ''
        await load()
      } finally {
        saving.value = false
      }
    }

    onMounted(load)

    return { cards, cardType, title, content, saving, save }
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
  border: 1px solid var(--border-color, #eee);
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
  background: rgba(22, 119, 255, 0.1);
}
.card-content {
  font-size: 13px;
  white-space: pre-wrap;
}
.cards-empty {
  opacity: 0.6;
  padding: 12px 0;
}
</style>
