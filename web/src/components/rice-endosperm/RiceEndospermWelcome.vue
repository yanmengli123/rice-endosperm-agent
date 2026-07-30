<template>
  <section class="rice-welcome" aria-labelledby="rice-welcome-title">
    <img
      class="rice-welcome__avatar"
      src="/brand/rice-endosperm/avatar.svg"
      alt="穗芯智能体"
    />

    <div class="rice-welcome__heading">
      <p>Rice Endosperm Intelligence</p>
      <h1 id="rice-welcome-title">你好，我是穗芯</h1>
      <span>面向水稻胚乳发育与品质研究的科研智能体</span>
    </div>

    <div class="rice-welcome__capabilities" aria-label="当前可用能力">
      <span><BookOpenCheck :size="14" />知识库问答</span>
      <span><Quote :size="14" />来源引用</span>
      <span><FileSearch :size="14" />文件分析</span>
    </div>

    <div class="rice-welcome__questions">
      <button
        v-for="item in questions"
        :key="item.category"
        type="button"
        @click="emit('select', item.question)"
      >
        <small>{{ item.category }}</small>
        <strong>{{ item.question }}</strong>
      </button>
    </div>

    <p class="rice-welcome__notice">
      科研结论可能受到品种、组织、发育时期与实验条件影响，请核验原始来源。
    </p>
  </section>
</template>

<script setup>
import { BookOpenCheck, FileSearch, Quote } from 'lucide-vue-next'

const emit = defineEmits(['select'])

const questions = [
  {
    category: '基因功能',
    question: 'OsbZIP58 在水稻胚乳中的功能及直接证据是什么？'
  },
  {
    category: '品质性状',
    question: '总结影响水稻胚乳垩白形成的关键基因和调控通路。'
  },
  {
    category: '发育过程',
    question: '梳理水稻胚乳灌浆过程中淀粉合成相关基因的表达变化。'
  },
  {
    category: '实验设计',
    question: '如何验证一个转录因子是否直接调控淀粉合成基因？'
  }
]
</script>

<style lang="less" scoped>
.rice-welcome {
  width: min(760px, 100%);
  margin: 0 auto 20px;
  color: var(--rice-text);
  text-align: center;
}

.rice-welcome__avatar {
  width: 72px;
  height: 72px;
  object-fit: contain;
  border-radius: 20px;
}

.rice-welcome__heading {
  margin-top: 12px;

  p {
    margin: 0 0 3px;
    color: var(--main-700);
    font-size: 11px;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    color: var(--rice-text);
    font-size: clamp(24px, 3.5vw, 32px);
    font-weight: 680;
    line-height: 1.35;
  }

  span {
    display: block;
    margin-top: 5px;
    color: var(--rice-text-secondary);
    font-size: 13px;
  }
}

.rice-welcome__capabilities {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 7px;
  margin: 14px 0 16px;

  span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 8px;
    color: var(--main-700);
    font-size: 11px;
    font-weight: 500;
    background: var(--main-50);
    border: 1px solid var(--main-100);
    border-radius: 999px;
  }
}

.rice-welcome__questions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px;

  button {
    min-width: 0;
    min-height: 78px;
    padding: 12px 14px;
    color: inherit;
    text-align: left;
    cursor: pointer;
    background: var(--rice-surface);
    border: 1px solid var(--rice-border);
    border-radius: 10px;
    transition:
      background-color 0.18s ease,
      border-color 0.18s ease;

    &:hover {
      background: var(--main-10);
      border-color: var(--main-200);
    }

    &:focus-visible {
      outline: 3px solid color-mix(in srgb, var(--main-color) 28%, transparent);
      outline-offset: 2px;
    }
  }

  small,
  strong {
    display: block;
  }

  small {
    margin-bottom: 4px;
    color: var(--main-700);
    font-size: 11px;
    font-weight: 650;
  }

  strong {
    color: var(--rice-text);
    font-size: 12px;
    font-weight: 500;
    line-height: 1.55;
  }
}

.rice-welcome__notice {
  margin: 12px 0 0;
  color: var(--rice-text-secondary);
  font-size: 11px;
  line-height: 1.55;
}

@media (max-height: 720px) {
  .rice-welcome__avatar {
    width: 56px;
    height: 56px;
    border-radius: 16px;
  }

  .rice-welcome__heading {
    margin-top: 7px;
  }

  .rice-welcome__capabilities {
    margin: 9px 0 11px;
  }

  .rice-welcome__questions button {
    min-height: 66px;
    padding: 9px 11px;
  }

  .rice-welcome__notice {
    display: none;
  }
}

@media (max-width: 520px) {
  .rice-welcome__heading span {
    font-size: 12px;
  }

  .rice-welcome__capabilities {
    gap: 5px;

    span {
      padding: 3px 6px;
    }
  }

  .rice-welcome__questions {
    gap: 7px;

    button {
      min-height: 86px;
      padding: 9px;
    }

    strong {
      font-size: 11px;
    }
  }
}
</style>
