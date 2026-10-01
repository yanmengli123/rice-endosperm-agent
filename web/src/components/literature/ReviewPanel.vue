<template>
  <div class="review-panel">
    <div class="review-head">
      <span v-if="meta.total">待复核 {{ meta.blocking_pending }} / {{ meta.total }}</span>
      <button class="btn" :disabled="dualLoading" @click="triggerDual">
        {{ dualLoading ? '双通道渲染入队中…' : '触发双通道渲染' }}
      </button>
    </div>
    <div v-if="notice" class="review-notice">{{ notice }}</div>

    <div v-if="!meta.total" class="review-empty">
      暂无复核清单（离线渲染不产生双通道差异；需先触发双通道渲染）
    </div>

    <ul v-else class="review-list">
      <li
        v-for="item in meta.items"
        :key="item.report_idx"
        class="review-item"
        :class="{ resolved: item.decision, blocking: item.blocking && !item.decision }"
      >
        <div class="item-head">
          <span class="item-idx">#{{ item.report_idx }}</span>
          <span v-if="item.para_id" class="item-para">{{ item.para_id }}</span>
          <span v-if="item.decision" class="item-decided">
            已裁决：{{ item.decision.choice }}
          </span>
        </div>
        <div class="item-bodies">
          <div v-if="item.a_text" class="body-mineru"><label>MinerU (A)</label>{{ item.a_text }}</div>
          <div v-if="item.b_text" class="body-paddle"><label>Paddle (B)</label>{{ item.b_text }}</div>
        </div>
        <div class="item-choices">
          <button
            v-for="choice in choices"
            :key="choice.key"
            class="choice-btn"
            @click="decide(item, choice.key)"
          >
            {{ choice.label }}
          </button>
        </div>
      </li>
    </ul>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '@/apis/base'

export default {
  name: 'ReviewPanel',
  props: {
    rid: { type: String, required: true }
  },
  setup(props) {
    const meta = ref({ total: 0, blocking_pending: 0, items: [] })
    const notice = ref('')
    const dualLoading = ref(false)
    const choices = [
      { key: 'A', label: 'A 保留 MinerU' },
      { key: 'B', label: 'B 采用 Paddle' },
      { key: 'C', label: 'C 两者合并' },
      { key: 'D', label: 'D 均不取' }
    ]

    const enc = (v) => encodeURIComponent(v)
    const load = async () => {
      try {
        meta.value = await apiGet(`/api/literature/papers/${enc(props.rid)}/review`)
      } catch (err) {
        meta.value = { total: 0, blocking_pending: 0, items: [] }
      }
    }
    const triggerDual = async () => {
      dualLoading.value = true
      notice.value = ''
      try {
        await apiPost(`/api/literature/papers/${enc(props.rid)}/review/dual-channel`)
        notice.value = '双通道渲染已入队（202），完成后重新打开本面板查看差异'
      } catch (err) {
        notice.value = `触发失败：${err?.message || err}`
      } finally {
        dualLoading.value = false
      }
    }
    const decide = async (item, choice) => {
      const resolvedText = choice === 'A' ? null : item.b_text || item.suggestion || ''
      try {
        await apiPost(`/api/literature/papers/${enc(props.rid)}/review/decide`, {
          report_idx: item.report_idx,
          para_id: item.para_id || '',
          choice,
          resolved_text: resolvedText
        })
        await load()
      } catch (err) {
        notice.value = `裁决失败：${err?.message || err}`
      }
    }

    onMounted(load)

    return { meta, notice, dualLoading, choices, triggerDual, decide }
  }
}
</script>

<style lang="less" scoped>
.review-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.review-notice {
  padding: 8px 12px;
  border-radius: 6px;
  background: rgba(22, 119, 255, 0.08);
  margin-bottom: 10px;
  font-size: 13px;
}
.review-empty {
  opacity: 0.6;
  padding: 16px 0;
}
.review-list {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.review-item {
  border: 1px solid var(--border-color, #eee);
  border-radius: 8px;
  padding: 10px 12px;
  &.blocking {
    border-color: #e6a23c;
  }
  &.resolved {
    opacity: 0.65;
  }
}
.item-head {
  display: flex;
  gap: 10px;
  font-size: 12px;
  margin-bottom: 6px;
}
.item-decided {
  color: var(--primary-color, #1677ff);
}
.item-bodies {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  label {
    display: inline-block;
    font-weight: 600;
    margin-right: 6px;
    opacity: 0.7;
  }
}
.item-choices {
  margin-top: 8px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.choice-btn {
  border: 1px solid var(--border-color, #ddd);
  border-radius: 6px;
  background: transparent;
  padding: 3px 10px;
  cursor: pointer;
  &:hover {
    border-color: var(--primary-color, #1677ff);
  }
}
</style>
