<template>
  <div class="diary-view">
    <div class="diary-head">
      <h2>阅读日记</h2>
      <input v-model="month" class="month-input" placeholder="YYYY-MM（可空）" @keyup.enter="load" />
    </div>
    <div class="diary-days">
      <button
        v-for="day in days"
        :key="day"
        class="day-chip"
        :class="{ active: day === selectedDate }"
        @click="selectDay(day)"
      >
        {{ day }}
      </button>
    </div>
    <textarea v-model="content" class="diary-content" rows="10" placeholder="写下当天的阅读心得（清空并保存即删除该天日记）"></textarea>
    <button class="btn" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存日记' }}</button>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { apiGet, apiPut } from '@/apis/base'

export default {
  name: 'DiaryView',
  setup() {
    const days = ref([])
    const month = ref('')
    const selectedDate = ref('')
    const content = ref('')
    const saving = ref(false)

    const load = async () => {
      const qs = month.value ? `?month=${encodeURIComponent(month.value)}` : ''
      const data = await apiGet(`/api/literature/diary/days${qs}`)
      days.value = data?.days || []
    }
    const selectDay = async (day) => {
      selectedDate.value = day
      const data = await apiGet(`/api/literature/diary/${encodeURIComponent(day)}`)
      content.value = data?.content || ''
    }
    const save = async () => {
      if (!selectedDate.value) {
        // 空日记可直接创建：默认落到今天（上游语义：用户主动写下即建行）
        selectedDate.value = new Date().toISOString().slice(0, 10)
      }
      saving.value = true
      try {
        await apiPut(`/api/literature/diary/${encodeURIComponent(selectedDate.value)}`, {
          content: content.value
        })
        await load()
      } finally {
        saving.value = false
      }
    }

    onMounted(load)

    return { days, month, selectedDate, content, saving, load, selectDay, save }
  }
}
</script>

<style lang="less" scoped>
.diary-view {
  padding: 16px 24px;
  max-width: 760px;
}
.diary-head {
  display: flex;
  gap: 16px;
  align-items: center;
}
.month-input {
  padding: 4px 10px;
  width: 140px;
}
.diary-days {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.day-chip {
  border: 1px solid var(--border-color, #ddd);
  border-radius: 10px;
  background: transparent;
  padding: 2px 10px;
  cursor: pointer;
  &.active {
    background: var(--primary-color, #1677ff);
    color: #fff;
  }
}
.diary-content {
  width: 100%;
  box-sizing: border-box;
  margin-bottom: 10px;
  padding: 10px 12px;
}
</style>
