<template>
  <div class="diary-view">
    <div class="diary-head">
      <h2>阅读日记</h2>
      <button class="btn" :disabled="saving" @click="changeMonth(adjacentMonth(month, -1))">
        上月
      </button>
      <input
        :value="month"
        type="month"
        class="month-input"
        aria-label="日记月份"
        :disabled="saving"
        @change="changeMonth($event.target.value)"
      />
      <button class="btn" :disabled="saving" @click="changeMonth(adjacentMonth(month, 1))">
        下月
      </button>
      <button class="btn" :disabled="saving" @click="selectDay(today)">今天</button>
      <input
        :value="selectedDate"
        type="date"
        aria-label="日记日期"
        @change="selectDay($event.target.value)"
      />
      <button class="btn" :disabled="exporting" @click="exportMonth">导出当月 Markdown</button>
    </div>
    <div class="diary-days" aria-label="日记月历">
      <span
        v-for="weekday in ['一', '二', '三', '四', '五', '六', '日']"
        :key="weekday"
        class="weekday"
        >{{ weekday }}</span
      >
      <button
        v-for="(day, index) in calendar"
        :key="index"
        class="day-chip"
        :disabled="!day || saving"
        :aria-label="day ? `${day}${days.includes(day) ? ' 已有日记' : ''}` : '空白日期'"
        :aria-pressed="!!day && day === selectedDate"
        :class="{
          active: day === selectedDate,
          recorded: days.includes(day),
          today: day === today
        }"
        @click="selectDay(day)"
      >
        {{ day ? Number(day.slice(-2)) : ''
        }}<span v-if="days.includes(day)" aria-hidden="true"> ·</span>
      </button>
    </div>
    <p class="calendar-hint">带圆点的日期已有日记；点击任意日期可新建。</p>
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
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiPut } from '@/apis/base'
import { literatureWorkspaceApi } from '@/apis/literature_api'
import { monthDays, adjacentMonth } from '@/utils/literature_calendar'
import { confirmAction } from '@/composables/useConfirmAction'

export default {
  name: 'DiaryView',
  emits: ['editing-change'],
  setup(_props, { emit }) {
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
    const calendar = computed(() => monthDays(month.value))
    watch([content, savedContent, saving], () =>
      emit('editing-change', saving.value || content.value !== savedContent.value)
    )
    let generation = 0
    let monthGeneration = 0

    const load = async () => {
      const ticket = ++monthGeneration
      const qs = month.value ? `?month=${encodeURIComponent(month.value)}` : ''
      try {
        const data = await apiGet(`/api/literature/diary/days${qs}`)
        if (ticket === monthGeneration) days.value = data?.days || []
      } catch (error) {
        if (ticket === monthGeneration) notice.value = error.message || '日记月历读取失败'
      }
    }
    const changeMonth = async (value) => {
      if (value === month.value || !monthDays(value).length) return
      await selectDay(value === today.slice(0, 7) ? today : `${value}-01`)
    }
    const selectDay = async (day) => {
      if (
        !/^\d{4}-\d{2}-\d{2}$/.test(day) ||
        !monthDays(day.slice(0, 7)).includes(day) ||
        saving.value
      )
        return
      if (content.value !== savedContent.value) {
        if (!(await confirmAction('切换日期会丢弃未保存的心得，继续？'))) return
      }
      if (saving.value || day === selectedDate.value) return
      const ticket = ++generation
      const previousDate = selectedDate.value
      loading.value = true
      notice.value = ''
      try {
        const data = await apiGet(`/api/literature/diary/${encodeURIComponent(day)}`)
        if (ticket !== generation) return
        selectedDate.value = day
        content.value = savedContent.value = data?.content || ''
        if (month.value !== day.slice(0, 7)) {
          month.value = day.slice(0, 7)
          days.value = []
          await load()
        }
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
    onBeforeUnmount(() => {
      generation++
      monthGeneration++
      emit('editing-change', false)
    })

    return {
      days,
      today,
      calendar,
      changeMonth,
      adjacentMonth,
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
  flex-wrap: wrap;
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
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 6px;
  margin: 12px 0;
}
.weekday {
  text-align: center;
  color: var(--color-text-secondary);
}
.calendar-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.day-chip {
  border: 1px solid var(--gray-300);
  border-radius: 10px;
  background: transparent;
  color: var(--color-text-secondary);
  padding: 2px 10px;
  cursor: pointer;
  min-height: 36px;
  &.today {
    border-color: var(--main-color);
  }
  &.recorded {
    font-weight: 600;
  }
  &:disabled {
    cursor: default;
  }
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
