<template>
  <section class="lit-settings-panel" aria-label="文献工作台设置">
    <h4>解析与导入（PDF 进入知识源时生效）</h4>
    <div class="settings-grid">
      <label
        >解析语言
        <select v-model="settings.parseLanguage">
          <option value="ch">中文（ch）</option>
          <option value="en">英文（en）</option>
        </select>
      </label>
      <label
        >OCR
        <select v-model="settings.parseIsOcr">
          <option :value="true">开（扫描件友好）</option>
          <option :value="false">关（原生文本层）</option>
        </select>
      </label>
      <label
        >表格识别
        <select v-model="settings.parseEnableTable">
          <option :value="true">开</option>
          <option :value="false">关</option>
        </select>
      </label>
      <label
        >公式识别
        <select v-model="settings.parseEnableFormula">
          <option :value="true">开</option>
          <option :value="false">关</option>
        </select>
      </label>
      <label
        >MinerU 模型
        <select v-model="settings.parseModelVersion">
          <option value="vlm">vlm（默认）</option>
          <option value="pipeline">pipeline</option>
        </select>
      </label>
      <label
        >篇间间隔（秒，0-60）
        <input v-model.number="settings.parseInterval" type="number" min="0" max="60" step="1" />
      </label>
    </div>
    <p class="settings-hint">
      细配参数随导入请求生效并参与解析指纹：同一 PDF 改参数会生成新的解析版本，不会静默复用旧解析。
    </p>

    <h4>处理流程</h4>
    <div class="settings-grid">
      <label
        >复核门控（翻译时机）
        <select v-model="settings.reviewPolicy">
          <option value="wait">wait：复核裁决后再翻译</option>
          <option value="auto">auto：渲染后自动跳过复核直接翻译</option>
        </select>
      </label>
    </div>
    <p class="settings-hint">
      auto 对标 PaperAgent「跳过审核翻译」；识别复核面板仍可独立使用，事后裁决照常回放。
    </p>

    <LiteraturePdfOperations />
    <h4>界面与护眼</h4>
    <div class="settings-grid">
      <label
        >主题
        <select v-model="settings.theme">
          <option v-for="item in LITERATURE_THEMES" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
      </label>
      <label
        >阅读字号（{{ settings.fontSize }}px）
        <input v-model.number="settings.fontSize" type="range" min="14" max="24" step="1" />
      </label>
      <label
        >阅读行宽（{{ settings.lineWidth }}px）
        <input v-model.number="settings.lineWidth" type="range" min="600" max="1200" step="20" />
      </label>
    </div>
    <label class="css-block"
      >自定义 CSS（作用于文献工作台，本机保存）
      <textarea
        v-model="settings.customCss"
        rows="5"
        spellcheck="false"
        placeholder=".reading-workspace .paper-detail { letter-spacing: 0.02em; }"
      ></textarea>
    </label>

    <div class="settings-actions">
      <button class="btn" type="button" @click="resetLiteratureSettings()">恢复默认</button>
      <span class="settings-hint">设置保存在本机浏览器，按用户分键存储。</span>
    </div>
  </section>
</template>

<script setup>
import LiteraturePdfOperations from '@/components/literature/LiteraturePdfOperations.vue'
import {
  LITERATURE_THEMES,
  resetLiteratureSettings,
  useLiteratureSettings
} from '@/composables/useLiteratureSettings'

const settings = useLiteratureSettings()
</script>

<style scoped>
.lit-settings-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  text-align: left;
  max-width: 760px;
}
.lit-settings-panel h4 {
  margin: 10px 0 4px;
  font-size: 14px;
}
.settings-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
}
.settings-grid label,
.css-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.settings-hint {
  color: var(--lit-muted, #7a7a6d);
  font-size: 12px;
}
.css-block textarea {
  width: 100%;
  font-family: monospace;
  font-size: 12px;
}
.settings-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 6px;
}
</style>
