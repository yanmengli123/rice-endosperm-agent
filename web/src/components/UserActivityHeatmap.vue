<template>
  <section class="activity-card" aria-label="近一年消息活动">
    <div class="activity-heading"><h3>近一年消息活动</h3><a-button size="small" :loading="loading" @click="load">刷新</a-button></div>
    <p>按上海时间统计本人的用户、助手和工具消息，已删除会话不计入。</p>
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <a-spin :spinning="loading">
      <div class="calendar-scroll">
        <div class="calendar" role="grid" aria-label="消息活动日历">
          <div v-for="(week, i) in weeks" :key="i" class="week" role="row">
            <span v-for="(day, j) in week" :key="day?.date || j" role="gridcell" class="day"
              :class="day ? `level-${level(day.message_count)}` : 'empty'"
              :title="day ? `${day.date}：${day.message_count} 条消息` : ''"
              :aria-label="day ? `${day.date}，${day.message_count} 条消息` : undefined" />
          </div>
        </div>
      </div>
      <p>{{ total }} 条消息 · {{ activeDays }} 个活跃日 <span class="legend">少 <i v-for="i in 5" :key="i" :class="`level-${i - 1}`" /> 多</span></p>
    </a-spin>
  </section>
</template>
<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { userApi } from '@/apis/user_api'
import { activityWeeks } from '@/utils/activityHeatmap'
const user = useUserStore()
const days = ref([]), loading = ref(false), error = ref('')
let request
const weeks = computed(() => activityWeeks(days.value))
const total = computed(() => days.value.reduce((sum, day) => sum + day.message_count, 0))
const activeDays = computed(() => days.value.filter((day) => day.message_count > 0).length)
const max = computed(() => Math.max(1, ...days.value.map((day) => day.message_count)))
const level = (value) => value > 0 ? Math.min(4, Math.ceil(value / max.value * 4)) : 0
async function load() {
  request?.abort()
  const current = new AbortController()
  request = current
  loading.value = true
  error.value = ''
  try {
    const result = await userApi.messageActivity({ signal: current.signal })
    if (request === current) days.value = result.daily_message_counts
  } catch (e) {
    if (!current.signal.aborted && request === current) error.value = e.message || '活动统计加载失败'
  } finally {
    if (request === current) loading.value = false
  }
}
watch(() => user.uid, () => { days.value = []; if (user.uid) load(); else request?.abort() }, { immediate: true })
onBeforeUnmount(() => request?.abort())
</script>
<style scoped lang="less">
.activity-card { padding: 20px; border: 1px solid var(--gray-150); border-radius: 12px; margin-top: 20px;
  p { color: var(--color-text-secondary); font-size: 12px; }
}
.activity-heading { display: flex; align-items: center; justify-content: space-between; h3 { margin: 0; } }
.calendar-scroll { overflow-x: auto; padding: 12px 0; }
.calendar { display: flex; gap: 3px; width: max-content; }
.week { display: grid; grid-template-rows: repeat(7, 11px); gap: 3px; }
.day, .legend i { display: inline-block; width: 11px; height: 11px; border-radius: 2px; background: var(--gray-100); }
.empty { visibility: hidden; }
.level-1 { background: color-mix(in srgb, var(--main-color) 25%, var(--gray-0)) !important; }
.level-2 { background: color-mix(in srgb, var(--main-color) 50%, var(--gray-0)) !important; }
.level-3 { background: color-mix(in srgb, var(--main-color) 75%, var(--gray-0)) !important; }
.level-4 { background: var(--main-color) !important; }
.legend { display: inline-flex; align-items: center; gap: 3px; float: right; }
</style>
