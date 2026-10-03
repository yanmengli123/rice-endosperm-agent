<template>
  <div class="diary-view">
    <div class="diary-head">
      <h2>阅读日记</h2>
      <input v-model="month" type="month" class="month-input" @change="load" />
      <input
        :value="selectedDate"
        type="date"
        aria-label="日记日期"
        @change="selectDay($event.target.value)"
      />
      <button class="btn" :disabled="exporting" @click="exportMonth">导出当月 Markdown</button>
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
    <textarea
      :disabled="loading || saving"
      v-model="content"
      class="diary-content"
      rows="10"
      placeholder="写下当天的阅读心得（清空并保存即删除该天日记）"
    ></textarea>
    <button class="btn" :disabled="saving || loading" @click="save">
      {{ saving ? '保存中…' : '保存日记' }}
    </button>
    <p v-if="notice" role="status">{{ notice }}</p>
  </div>
</template>

<script>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiPut } from '@/apis/base'
import { literatureWorkspaceApi } from '@/apis/literature_api'

export default {
  name: 'DiaryView',
  setup() {
    const days = ref([])
    const now = new Date()
    const today = [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, '0'),
      String(now.getDate()).padStart(2, '0')
    ].join('-')
    const month = ref(today.slice(0, 7))
    const selectedDate = ref(today)
    const content = ref('')
    const saving = ref(false)
    const loading = ref(false)
    const exporting = ref(false)
    const notice = ref('')
    const savedContent = ref('')
    let generation = 0

    const load = async () => {
      const qs = month.value ? `?month=${encodeURIComponent(month.value)}` : ''
      const data = await apiGet(`/api/literature/diary/days${qs}`)
      days.value = data?.days || []
    }
    const selectDay = async (day) => {
      if (!day || saving.value) return
      if (
        content.value !== savedContent.value &&
        !window.confirm('切换日期会丢弃未保存的心得，继续？')
      )
        return
      const ticket = ++generation
      const previousDate = selectedDate.value
      selectedDate.value = day
      loading.value = true
      notice.value = ''
      try {
        const data = await apiGet(`/api/literature/diary/${encodeURIComponent(day)}`)
        if (ticket !== generation) return
        content.value = savedContent.value = data?.content || ''
      } catch (error) {
        if (ticket === generation) {
          selectedDate.value = previousDate
          notice.value = error.message || '读取日记失败'
        }
      } finally {
        if (ticket === generation) loading.value = false
      }
    }
    const exportMonth = async () => {
      exporting.value = true
      try {
        const response = await literatureWorkspaceApi.exportDiary(month.value)
        const url = URL.createObjectURL(await response.blob())
        const link = document.createElement('a')
        link.href = url
        link.download = `阅读日记-${month.value}.md`
        link.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
      } catch (error) {
        notice.value = error.message || '导出失败'
      } finally {
        exporting.value = false
      }
    }
    const save = async () => {
      if (!selectedDate.value) {
        // 空日记可直接创建：默认落到今天（上游语义：用户主动写下即建行）
        const now = new Date()
        selectedDate.value = [
          now.getFullYear(),
          String(now.getMonth() + 1).padStart(2, '0'),
          String(now.getDate()).padStart(2, '0')
        ].join('-')
      }
      saving.value = true
      const day = selectedDate.value
      const text = content.value
      try {
        await apiPut(`/api/literature/diary/${encodeURIComponent(day)}`, {
          content: text
        })
        savedContent.value = text
        notice.value = '已保存 ✓'
        await load()
      } catch (error) {
        notice.value = error.message || '日记保存失败'
      } finally {
        saving.value = false
      }
    }

    onMounted(async () => {
      await load()
      await selectDay(today)
    })
    onBeforeUnmount(() => generation++)

    return {
      days,
      month,
      selectedDate,
      content,
      saving,
      loading,
      load,
      selectDay,
      save,
      notice,
      exporting,
      exportMonth
    }
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
  background: var(--gray-100);
  color: var(--color-text);
  border: 1px solid var(--gray-300);
  border-radius: 4px;
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
  border: 1px solid var(--gray-300);
  border-radius: 10px;
  background: transparent;
  color: var(--color-text-secondary);
  padding: 2px 10px;
  cursor: pointer;
  &:hover {
    border-color: var(--main-color);
  }
  &.active {
    background: var(--main-color);
    border-color: var(--main-color);
    color: var(--gray-0);
    font-weight: 500;
  }
}
.diary-content {
  background: var(--gray-100);
  color: var(--color-text);
  border: 1px solid var(--gray-300);
  border-radius: 6px;
  width: 100%;
  box-sizing: border-box;
  margin-bottom: 10px;
  padding: 10px 12px;
}
</style>
