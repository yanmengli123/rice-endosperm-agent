<template>
  <div class="review-panel">
    <div class="review-head">
      <span v-if="meta.total">待复核 {{ meta.blocking_pending }} / {{ meta.total }}</span>
      <button class="btn" :disabled="dualLoading" @click="triggerDual">
        {{ dualLoading ? '双通道渲染入队中…' : '触发双通道渲染' }}
      </button>
      <button class="btn" :disabled="applying" @click="applyDecisions">
        {{ applying ? '回放中…' : '应用裁决（修正回放）' }}
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
          <span v-for="pid in item.para_ids" :key="pid" class="item-para">{{ pid }}</span>
          <span v-if="item.decision" class="item-decided">
            已裁决：{{ item.decision.choice }}
          </span>
        </div>
        <div class="item-bodies">
          <div v-if="item.mineru_text" class="body-mineru">
            <label>MinerU (A)</label>{{ item.mineru_text }}
          </div>
          <div v-if="item.paddleocr_text" class="body-paddle">
            <label>Paddle (B)</label>{{ item.paddleocr_text }}
          </div>
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
          <button class="choice-btn suggest" @click="suggest(item)">✨ AI 建议</button>
        </div>
        <div v-if="item.suggestion" class="item-suggestion">
          <label>AI 建议（仅预填，仍需人工确认）：</label>
          <textarea v-model="item.suggestion" rows="2" class="suggest-input"></textarea>
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
    const applying = ref(false)
    const choices = [
      { key: 'A', label: 'A 保留/还原 MinerU' },
      { key: 'B', label: 'B 采用 PaddleOCR' },
      { key: 'C', label: 'C 两者皆可' },
      { key: 'D', label: 'D 都有误（存疑）' }
    ]

    const enc = (v) => encodeURIComponent(v)
    const load = async () => {
      try {
        meta.value = await apiGet(`/api/literature/papers/${enc(props.rid)}/review`)
      } catch (err) {
        meta.value = { total: 0, blocking_pending: 0, items: [] }
        notice.value = `复核结果读取失败：${err?.message || err}`
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
    const suggest = async (item) => {
      try {
        const data = await apiPost(`/api/literature/papers/${enc(props.rid)}/review/suggest`, {
          report_idx: item.report_idx
        })
        item.suggestion = data?.suggestion || ''
      } catch (err) {
        notice.value = `AI 建议失败：${err?.message || err}`
      }
    }
    const applyDecisions = async () => {
      applying.value = true
      notice.value = ''
      try {
        const manifest = await apiPost(`/api/literature/papers/${enc(props.rid)}/review/apply`)
        notice.value = `修正回放完成：${manifest.applied_decisions} 条裁决（替换 ${manifest.actions.replaced} / 还原 ${manifest.actions.restored} / 标记 ${manifest.actions.marked} / 保留 ${manifest.actions.kept}）`
        await load()
      } catch (err) {
        notice.value = `回放失败：${err?.message || err}`
      } finally {
        applying.value = false
      }
    }
    const decide = async (item, choice) => {
      const legacyAck = item.stale_decision?.contract_version === 'legacy_v1'
      if (
        legacyAck &&
        !window.confirm(
          '此项有旧契约裁决。确认按当前 A/B/C/D 语义重新裁决？历史文本不会被当作删除/合并指令。'
        )
      )
        return
      // 兼容语义（review_item_v2）：仅 B 携带替换文本（AI 建议优先，缺省 Paddle 文本）；A/C/D 不改写
      const resolvedText = choice === 'B' ? item.suggestion || item.paddleocr_text || '' : null
      try {
        await apiPost(`/api/literature/papers/${enc(props.rid)}/review/decide`, {
          report_idx: item.report_idx,
          item_id: item.item_id,
          render_id: item.render_id,
          render_digest: item.render_digest,
          legacy_ack: legacyAck,
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

    return {
      meta,
      notice,
      dualLoading,
      applying,
      choices,
      triggerDual,
      decide,
      suggest,
      applyDecisions
    }
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
  background: color-mix(in srgb, var(--main-color) 10%, transparent);
  margin-bottom: 10px;
  font-size: 13px;
}
.review-empty {
  color: var(--color-text-tertiary);
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
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 10px 12px;
  &.blocking {
    border-color: color-mix(in srgb, #e6a23c 60%, transparent);
  }
  &.resolved {
    color: var(--color-text-secondary);
  }
}
.item-head {
  display: flex;
  gap: 10px;
  font-size: 12px;
  margin-bottom: 6px;
}
.item-decided {
  color: var(--main-color);
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
    color: var(--color-text-secondary);
  }
}
.item-choices {
  margin-top: 8px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.choice-btn {
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: transparent;
  padding: 3px 10px;
  cursor: pointer;
  &:hover {
    border-color: var(--main-color);
  }
}
</style>
