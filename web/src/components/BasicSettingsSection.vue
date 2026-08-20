<template>
  <div class="basic-settings-section">
    <template v-if="userStore.isAdmin">
      <div class="section-title">默认项配置</div>
      <div class="settings-panel">
        <template v-if="userStore.isSuperAdmin">
          <div class="setting-row two-cols">
            <div class="col-item">
              <div class="setting-label">{{ items?.default_model?.des || '默认对话模型' }}</div>
              <div class="setting-content">
                <ModelSelectorComponent
                  @select-model="handleChatModelSelect"
                  :model_spec="configStore.config?.default_model"
                  placeholder="请选择默认模型"
                />
              </div>
            </div>
            <div class="col-item">
              <div class="setting-label">{{ items?.fast_model?.des }}</div>
              <div class="setting-content">
                <ModelSelectorComponent
                  @select-model="handleFastModelSelect"
                  :model_spec="configStore.config?.fast_model"
                  placeholder="请选择模型"
                />
              </div>
            </div>
          </div>
          <div class="setting-row two-cols">
            <div class="col-item">
              <div class="setting-label">{{ items?.embed_model?.des }}</div>
              <div class="setting-content">
                <EmbeddingModelSelector
                  :value="configStore.config?.embed_model"
                  @change="handleChange('embed_model', $event)"
                  style="width: 100%"
                />
              </div>
            </div>
            <div class="col-item">
              <div class="setting-label">{{ items?.reranker?.des }}</div>
              <div class="setting-content">
                <RerankModelSelector
                  :value="configStore.config?.reranker"
                  @change="handleChange('reranker', $event)"
                  style="width: 100%"
                />
              </div>
            </div>
          </div>
          <div class="setting-row two-cols">
            <div class="col-item">
              <div class="setting-label">
                {{ items?.default_ocr_engine?.des || '默认 OCR 解析引擎' }}
              </div>
              <div class="setting-content">
                <a-select
                  :value="configStore.config?.default_ocr_engine || 'rapid_ocr'"
                  @change="handleChange('default_ocr_engine', $event)"
                  class="full-width"
                >
                  <a-select-option
                    v-for="option in ocrEngineOptions"
                    :key="option.value"
                    :value="option.value"
                  >
                    {{ option.label }}
                  </a-select-option>
                </a-select>
              </div>
            </div>
          </div>
          <div class="mineru-config-card">
            <div class="mineru-card-header">
              <div>
                <div class="mineru-card-title">
                  <ShieldCheck :size="18" />
                  MinerU 官方 API（免费额度）
                  <a-tag :color="mineruConfig.token_configured ? 'green' : 'orange'">
                    {{ mineruConfig.token_configured ? 'Token 已配置' : 'Token 未配置' }}
                  </a-tag>
                  <a-tag v-if="mineruConfig.is_default" color="blue">当前默认</a-tag>
                </div>
                <p class="mineru-card-description">
                  全局配置一次，知识库上传和附件解析都会使用同一 Token。Token 仅保存在服务端，页面不会回显。
                </p>
              </div>
              <a
                href="https://mineru.net/apiManage/token"
                target="_blank"
                rel="noopener noreferrer"
                class="official-link"
              >
                官网创建 Token
                <ExternalLink :size="14" />
              </a>
            </div>

            <div class="mineru-form-grid">
              <div class="mineru-field mineru-token-field">
                <label>API Token</label>
                <a-input-password
                  v-model:value="mineruForm.api_token"
                  :placeholder="
                    mineruConfig.token_configured
                      ? '已安全配置；留空保持不变'
                      : '粘贴 MinerU 官网创建的 Token'
                  "
                  autocomplete="new-password"
                />
              </div>
              <div class="mineru-field">
                <label>解析模型</label>
                <a-select v-model:value="mineruForm.model_version" class="full-width">
                  <a-select-option value="vlm">VLM（推荐）</a-select-option>
                  <a-select-option value="pipeline">Pipeline</a-select-option>
                </a-select>
              </div>
            </div>

            <div class="mineru-card-footer">
              <div class="connection-status" :class="mineruConnection.status">
                {{ mineruConnection.message || '保存前可先测试 Token；测试不会创建解析任务。' }}
              </div>
              <div class="mineru-actions">
                <a-button :loading="mineruTesting" @click="testMineruConnection">测试连接</a-button>
                <a-button
                  type="primary"
                  :loading="mineruSaving"
                  @click="saveMineruAsDefault"
                >
                  保存并设为默认
                </a-button>
              </div>
            </div>
          </div>
        </template>
      </div>

      <template v-if="userStore.isSuperAdmin">
        <div class="section-title">内容审查配置</div>
        <div class="section">
          <div class="card">
            <span class="label">{{ items?.enable_content_guard?.des }}</span>
            <a-switch
              :checked="configStore.config?.enable_content_guard"
              @change="handleChange('enable_content_guard', $event)"
            />
          </div>
          <div class="card" v-if="configStore.config?.enable_content_guard">
            <span class="label">{{ items?.enable_content_guard_llm?.des }}</span>
            <a-switch
              :checked="configStore.config?.enable_content_guard_llm"
              @change="handleChange('enable_content_guard_llm', $event)"
            />
          </div>
          <div
            class="card card-select"
            v-if="
              configStore.config?.enable_content_guard &&
              configStore.config?.enable_content_guard_llm
            "
          >
            <span class="label">{{ items?.content_guard_llm_model?.des }}</span>
            <ModelSelectorComponent
              @select-model="handleContentGuardModelSelect"
              :model_spec="configStore.config?.content_guard_llm_model"
              placeholder="请选择模型"
            />
          </div>
        </div>
      </template>
    </template>

    <!-- 服务链接部分 -->
    <div v-if="userStore.isAdmin" class="section-title">服务链接</div>
    <div v-if="userStore.isAdmin">
      <p class="section-description">
        快速访问系统相关的外部服务，需要将 localhost 替换为实际的 IP 地址。
      </p>
      <div class="services-grid">
        <div class="service-link-card">
          <div class="service-info">
            <h4>Neo4j 浏览器</h4>
            <p>图数据库管理界面</p>
          </div>
          <a-button
            type="default"
            class="lucide-icon-btn"
            @click="openLink('http://localhost:7474/')"
            :icon="h(Globe, { size: 18 })"
          >
            访问
          </a-button>
        </div>

        <div class="service-link-card">
          <div class="service-info">
            <h4>API 接口文档</h4>
            <p>系统接口文档和调试工具</p>
          </div>
          <a-button
            type="default"
            class="lucide-icon-btn"
            @click="openLink('http://localhost:5050/docs')"
            :icon="h(Globe, { size: 18 })"
          >
            访问
          </a-button>
        </div>

        <div class="service-link-card">
          <div class="service-info">
            <h4>MinIO 对象存储</h4>
            <p>文件存储管理控制台</p>
          </div>
          <a-button
            type="default"
            class="lucide-icon-btn"
            @click="openLink('http://localhost:9001')"
            :icon="h(Globe, { size: 18 })"
          >
            访问
          </a-button>
        </div>

        <div class="service-link-card">
          <div class="service-info">
            <h4>Milvus WebUI</h4>
            <p>向量数据库管理界面</p>
          </div>
          <a-button
            type="default"
            class="lucide-icon-btn"
            @click="openLink('http://localhost:9091/webui/')"
            :icon="h(Globe, { size: 18 })"
          >
            访问
          </a-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, h, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useConfigStore } from '@/stores/config'
import { useUserStore } from '@/stores/user'
import { ExternalLink, Globe, ShieldCheck } from '@lucide/vue'
import { ocrApi } from '@/apis/system_api'
import ModelSelectorComponent from '@/components/ModelSelectorComponent.vue'
import EmbeddingModelSelector from '@/components/EmbeddingModelSelector.vue'
import RerankModelSelector from '@/components/RerankModelSelector.vue'

const configStore = useConfigStore()
const userStore = useUserStore()
const items = computed(() => configStore.config?._config_items || {})
const mineruConfig = ref({ token_configured: false, is_default: false })
const mineruForm = reactive({ api_token: '', model_version: 'vlm' })
const mineruConnection = reactive({ status: '', message: '' })
const mineruTesting = ref(false)
const mineruSaving = ref(false)
const ocrEngineOptions = [
  { value: 'disable', label: '不启用' },
  { value: 'rapid_ocr', label: 'RapidOCR (ONNX)' },
  { value: 'mineru_ocr', label: 'MinerU 本地服务' },
  { value: 'mineru_official', label: 'MinerU 官方 API（免费额度）' },
  { value: 'pp_structure_v3_ocr', label: 'PP-Structure-V3' },
  { value: 'deepseek_ocr', label: 'DeepSeek OCR' },
  { value: 'paddleocr_vl_1_6', label: 'PaddleOCR-VL-1.6' },
  { value: 'paddleocr_pp_ocrv6', label: 'PP-OCRv6' }
]

const loadMineruConfig = async () => {
  if (!userStore.isSuperAdmin) return
  try {
    const response = await ocrApi.getMineruOfficialConfig()
    mineruConfig.value = response.data
    mineruForm.model_version = response.data?.settings?.model_version || 'vlm'
  } catch (error) {
    mineruConnection.status = 'error'
    mineruConnection.message = error.message || '读取 MinerU 配置失败'
  }
}

const testMineruConnection = async () => {
  mineruTesting.value = true
  try {
    const response = await ocrApi.testMineruOfficialConfig(mineruForm.api_token)
    mineruConnection.status = response.data?.status || 'healthy'
    mineruConnection.message = response.data?.message || 'MinerU 官方 API 连接正常'
    message.success('MinerU Token 测试通过')
  } catch (error) {
    mineruConnection.status = 'unhealthy'
    mineruConnection.message = error.message || 'MinerU Token 测试失败'
    message.error(mineruConnection.message)
  } finally {
    mineruTesting.value = false
  }
}

const saveMineruAsDefault = async () => {
  mineruSaving.value = true
  try {
    const response = await ocrApi.saveMineruOfficialConfig({
      api_token: mineruForm.api_token || null,
      model_version: mineruForm.model_version,
      set_as_default: true
    })
    mineruForm.api_token = ''
    mineruConfig.value = response.data
    mineruConnection.status = response.connection?.status || 'healthy'
    mineruConnection.message = response.connection?.message || 'MinerU 官方 API 连接正常'
    await configStore.refreshConfig()
    message.success('MinerU 已保存并设为默认 OCR 解析引擎')
  } catch (error) {
    mineruConnection.status = 'unhealthy'
    mineruConnection.message = error.message || '保存 MinerU 配置失败'
    message.error(mineruConnection.message)
  } finally {
    mineruSaving.value = false
  }
}

onMounted(loadMineruConfig)

const handleChange = (key, e) => {
  configStore.setConfigValue(key, e)
}

const handleChatModelSelect = (spec) => {
  if (typeof spec === 'string' && spec) {
    configStore.setConfigValue('default_model', spec)
  }
}

const handleFastModelSelect = (spec) => {
  if (typeof spec === 'string' && spec) {
    configStore.setConfigValue('fast_model', spec)
  }
}

const handleContentGuardModelSelect = (spec) => {
  if (typeof spec === 'string' && spec) {
    configStore.setConfigValue('content_guard_llm_model', spec)
  }
}

const openLink = (url) => {
  window.open(url, '_blank')
}
</script>

<style lang="less" scoped>
.basic-settings-section {
  .section {
    background-color: var(--gray-0);
    padding: 10px 16px;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    border: 1px solid var(--gray-150);
  }

  .settings-panel {
    background-color: var(--gray-50);
    border: 1px solid var(--gray-200);
    border-radius: 8px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .setting-row {
    display: flex;
    flex-direction: column;
    gap: 8px;

    &.two-cols {
      flex-direction: row;
      gap: 20px;
    }

    .col-item {
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 0;
    }
  }

  .setting-label {
    font-size: 13px;
    font-weight: 500;
    color: var(--gray-700);
  }

  .setting-content {
    width: 100%;

    .full-width {
      width: 100%;
    }
  }

  .mineru-config-card {
    padding: 16px;
    border: 1px solid var(--gray-200);
    border-radius: 8px;
    background: var(--gray-0);
  }

  .mineru-card-header,
  .mineru-card-footer,
  .mineru-card-title,
  .mineru-actions,
  .official-link {
    display: flex;
    align-items: center;
  }

  .mineru-card-header,
  .mineru-card-footer {
    justify-content: space-between;
    gap: 16px;
  }

  .mineru-card-title {
    gap: 8px;
    color: var(--gray-900);
    font-size: 15px;
    font-weight: 600;
  }

  .mineru-card-description {
    margin: 6px 0 0;
    color: var(--gray-600);
    font-size: 13px;
  }

  .official-link {
    flex-shrink: 0;
    gap: 5px;
    color: var(--color-primary-700);
    font-size: 13px;
  }

  .mineru-form-grid {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(180px, 1fr);
    gap: 16px;
    margin: 16px 0;
  }

  .mineru-field {
    display: flex;
    flex-direction: column;
    gap: 6px;

    label {
      color: var(--gray-700);
      font-size: 13px;
      font-weight: 500;
    }
  }

  .mineru-actions {
    gap: 8px;
  }

  .connection-status {
    color: var(--gray-600);
    font-size: 13px;

    &.healthy {
      color: var(--color-success-700);
    }

    &.unhealthy,
    &.error {
      color: var(--color-error-700);
    }
  }

  .card {
    display: flex;
    align-items: center;
    justify-content: space-between;

    .label {
      margin-right: 20px;
      font-weight: 500;
      color: var(--gray-800);
      flex-shrink: 0;
      min-width: 140px;
    }

    &.card-select {
      align-items: flex-start;
      gap: 12px;

      .label {
        margin-right: 0;
        margin-top: 6px;
      }
    }
  }

  .agent-select {
    width: 320px;
    max-width: 100%;
  }

  .services-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
    margin-top: 16px;
  }

  .service-link-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    background: var(--gray-0);
    transition: all 0.2s;
    min-height: 70px;

    &:hover {
      box-shadow: 0 1px 8px var(--gray-150);
      border-color: var(--gray-100);
    }

    .service-info {
      flex: 1;
      margin-right: 16px;

      h4 {
        margin: 0 0 4px 0;
        color: var(--gray-900);
        font-size: 15px;
        font-weight: 500;
      }

      p {
        margin: 0;
        color: var(--gray-600);
        font-size: 13px;
        line-height: 1.4;
      }
    }
  }

  @media (max-width: 768px) {
    .agent-select {
      width: 100%;
    }

    .mineru-card-header,
    .mineru-card-footer {
      align-items: flex-start;
      flex-direction: column;
    }

    .mineru-form-grid {
      grid-template-columns: 1fr;
    }
  }
}
</style>
