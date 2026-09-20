<template>
  <div class="database-container layout-container">
    <PageHeader
      v-if="!props.embedded"
      title="知识库"
      :active-key="knowledgeActiveView"
      :tabs="knowledgeViewItems"
      :loading="dbState.listLoading"
      :show-border="true"
      aria-label="知识库视图切换"
    />

    <PageShoulder v-model:search="searchQuery" search-placeholder="搜索知识库...">
      <template #filters>
        <a-select
          v-model:value="typeFilter"
          style="width: 120px"
          placeholder="全部类型"
          allow-clear
        >
          <a-select-option :value="null">全部类型</a-select-option>
          <a-select-option v-for="t in filterKbTypes" :key="t" :value="t">
            {{ getKbTypeLabel(t) }}
          </a-select-option>
        </a-select>
        <a-select v-model:value="scopeFilter" style="width: 140px" placeholder="默认问答范围">
          <a-select-option value="all">全部范围状态</a-select-option>
          <a-select-option value="included">已纳入问答</a-select-option>
          <a-select-option value="excluded">未纳入问答</a-select-option>
        </a-select>
      </template>
      <template #actions>
        <a-button class="lucide-icon-btn" @click="openWikiCreateModal">
          <BookOpenCheck :size="16" /> 新建动态 Wiki
        </a-button>
        <a-button
          type="primary"
          class="lucide-icon-btn"
          :disabled="!kbTypes.length"
          @click="state.openNewDatabaseModel = true"
        >
          <Plus :size="16" /> 新建知识库
        </a-button>
      </template>
    </PageShoulder>

    <a-modal
      :open="state.openNewDatabaseModel"
      title="新建知识库"
      :confirm-loading="dbState.creating"
      @ok="handleCreateDatabase"
      @cancel="cancelCreateDatabase"
      class="new-database-modal"
      width="800px"
      destroyOnClose
    >
      <div class="new-database-form">
        <a-steps :current="state.wizardStep - 1" size="small" class="wizard-steps">
          <a-step title="选择知识源" />
          <a-step title="业务信息" />
          <a-step title="数据处理策略" />
          <a-step title="确认创建" />
        </a-steps>

        <!-- Step 1：选择知识源（必选，不允许空模板） -->
        <div v-if="state.wizardStep === 1" class="form-section">
          <h3 class="section-title">知识源契约<span class="required-mark">*</span></h3>
          <a-alert
            class="derived-product-hint"
            type="info"
            show-icon
            message="知识源契约决定知识库接受什么数据、如何处理以及权威边界在哪里；创建后不可更改。动态 LLM-Wiki 是派生知识产品，使用独立入口创建。"
          />
          <div class="format-template-cards">
            <div
              v-for="parent in contractParents"
              :key="parent.key"
              class="format-template-card"
              :class="{ active: isContractParentActive(parent.key) }"
            >
              <button
                type="button"
                class="template-card-select"
                :aria-pressed="isContractParentActive(parent.key)"
                @click="selectContractParent(parent.key)"
              >
                <span class="card-header">
                  <span class="type-title">{{ parent.label }}</span>
                </span>
                <span class="card-description">{{ contractParentDescription(parent) }}</span>
                <span class="template-select-control">
                  <CheckCircle2 v-if="isContractParentActive(parent.key)" :size="16" />
                  {{ isContractParentActive(parent.key) ? '已选择' : '选择此类型' }}
                </span>
              </button>
              <div
                v-if="isContractParentActive(parent.key) && parent.key === 'csv'"
                class="template-sub-option"
              >
                <span class="sub-option-label">CSV 数据用途</span>
                <a-radio-group
                  :value="state.selectedContractKey"
                  size="small"
                  @change="handleCsvContractChange"
                >
                  <a-radio-button value="csv_record">结构化记录</a-radio-button>
                  <a-radio-button value="csv_qa">标准问答</a-radio-button>
                </a-radio-group>
              </div>
            </div>
          </div>
          <a-collapse
            v-if="genericDocumentParent || advancedKbTypes.length"
            class="advanced-kb-collapse"
          >
            <a-collapse-panel key="advanced" header="高级：通用文档与外部知识源">
              <div v-if="genericDocumentParent" class="advanced-source-section">
                <h4 class="advanced-source-title">本地通用知识库</h4>
                <div
                  class="format-template-card generic-document-card"
                  :class="{ active: isContractParentActive(genericDocumentParent.key) }"
                >
                  <button
                    type="button"
                    class="template-card-select"
                    :aria-pressed="isContractParentActive(genericDocumentParent.key)"
                    @click="selectContractParent(genericDocumentParent.key)"
                  >
                    <span class="card-header">
                      <span class="type-title">{{ genericDocumentParent.label }}</span>
                    </span>
                    <span class="card-description">
                      {{ contractParentDescription(genericDocumentParent) }}
                    </span>
                    <span class="template-select-control">
                      <CheckCircle2
                        v-if="isContractParentActive(genericDocumentParent.key)"
                        :size="16"
                      />
                      {{
                        isContractParentActive(genericDocumentParent.key)
                          ? '已选择'
                          : '选择通用文档知识库'
                      }}
                    </span>
                  </button>
                </div>
              </div>
              <div v-if="advancedKbTypes.length" class="advanced-source-section">
                <h4 class="advanced-source-title">外部知识源连接器</h4>
                <div class="kb-type-cards">
                  <div
                    v-for="typeKey in advancedKbTypes"
                    :key="typeKey"
                    class="kb-type-card"
                    :class="{
                      active: !state.selectedContractKey && newDatabase.kb_type === typeKey
                    }"
                    @click="selectAdvancedKbType(typeKey)"
                  >
                    <div class="card-header">
                      <component :is="getKbTypeIcon(typeKey)" class="type-icon" />
                      <span class="type-title">{{ getKbTypeLabel(typeKey) }}</span>
                    </div>
                    <div class="card-description">
                      {{ getKbTypeDescription(supportedKbTypes[typeKey]) }}
                    </div>
                  </div>
                </div>
              </div>
            </a-collapse-panel>
          </a-collapse>
        </div>

        <!-- Step 2：业务信息 -->
        <div v-else-if="state.wizardStep === 2">
          <div class="form-section">
            <h3 class="section-title">知识库名称<span class="required-mark">*</span></h3>
            <a-input v-model:value="newDatabase.name" :placeholder="nameSuggestion" />
            <p class="field-hint">建议格式：研究领域｜知识库定位（如「{{ nameSuggestion }}」）</p>
          </div>

          <div class="form-grid two-columns">
            <div class="form-section compact-section">
              <h3 class="section-title">内容领域</h3>
              <a-input v-model:value="newDatabase.content_domain" placeholder="如：水稻胚乳发育" />
            </div>
            <div v-if="requiresEmbeddingModel" class="form-section compact-section">
              <h3 class="section-title">嵌入模型</h3>
              <EmbeddingModelSelector
                v-model:value="newDatabase.embedding_model_spec"
                class="full-width"
                placeholder="请选择嵌入模型"
              />
            </div>
          </div>

          <div class="form-section">
            <h3 class="section-title">用途说明</h3>
            <p class="field-hint description-hint">面向人阅读：说明这个知识库供谁查询什么内容。</p>
            <AiTextarea
              v-model="newDatabase.description"
              :name="newDatabase.name"
              placeholder="如：供课题组查询论文实验结论和出处"
              :auto-size="{ minRows: 2, maxRows: 6 }"
            />
          </div>

          <div class="form-section">
            <h3 class="section-title">Agent 工具说明</h3>
            <p class="field-hint description-hint">
              面向智能体：智能体根据这段说明决定何时调用该知识库。留空时后端沿用用途说明。
            </p>
            <a-textarea
              v-model:value="newDatabase.tool_description"
              placeholder="如：用于检索水稻胚乳发育相关论文的实验结论、数值与出处"
              :auto-size="{ minRows: 2, maxRows: 6 }"
            />
          </div>

          <div
            v-if="createParamOptions.length && !state.selectedContractKey"
            class="form-grid three-columns"
          >
            <div
              v-for="field in createParamOptions"
              :key="field.key"
              class="form-section compact-section"
            >
              <h3 class="section-title">
                {{ field.label || field.key
                }}<span v-if="field.required" class="required-mark">*</span>
              </h3>
              <a-input-password
                v-if="field.type === 'password'"
                v-model:value="newDatabase.additional_params[field.key]"
                :placeholder="field.placeholder"
              />
              <a-input-number
                v-else-if="field.type === 'number'"
                v-model:value="newDatabase.additional_params[field.key]"
                :min="field.min"
                :max="field.max"
                :step="field.step"
                class="full-width"
              />
              <a-switch
                v-else-if="field.type === 'boolean'"
                v-model:checked="newDatabase.additional_params[field.key]"
              />
              <a-select
                v-else-if="field.type === 'select'"
                v-model:value="newDatabase.additional_params[field.key]"
                :options="field.options || []"
                class="full-width"
              />
              <a-input
                v-else
                v-model:value="newDatabase.additional_params[field.key]"
                :placeholder="field.placeholder"
              />
              <p v-if="field.description" class="field-hint">{{ field.description }}</p>
            </div>
          </div>

          <div class="form-section compact-section">
            <h3 class="section-title">共享设置</h3>
            <p class="field-hint description-hint">
              默认私有（仅创建者可见）；团队/部门/全局共享需在此显式选择。
            </p>
            <ShareConfigForm
              ref="shareConfigFormRef"
              v-model="shareConfig"
              :auto-select-user-dept="true"
            />
          </div>
        </div>

        <!-- Step 3：数据处理、权威与检索策略（只读） -->
        <div v-else-if="state.wizardStep === 3" class="form-section">
          <h3 class="section-title">数据处理、权威与检索策略</h3>
          <a-alert
            type="info"
            show-icon
            message="以下策略由知识源契约决定并由系统托管，创建后由后端强制执行，不可在知识库级别修改。"
          />
          <div v-if="selectedContract" class="policy-panel">
            <div
              class="policy-row"
              v-for="(text, key) in selectedContract.processing_policy"
              :key="key"
            >
              <span class="policy-key">{{ policyKeyLabel(key) }}</span>
              <span class="policy-value">{{ text }}</span>
            </div>
          </div>
          <div v-else class="policy-panel">
            <p class="field-hint">
              外部知识源（{{
                getKbTypeLabel(newDatabase.kb_type)
              }}）不做本地解析与索引，数据留在远端系统。
            </p>
          </div>
        </div>

        <!-- Step 4：创建确认（Contract Preview） -->
        <div v-else class="form-section">
          <h3 class="section-title">确认创建</h3>
          <a-descriptions bordered :column="1" size="small" class="contract-preview">
            <a-descriptions-item label="知识源契约">
              <template v-if="selectedContract">
                {{
                  selectedContract.contract_ref || `${state.selectedContractKey}@${contractVersion}`
                }}
                <span v-if="selectedContract.digest" class="contract-digest"
                  >{{ selectedContract.digest.slice(0, 19) }}…</span
                >
              </template>
              <template v-else>legacy 兼容（{{ getKbTypeLabel(newDatabase.kb_type) }}）</template>
            </a-descriptions-item>
            <a-descriptions-item label="名称">{{ newDatabase.name || '—' }}</a-descriptions-item>
            <a-descriptions-item v-if="newDatabase.content_domain" label="内容领域">
              {{ newDatabase.content_domain }}
            </a-descriptions-item>
            <a-descriptions-item v-if="selectedContract?.authority_policy" label="权威来源">
              {{ authoritySummary }}
            </a-descriptions-item>
            <a-descriptions-item v-if="selectedContract?.accepted_media?.length" label="允许的数据">
              {{ acceptedMediaSummary }}
            </a-descriptions-item>
            <a-descriptions-item label="权限范围">{{ shareSummary }}</a-descriptions-item>
            <a-descriptions-item label="创建后的下一步">{{ nextStepHint }}</a-descriptions-item>
          </a-descriptions>
          <a-alert
            class="derived-product-hint"
            type="warning"
            show-icon
            message="点击创建后仅生成 DRAFT 知识库（不含数据）；随后在知识库详情页完成数据导入与校验。"
          />
        </div>
      </div>
      <template #footer>
        <a-button key="back" @click="cancelCreateDatabase">取消</a-button>
        <a-button v-if="state.wizardStep > 1" @click="state.wizardStep -= 1">上一步</a-button>
        <a-button v-if="state.wizardStep < 4" key="next" type="primary" @click="goNextStep"
          >下一步</a-button
        >
        <a-button
          v-else
          key="submit"
          type="primary"
          :loading="dbState.creating"
          @click="handleCreateDatabase"
          >创建知识库</a-button
        >
      </template>
    </a-modal>

    <a-modal
      :open="wikiCreate.open"
      title="新建动态 LLM-Wiki"
      width="760px"
      :confirm-loading="wikiCreate.saving"
      destroy-on-close
      @ok="handleCreateWiki"
      @cancel="closeWikiCreateModal"
    >
      <div class="wiki-create-form">
        <a-alert
          type="warning"
          show-icon
          message="动态 Wiki 是派生导航产品，不是新的事实来源"
          description="它会从已选权威知识源生成实体页、别名和检索路径；问答引用仍必须回到原 PDF 锚点、CSV 行或规范图谱 Evidence。"
        />
        <div class="form-grid two-columns">
          <div class="form-section compact-section">
            <h3 class="section-title">产品名称<span class="required-mark">*</span></h3>
            <a-input
              v-model:value="wikiCreate.form.name"
              placeholder="例如：水稻胚乳动态知识导航"
            />
          </div>
          <div class="form-section compact-section">
            <h3 class="section-title">更新策略</h3>
            <a-select v-model:value="wikiCreate.form.update_mode" class="full-width">
              <a-select-option value="MANUAL">手动构建（推荐）</a-select-option>
              <a-select-option value="ON_SOURCE_CHANGE">知识源变化后排队</a-select-option>
              <a-select-option value="SCHEDULED">计划更新</a-select-option>
            </a-select>
          </div>
        </div>
        <div class="form-section">
          <h3 class="section-title">绑定权威知识源<span class="required-mark">*</span></h3>
          <a-select
            v-model:value="wikiCreate.form.source_kb_ids"
            mode="multiple"
            show-search
            option-filter-prop="label"
            class="full-width"
            placeholder="选择同一权限域内的 PDF、CSV 或图谱知识库"
            :options="authorityDatabaseOptions"
          />
          <p class="field-hint">
            为防止权限并集越权，服务端会强制要求所选知识源具有完全相同的共享权限域。
          </p>
        </div>
        <div class="form-section">
          <h3 class="section-title">产品说明</h3>
          <a-textarea
            v-model:value="wikiCreate.form.description"
            :auto-size="{ minRows: 3, maxRows: 6 }"
            placeholder="说明导航范围、更新目标和适用科研问题"
          />
        </div>
        <div class="wiki-policy-row">
          <div>
            <div class="scope-section-title">构建完成后自动发布</div>
            <div class="scope-section-hint">建议首轮关闭，人工核验 Claim 与来源后再发布。</div>
          </div>
          <a-switch v-model:checked="wikiCreate.form.auto_publish" />
        </div>
      </div>
    </a-modal>

    <a-modal
      :open="scopeModal.open"
      title="默认问答范围"
      width="680px"
      :confirm-loading="scopeModal.saving"
      destroyOnClose
      @ok="saveScopeMember"
      @cancel="closeScopeModal"
    >
      <div v-if="scopeModal.database" class="scope-config">
        <div class="scope-summary">
          <div>
            <div class="scope-kb-name">{{ scopeModal.database.name }}</div>
            <div class="scope-kb-id">{{ scopeModal.database.kb_id }}</div>
          </div>
          <a-tag :color="healthTag(scopeForm.health_status).color">
            {{ healthTag(scopeForm.health_status).label }}
          </a-tag>
        </div>

        <a-alert
          type="info"
          show-icon
          message="纳入问答只改变检索策略，不会重新索引、删除文件或修改图谱。最终范围仍会与用户权限取交集。"
        />

        <div class="scope-section scope-enabled-row">
          <div>
            <div class="scope-section-title">纳入默认问答范围</div>
            <div class="scope-section-hint">启用后，继承默认范围的智能体可检索此知识库。</div>
          </div>
          <a-switch v-model:checked="scopeForm.enabled" />
        </div>

        <div class="scope-section">
          <div class="scope-section-title">检索通道</div>
          <div class="scope-option-grid">
            <label class="scope-option" :class="{ 'scope-option-locked': isWikiScopeProduct }">
              <span><FileText :size="16" /> 文档 Chunk</span>
              <a-switch
                v-model:checked="scopeForm.document_enabled"
                size="small"
                :disabled="isWikiScopeProduct"
              />
            </label>
            <label class="scope-option" :class="{ 'scope-option-locked': isWikiScopeProduct }">
              <span><Network :size="16" /> 知识图谱</span>
              <a-switch
                v-model:checked="scopeForm.graph_enabled"
                size="small"
                :disabled="isWikiScopeProduct"
              />
            </label>
            <label class="scope-option" :class="{ 'scope-option-locked': isWikiScopeProduct }">
              <span><TableProperties :size="16" /> 结构化证据</span>
              <a-switch
                v-model:checked="scopeForm.structured_enabled"
                size="small"
                :disabled="isWikiScopeProduct"
              />
            </label>
            <label class="scope-option" :class="{ 'scope-option-locked': !isWikiScopeProduct }">
              <span><Compass :size="16" /> Wiki 导航</span>
              <a-tooltip
                title="仅派生知识产品（动态 LLM-Wiki）可开启导航通道；权威知识源永远不参与 Wiki 导航。"
              >
                <a-switch
                  v-model:checked="scopeForm.wiki_navigation_enabled"
                  size="small"
                  :disabled="!isWikiScopeProduct"
                />
              </a-tooltip>
            </label>
          </div>
        </div>

        <div v-if="!isWikiScopeProduct" class="scope-section">
          <div class="scope-section-title">科研证据策略</div>
          <div class="evidence-options">
            <a-checkbox v-model:checked="scopeForm.evidence_strict">STRICT 严格证据</a-checkbox>
            <a-checkbox v-model:checked="scopeForm.evidence_supporting"
              >SUPPORTING 支持证据</a-checkbox
            >
            <a-checkbox v-model:checked="scopeForm.evidence_candidate"
              >CANDIDATE 候选证据</a-checkbox
            >
            <a-checkbox v-model:checked="scopeForm.evidence_rejected">REJECTED 否定证据</a-checkbox>
          </div>
          <a-alert
            v-if="scopeForm.evidence_candidate || scopeForm.evidence_rejected"
            type="warning"
            show-icon
            message="候选或否定证据只用于展示不确定性与冲突，不能自动升级为已证实结论。"
          />
        </div>

        <a-alert
          v-else
          type="info"
          show-icon
          message="Wiki 不提供证据等级开关；它只生成导航词，答案证据策略由绑定的权威知识源决定。"
        />

        <div class="scope-section priority-row">
          <div>
            <div class="scope-section-title">检索优先级</div>
            <div class="scope-section-hint">数值越小越优先；全局重排仍会综合相关度和证据等级。</div>
          </div>
          <a-input-number v-model:value="scopeForm.priority" :min="0" :max="1000" />
        </div>

        <div class="scope-health-grid">
          <div v-for="item in healthMetrics" :key="item.label" class="scope-health-item">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
        </div>
      </div>
    </a-modal>

    <!-- 加载状态 -->
    <div v-if="dbState.listLoading" class="loading-container">
      <a-spin size="large" />
      <p>正在加载知识库...</p>
    </div>

    <!-- 空状态显示 -->
    <ResourceEmptyState
      v-else-if="!databases || databases.length === 0"
      title="暂无知识库"
      description="创建知识库后，可以上传文件并配置检索、图谱和评估能力。"
      :icon="getKbTypeIcon('milvus')"
    >
      <template #actions>
        <a-button class="lucide-icon-btn" @click="openWikiCreateModal">
          <BookOpenCheck :size="16" /> 创建动态 Wiki
        </a-button>
        <a-button
          type="primary"
          size="large"
          class="lucide-icon-btn"
          :disabled="!kbTypes.length"
          @click="state.openNewDatabaseModel = true"
        >
          <template #icon>
            <Plus :size="16" />
          </template>
          创建知识库
        </a-button>
      </template>
    </ResourceEmptyState>

    <!-- 数据库列表 -->
    <ExtensionCardGrid v-else>
      <InfoCard
        v-for="database in filteredDatabases"
        :key="database.kb_id"
        :title="database.name"
        :subtitle="cardSubtitle(database)"
        :description="database.description || '暂无描述'"
        :tags="cardTags(database)"
        @click="navigateToDatabase(database)"
      >
        <template #icon>
          <component :is="getKbTypeIcon(database.kb_type || 'milvus')" :size="20" />
        </template>
        <template #card-more-action-corner>
          <a-menu @click="({ key }) => handleDatabaseAction(key, database)">
            <a-menu-item key="copy">
              <span class="lucide-menu-item">
                <Copy :size="15" />
                <span>复制 ID</span>
              </span>
            </a-menu-item>
            <a-menu-item key="edit">
              <span class="lucide-menu-item">
                <Pencil :size="15" />
                <span>编辑知识库</span>
              </span>
            </a-menu-item>
            <a-menu-divider />
            <a-menu-item key="delete" danger>
              <span class="lucide-menu-item">
                <Trash2 :size="15" />
                <span>删除知识库</span>
              </span>
            </a-menu-item>
          </a-menu>
        </template>
        <template #footer>
          <div class="scope-card-state">
            <span class="scope-state-dot" :class="scopeStateClass(database)"></span>
            <span>{{ scopeStateLabel(database) }}</span>
          </div>
          <a-button size="small" @click.stop="openScopeModal(database)">
            <Settings2 :size="14" /> 配置问答范围
          </a-button>
        </template>
      </InfoCard>
    </ExtensionCardGrid>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive, watch, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useConfigStore } from '@/stores/config'
import { useDatabaseStore } from '@/stores/database'
import {
  BookOpenCheck,
  CheckCircle2,
  Compass,
  Copy,
  FileText,
  Network,
  Pencil,
  Plus,
  Settings2,
  TableProperties,
  Trash2
} from '@lucide/vue'
import { message, Modal } from 'ant-design-vue'
import {
  databaseApi,
  knowledgeScopeApi,
  sourceContractApi,
  typeApi,
  wikiApi
} from '@/apis/knowledge_api'
import PageHeader from '@/components/shared/PageHeader.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import ResourceEmptyState from '@/components/shared/ResourceEmptyState.vue'
import EmbeddingModelSelector from '@/components/EmbeddingModelSelector.vue'
import ShareConfigForm from '@/components/ShareConfigForm.vue'
import ExtensionCardGrid from '@/components/extensions/ExtensionCardGrid.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import dayjs, { parseToShanghai } from '@/utils/time'
import AiTextarea from '@/components/AiTextarea.vue'
import { getKbTypeLabel, getKbTypeIcon, getKbTypeColor, kbUtils } from '@/utils/kb_utils'
import { pickLatestContract } from '@/utils/kbContract'

const route = useRoute()
const router = useRouter()
const configStore = useConfigStore()
const databaseStore = useDatabaseStore()
const props = defineProps({
  embedded: { type: Boolean, default: false }
})

// 使用 store 的状态
const { databases, state: dbState } = storeToRefs(databaseStore)

const knowledgeActiveView = 'documents'
const knowledgeViewItems = [
  { key: 'documents', label: '文档知识库', path: '/extensions?tab=knowledge' }
]

const kbTypes = computed(() => Object.keys(supportedKbTypes.value))
const filterKbTypes = computed(() =>
  Array.from(
    new Set([...kbTypes.value, ...databases.value.map((item) => item.kb_type).filter(Boolean)])
  )
)
const searchQuery = ref('')
const typeFilter = ref(null)
const scopeFilter = ref('all')
const scopeState = reactive({ scope: null, members: new Map(), loading: false })

const emptyScopeForm = () => ({
  enabled: false,
  document_enabled: true,
  graph_enabled: true,
  structured_enabled: true,
  wiki_navigation_enabled: false,
  evidence_strict: true,
  evidence_supporting: true,
  evidence_candidate: false,
  evidence_rejected: false,
  priority: 100,
  health_status: 'VALIDATING',
  health_details: {}
})

const scopeForm = reactive(emptyScopeForm())
const scopeModal = reactive({ open: false, saving: false, database: null })
const isWikiScopeProduct = computed(() => scopeModal.database?.kb_type === 'llmwiki')
const emptyWikiForm = () => ({
  name: '',
  description: '',
  source_kb_ids: [],
  update_mode: 'MANUAL',
  debounce_seconds: 300,
  auto_publish: false
})
const wikiCreate = reactive({ open: false, saving: false, form: emptyWikiForm() })
const authorityDatabaseOptions = computed(() =>
  databases.value
    .filter((item) => item.kb_type !== 'llmwiki')
    .map((item) => ({ value: item.kb_id, label: `${item.name} · ${getKbTypeLabel(item.kb_type)}` }))
)

const filteredDatabases = computed(() => {
  let list = databases.value
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase()
    list = list.filter(
      (db) =>
        db.name.toLowerCase().includes(q) ||
        (db.description && db.description.toLowerCase().includes(q))
    )
  }
  if (typeFilter.value) {
    list = list.filter((db) => (db.kb_type || 'milvus') === typeFilter.value)
  }
  if (scopeFilter.value !== 'all') {
    list = list.filter((db) => {
      const included = Boolean(scopeState.members.get(db.kb_id)?.enabled)
      return scopeFilter.value === 'included' ? included : !included
    })
  }
  return list
})

const loadDefaultScope = async () => {
  scopeState.loading = true
  try {
    const data = await knowledgeScopeApi.getDefaultScope()
    scopeState.scope = data.scope || null
    scopeState.members = new Map((data.members || []).map((item) => [item.kb_id, item]))
  } catch (error) {
    message.error(error.message || '默认问答范围加载失败')
  } finally {
    scopeState.loading = false
  }
}

const healthTag = (status) => {
  const map = {
    HEALTHY: { label: '健康', color: 'green' },
    DEGRADED: { label: '部分可用', color: 'orange' },
    UNAVAILABLE: { label: '不可用', color: 'red' },
    VALIDATING: { label: '待验证', color: 'blue' }
  }
  return map[status] || map.VALIDATING
}

const scopeStateLabel = (database) => {
  const member = scopeState.members.get(database.kb_id)
  if (!member?.enabled) return '未纳入默认问答'
  return `已纳入 · ${healthTag(member.health_status).label}`
}

const scopeStateClass = (database) => {
  const member = scopeState.members.get(database.kb_id)
  if (!member?.enabled) return 'is-off'
  if (member.health_status === 'HEALTHY') return 'is-healthy'
  if (member.health_status === 'UNAVAILABLE') return 'is-error'
  return 'is-warning'
}

const openScopeModal = (database) => {
  const member = scopeState.members.get(database.kb_id) || emptyScopeForm()
  Object.assign(scopeForm, emptyScopeForm(), member)
  if (database.kb_type === 'llmwiki') {
    Object.assign(scopeForm, {
      document_enabled: false,
      graph_enabled: false,
      structured_enabled: false,
      wiki_navigation_enabled: true
    })
  } else {
    scopeForm.wiki_navigation_enabled = false
  }
  scopeModal.database = database
  scopeModal.open = true
}

const closeScopeModal = () => {
  scopeModal.open = false
  scopeModal.database = null
  Object.assign(scopeForm, emptyScopeForm())
}

const healthMetrics = computed(() => {
  const details = scopeForm.health_details || {}
  if (isWikiScopeProduct.value) {
    return [
      { label: '发布页面', value: details.wiki_pages ?? '—' },
      { label: '验证 Claim', value: details.wiki_claims ?? '—' },
      { label: '发布版本', value: details.publication_id ? '已激活' : '—' }
    ]
  }
  return [
    { label: '文件', value: details.files ?? '—' },
    { label: 'Chunks', value: details.chunks ?? '—' },
    { label: '实体', value: details.entities ?? '—' },
    { label: '关系', value: details.triples ?? '—' },
    { label: '证据', value: details.evidence ?? '—' }
  ]
})

const saveScopeMember = async () => {
  if (!scopeModal.database || !scopeState.scope) return
  if (
    scopeForm.enabled &&
    !scopeForm.document_enabled &&
    !scopeForm.graph_enabled &&
    !scopeForm.structured_enabled &&
    !scopeForm.wiki_navigation_enabled
  ) {
    message.warning('纳入问答时至少启用一个检索通道')
    return
  }
  scopeModal.saving = true
  try {
    const payload = {
      expected_version: scopeState.scope.version,
      enabled: scopeForm.enabled,
      document_enabled: scopeForm.document_enabled,
      graph_enabled: scopeForm.graph_enabled,
      structured_enabled: scopeForm.structured_enabled,
      wiki_navigation_enabled: scopeForm.wiki_navigation_enabled,
      evidence_strict: scopeForm.evidence_strict,
      evidence_supporting: scopeForm.evidence_supporting,
      evidence_candidate: scopeForm.evidence_candidate,
      evidence_rejected: scopeForm.evidence_rejected,
      priority: scopeForm.priority
    }
    const data = await knowledgeScopeApi.updateDefaultScopeMember(
      scopeModal.database.kb_id,
      payload
    )
    scopeState.scope = data.scope
    scopeState.members.set(scopeModal.database.kb_id, {
      ...data.member,
      name: scopeModal.database.name,
      kb_type: scopeModal.database.kb_type
    })
    message.success(scopeForm.enabled ? '已纳入默认问答范围' : '已从默认问答范围停用')
    closeScopeModal()
  } catch (error) {
    if (error.response?.status === 409) {
      await loadDefaultScope()
      message.warning('范围配置已被其他管理员更新，已刷新到最新版本，请重新确认')
    } else {
      message.error(error.message || '问答范围保存失败')
    }
  } finally {
    scopeModal.saving = false
  }
}

const state = reactive({
  openNewDatabaseModel: false,
  wizardStep: 1,
  selectedContractKey: '',
  sourceContracts: [],
  contractsLoading: false
})

const openWikiCreateModal = () => {
  Object.assign(wikiCreate.form, emptyWikiForm())
  wikiCreate.open = true
}

const closeWikiCreateModal = () => {
  wikiCreate.open = false
  Object.assign(wikiCreate.form, emptyWikiForm())
}

const handleCreateWiki = async () => {
  if (!wikiCreate.form.name.trim()) {
    message.warning('请输入动态 Wiki 名称')
    return
  }
  if (!wikiCreate.form.source_kb_ids.length) {
    message.warning('请至少绑定一个权威知识源')
    return
  }
  wikiCreate.saving = true
  try {
    const data = await wikiApi.create({
      ...wikiCreate.form,
      name: wikiCreate.form.name.trim(),
      description: wikiCreate.form.description.trim()
    })
    closeWikiCreateModal()
    await databaseStore.loadDatabases()
    await loadDefaultScope()
    message.success('动态 Wiki 已创建，请执行首轮构建并核验后发布')
    if (data.wiki?.wiki_id) {
      router.push(`/extensions/wiki/${data.wiki.wiki_id}`)
    }
  } catch (error) {
    message.error(error.message || '动态 Wiki 创建失败')
  } finally {
    wikiCreate.saving = false
  }
}

// 知识源契约选型卡：三张科研主卡；通用文档在高级区显式选择。
const CONTRACT_PARENTS = [
  {
    key: 'pdf_evidence',
    label: '📄 PDF 科研文献证据库',
    fallbackDescription:
      '权威原件为对象存储中的 PDF；规范解析落在 PostgreSQL（证据锚点/题录/能力报告），Milvus 仅作检索投影。上传、解析、学术分块与混合检索全部由系统托管。',
    nameHint: '水稻胚乳发育｜文献证据库',
    nextStep: '上传 PDF → 系统解析与能力报告 → 预览发布'
  },
  {
    key: 'csv',
    label: '📊 CSV 结构化数据集',
    fallbackDescription:
      '结构化记录：一行一条记录独立成块，保留行级来源；标准问答：question/answer 两列必须显式确认映射，空问答行不进入有效集。',
    nameHint: '水稻胚乳发育｜结构化数据集',
    nextStep: '上传 CSV → Schema/列映射预检 → Canonical Commit → 建索引'
  },
  {
    key: 'managed_graph',
    label: '🕸 规范科研知识图谱',
    fallbackDescription:
      '节点 CSV + 关系 CSV；PostgreSQL 为规范事实源，Neo4j/Milvus 仅作遍历与语义投影。禁止普通文档上传与 LLM 自动抽图。',
    nameHint: '水稻胚乳发育｜科研知识图谱',
    nextStep: '导入节点 CSV 与关系 CSV → 完整性验证 → 双投影 → 发布'
  }
]

const contractParents = CONTRACT_PARENTS

const GENERIC_DOCUMENT_PARENT = {
  key: 'generic_document',
  label: '📚 通用文档知识库',
  fallbackDescription:
    '面向 Word、Markdown、文本、网页、表格、演示文稿、图片和普通 PDF 的通用检索库；保留默认解析、分块与向量检索能力，不启用科研 PDF 的强证据链语义。',
  nameHint: '水稻胚乳发育｜通用文档库',
  nextStep: '上传通用文档 → 解析与质量校验 → 建立检索索引'
}

// 按 key 取契约必须锚定 latest_version（后端快照提供；旧后端回退客户端 semver 取最大）。
// 禁止 find() 取注册顺序首条——曾把 managed_graph 新库冻结到 1.0.0，
// 造成 1.1 独有的图谱导图入口可见但被契约拒绝。
const contractByKey = (key) => pickLatestContract(state.sourceContracts, key)

const selectedContract = computed(() => contractByKey(state.selectedContractKey))

const genericDocumentParent = computed(() =>
  contractByKey(GENERIC_DOCUMENT_PARENT.key) ? GENERIC_DOCUMENT_PARENT : null
)

const contractVersion = computed(() => selectedContract.value?.version || '1.0.0')

const contractParentDescription = (parent) => {
  if (parent.key === 'csv') {
    const record = contractByKey('csv_record')
    const qa = contractByKey('csv_qa')
    if (record?.display?.card_description || qa?.display?.card_description) {
      return state.selectedContractKey === 'csv_qa'
        ? qa.display.card_description
        : record.display.card_description
    }
    return parent.fallbackDescription
  }
  return contractByKey(parent.key)?.display?.card_description || parent.fallbackDescription
}

const isContractParentActive = (parentKey) => {
  if (parentKey === 'csv') {
    return state.selectedContractKey === 'csv_record' || state.selectedContractKey === 'csv_qa'
  }
  return state.selectedContractKey === parentKey
}

const selectContractParent = (parentKey) => {
  if (isContractParentActive(parentKey)) return
  state.selectedContractKey = parentKey === 'csv' ? 'csv_record' : parentKey
  newDatabase.kb_type = 'milvus'
}

const handleCsvContractChange = (event) => {
  state.selectedContractKey = event.target.value
  newDatabase.kb_type = 'milvus'
}

const selectAdvancedKbType = (typeKey) => {
  state.selectedContractKey = ''
  newDatabase.kb_type = typeKey
  resetCreateParamValues()
}

const nameSuggestion = computed(() => {
  if (state.selectedContractKey === 'csv_record' || state.selectedContractKey === 'csv_qa') {
    return CONTRACT_PARENTS[1].nameHint
  }
  const parent = [...CONTRACT_PARENTS, GENERIC_DOCUMENT_PARENT].find(
    (item) => item.key === state.selectedContractKey
  )
  return parent?.nameHint || '新建知识库名称'
})

const requiresEmbeddingModel = computed(() =>
  Boolean(selectedKbTypeInfo.value?.requires_embedding_model)
)

const policyKeyLabel = (key) => {
  const labels = {
    chunking: '分块策略',
    parsing: '解析',
    retrieval: '检索',
    quality_gate: '质量门禁',
    ingest: '数据接入',
    identity: '记录识别',
    strict_validation: '严格校验',
    rollback: '回滚',
    navigation_products: '导航派生产品'
  }
  return labels[key] || key
}

const authoritySummary = computed(() => {
  const policy = selectedContract.value?.authority_policy || {}
  return Object.values(policy)
    .filter(Boolean)
    .map((value) => String(value).replace(/_/g, ' '))
    .join('；')
})

const acceptedMediaSummary = computed(() => {
  const media = selectedContract.value?.accepted_media || []
  return media.map((rule) => `${rule.role}：${(rule.extensions || []).join(' / ')}`).join('；')
})

const shareSummary = computed(() => {
  const labels = {
    global: '全局共享（租户内）',
    department: '部门共享',
    user: '私有（仅指定成员）'
  }
  return labels[shareConfig.value.access_level] || shareConfig.value.access_level
})

const nextStepHint = computed(() => {
  if (!state.selectedContractKey) return '在远端系统中管理数据，本平台仅代理检索'
  if (state.selectedContractKey === 'generic_document') {
    return GENERIC_DOCUMENT_PARENT.nextStep
  }
  if (state.selectedContractKey === 'managed_graph') return CONTRACT_PARENTS[2].nextStep
  if (state.selectedContractKey.startsWith('csv_')) return CONTRACT_PARENTS[1].nextStep
  return CONTRACT_PARENTS[0].nextStep
})

const loadSourceContracts = async () => {
  state.contractsLoading = true
  try {
    const data = await sourceContractApi.getContracts()
    state.sourceContracts = data.contracts || []
  } catch (error) {
    console.error('加载知识源契约失败:', error)
    state.sourceContracts = []
  } finally {
    state.contractsLoading = false
  }
}

const goNextStep = () => {
  if (state.wizardStep === 1) {
    if (!state.selectedContractKey && !newDatabase.kb_type) {
      message.warning('请选择一个知识源契约（或展开高级区域连接外部知识源）')
      return
    }
    state.wizardStep = 2
    return
  }
  if (state.wizardStep === 2) {
    if (!newDatabase.name.trim()) {
      message.warning('请输入知识库名称')
      return
    }
    if (requiresEmbeddingModel.value && !newDatabase.embedding_model_spec) {
      message.warning('请选择嵌入模型')
      return
    }
    state.wizardStep = 3
    return
  }
  if (state.wizardStep === 3) {
    state.wizardStep = 4
  }
}

// 权限默认值：Private（仅创建者可见）；团队/部门/全局共享必须显式选择。
// 此前默认 global 且总是发送，导致绕过后端更安全的默认值——已修复。
const createDefaultShareConfig = () => ({
  access_level: 'user',
  department_ids: [],
  user_uids: []
})

const shareConfig = ref(createDefaultShareConfig())
const shareConfigFormRef = ref(null)

const createEmptyDatabaseForm = () => ({
  name: '',
  description: '',
  content_domain: '',
  tool_description: '',
  embedding_model_spec: configStore.config?.embed_model,
  kb_type: '',
  additional_params: {}
})

const newDatabase = reactive(createEmptyDatabaseForm())

// 支持的知识库类型
const supportedKbTypes = ref({})

// 高级区：外部知识源类型（milvus 只能通过知识源契约进入）
const advancedKbTypes = computed(() =>
  Object.keys(supportedKbTypes.value).filter((type) => type !== 'milvus')
)

const selectedKbTypeInfo = computed(() => supportedKbTypes.value[newDatabase.kb_type] || null)

const createParamOptions = computed(() => selectedKbTypeInfo.value?.create_params?.options || [])

const getKbTypeDescription = (typeInfo) => typeInfo?.description || ''

const resetCreateParamValues = () => {
  newDatabase.additional_params = {}
  for (const field of createParamOptions.value) {
    if ('default' in field) {
      newDatabase.additional_params[field.key] = field.default
    } else if (field.type === 'boolean') {
      newDatabase.additional_params[field.key] = false
    } else {
      newDatabase.additional_params[field.key] = ''
    }
  }
}

// 加载支持的知识库类型
const loadSupportedKbTypes = async () => {
  try {
    const data = await typeApi.getKnowledgeBaseTypes()
    supportedKbTypes.value = data.kb_types || {}
    resetCreateParamValues()
  } catch (error) {
    console.error('加载知识库类型失败:', error)
    supportedKbTypes.value = {}
    resetCreateParamValues()
    message.error('加载知识库类型失败，请稍后重试')
  }
}

const resetNewDatabase = () => {
  Object.assign(newDatabase, createEmptyDatabaseForm())
  newDatabase.kb_type = ''
  state.wizardStep = 1
  state.selectedContractKey = ''
  resetCreateParamValues()
  shareConfig.value = createDefaultShareConfig()
}

const cancelCreateDatabase = () => {
  state.openNewDatabaseModel = false
  resetNewDatabase()
}

// 格式化创建时间
const formatCreatedTime = (createdAt) => {
  if (!createdAt) return ''
  const parsed = parseToShanghai(createdAt)
  if (!parsed) return ''

  const today = dayjs().startOf('day')
  const createdDay = parsed.startOf('day')
  const diffInDays = today.diff(createdDay, 'day')

  if (diffInDays === 0) {
    return '今天创建'
  }
  if (diffInDays === 1) {
    return '昨天创建'
  }
  if (diffInDays < 7) {
    return `${diffInDays} 天前创建`
  }
  if (diffInDays < 30) {
    const weeks = Math.floor(diffInDays / 7)
    return `${weeks} 周前创建`
  }
  if (diffInDays < 365) {
    const months = Math.floor(diffInDays / 30)
    return `${months} 个月前创建`
  }
  const years = Math.floor(diffInDays / 365)
  return `${years} 年前创建`
}

// 处理知识库类型改变
// 构建请求数据：契约路径携带 source_contract；高级路径走 legacy 兼容
const buildRequestData = () => {
  const requestData = {
    database_name: newDatabase.name.trim(),
    description: newDatabase.description?.trim() || '',
    kb_type: newDatabase.kb_type,
    additional_params: {}
  }

  if (requiresEmbeddingModel.value) {
    requestData.embedding_model_spec =
      newDatabase.embedding_model_spec || configStore.config.embed_model
  }

  requestData.share_config = {
    access_level: shareConfig.value.access_level,
    department_ids:
      shareConfig.value.access_level === 'department' ? shareConfig.value.department_ids || [] : [],
    user_uids: shareConfig.value.access_level === 'user' ? shareConfig.value.user_uids || [] : []
  }

  // 内容领域与工具说明对契约路径与外部连接器路径同属 Step2 表单（后端 create_database
  // 统一受理），必须放公共段——此前只在契约分支写入，连接器路径用户填了即被静默丢弃
  requestData.content_domain = newDatabase.content_domain?.trim() || ''
  requestData.tool_description = newDatabase.tool_description?.trim() || ''

  if (state.selectedContractKey) {
    // 契约路径：分块/解析/检索参数由系统托管，前端不传处理参数。
    // 只提交 key 不提交版本——后端 resolve_contract 权威解析为该 key 的
    // 最新版本，杜绝前端版本选择与注册中心漂移。
    requestData.source_contract = {
      key: state.selectedContractKey
    }
    return requestData
  }

  // 高级路径：外部知识源类型自己的动态参数
  // （storage 死分支已移除：UI 从未写入该值，且高级区不可能出现 milvus）
  for (const field of createParamOptions.value) {
    const value = newDatabase.additional_params[field.key]
    requestData.additional_params[field.key] = typeof value === 'string' ? value.trim() : value
  }

  return requestData
}

// 创建按钮处理
const handleCreateDatabase = async () => {
  if (shareConfigFormRef.value) {
    const validation = shareConfigFormRef.value.validate()
    if (!validation.valid) {
      message.warning(validation.message)
      return
    }
  }

  const requestData = buildRequestData()
  try {
    const selectedKey = state.selectedContractKey
    const data = await databaseStore.createDatabase(requestData)
    resetNewDatabase()
    state.openNewDatabaseModel = false
    // 规范图谱创建成功后直接引导到图谱页执行 CSV 导入
    if (selectedKey === 'managed_graph') {
      const createdKbId = data?.kb_id || data?.database?.kb_id || ''
      if (createdKbId) {
        router.push(`/extensions/knowledgebase/${createdKbId}?tab=graph`)
        message.info('知识库已创建（DRAFT），请在图谱页导入节点 CSV 与关系 CSV')
      } else {
        // 响应缺失 kb_id 属异常：留在列表页提示，避免跳到列表首个（可能非刚建的）库
        message.warning('知识库已创建，但响应未包含 ID，请在列表中打开新库执行图谱导入')
      }
    }
  } catch {
    // 错误已在 store 中处理
  }
}

const cardSubtitle = (database) => {
  const parts = []
  if (database.created_at) {
    parts.push(formatCreatedTime(database.created_at))
  }
  if (!kbUtils.isReadOnlyDatabase(database)) {
    parts.push(`${database.row_count || 0} 文件`)
  }
  return parts.join(' · ')
}

const cardTags = (database) => {
  const tags = [
    {
      name: getKbTypeLabel(database.kb_type || 'milvus'),
      color: getKbTypeColor(database.kb_type || 'milvus')
    }
  ]
  if (database.embedding_model_spec) {
    tags.push({
      name: database.embedding_model_spec.split('/').slice(-1)[0],
      color: 'blue'
    })
  }
  if (database.kb_type === 'llmwiki') {
    tags.push({ name: '仅导航 · 非证据', color: 'orange' })
  }
  return tags
}

const navigateToDatabase = (database) => {
  if (database.kb_type === 'llmwiki') {
    const wikiId = database.additional_params?.wiki_id || database.metadata?.wiki_id
    if (wikiId) {
      router.push({ path: `/extensions/wiki/${wikiId}` })
      return
    }
  }
  router.push({ path: `/extensions/knowledgebase/${database.kb_id}` })
}

const copyDatabaseId = async (database) => {
  try {
    await navigator.clipboard.writeText(database.kb_id)
  } catch {
    const textArea = document.createElement('textarea')
    textArea.value = database.kb_id
    document.body.appendChild(textArea)
    textArea.select()
    document.execCommand('copy')
    document.body.removeChild(textArea)
  }
  message.success('知识库 ID 已复制')
}

const deleteDatabase = (database) => {
  Modal.confirm({
    title: '删除知识库',
    content: `确定要删除知识库“${database.name}”吗？此操作不可撤销。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        if (database.kb_type === 'llmwiki') {
          const wikiId = database.additional_params?.wiki_id || database.metadata?.wiki_id
          if (!wikiId) throw new Error('动态 Wiki 标识缺失')
          await wikiApi.remove(wikiId)
        } else {
          await databaseApi.deleteDatabase(database.kb_id)
        }
        message.success('知识库已删除')
        await databaseStore.loadDatabases()
      } catch (error) {
        message.error(error.message || '删除失败')
        throw error
      }
    }
  })
}

const handleDatabaseAction = (key, database) => {
  if (key === 'copy') {
    copyDatabaseId(database)
    return
  }
  if (key === 'edit') {
    if (database.kb_type === 'llmwiki') {
      navigateToDatabase(database)
      return
    }
    router.push({
      path: `/extensions/knowledgebase/${database.kb_id}`,
      query: { action: 'edit' }
    })
    return
  }
  if (key === 'delete') {
    deleteDatabase(database)
  }
}

watch(
  () => route.path,
  (newPath) => {
    if (newPath === '/extensions' && route.query.tab === 'knowledge') {
      databaseStore.loadDatabases()
    }
  }
)

onMounted(() => {
  loadSourceContracts()
  loadSupportedKbTypes()
  databaseStore.loadDatabases()
  loadDefaultScope()
})

defineExpose({
  loading: computed(() => dbState.value.listLoading)
})
</script>

<style lang="less" scoped>
.database-container {
  :deep(.info-card-icon) {
    background: var(--gray-0);
  }
}

.wiki-create-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

// 表单通用规则：必须放 scoped 顶层。a-modal 内容会 teleport 到 body，
// 嵌套在某个 modal 容器（如 .new-database-modal）内的规则无法命中
// 其他 modal 的同类元素——「新建动态 Wiki」的下拉框曾因此宽度塌陷。
.form-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.form-section.compact-section {
  gap: 6px;
}

.form-grid {
  display: grid;
  gap: 16px;

  &.two-columns {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  &.three-columns {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  @media (max-width: 768px) {
    &.two-columns,
    &.three-columns {
      grid-template-columns: 1fr;
    }
  }
}

.full-width {
  width: 100%;
}

.section-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-800);
}

.required-mark {
  margin-left: 2px;
  color: var(--color-error-500);
}

.field-hint {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--gray-600);
}

.wiki-policy-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-10);
}

.scope-config {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.scope-summary,
.scope-enabled-row,
.priority-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.scope-kb-name,
.scope-section-title {
  color: var(--gray-900);
  font-size: 14px;
  font-weight: 600;
}

.scope-kb-id,
.scope-section-hint {
  margin-top: 3px;
  color: var(--gray-500);
  font-size: 12px;
}

.scope-kb-id {
  font-family: monospace;
}

.scope-section {
  padding: 14px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-10);
}

.scope-option-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
}

.scope-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);

  > span {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--gray-700);
    font-size: 12px;
  }
}

.scope-option-locked {
  opacity: 0.6;
  background: var(--gray-25);
}

.derived-product-hint {
  margin-bottom: 12px;
}

.evidence-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 12px 0;
}

.scope-health-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
}

.scope-health-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 9px;
  border-radius: 6px;
  background: var(--gray-50);
  color: var(--gray-500);
  font-size: 11px;

  strong {
    color: var(--gray-900);
    font-size: 16px;
  }
}

.scope-card-state {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-600);
  font-size: 12px;
}

.scope-state-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--gray-300);

  &.is-healthy {
    background: var(--color-success-500);
  }

  &.is-warning {
    background: var(--color-warning-500);
  }

  &.is-error {
    background: var(--color-error-500);
  }
}

@media (max-width: 768px) {
  .scope-option-grid,
  .evidence-options {
    grid-template-columns: 1fr;
  }

  .scope-health-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.new-database-modal {
  .new-database-form {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .compact-model-selector {
    height: 40px;
  }

  .description-hint {
    margin-top: -2px;
  }

  .chunk-preset-title-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .chunk-preset-help-icon {
    color: var(--gray-500);
    cursor: help;
    font-size: 14px;
  }

  .kb-type-guide {
    margin: 12px 0;
  }

  .privacy-config {
    display: flex;
    align-items: center;
    margin-bottom: 12px;
  }

  .kb-type-cards {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin: 4px 0 0;

    @media (max-width: 768px) {
      grid-template-columns: 1fr;
      gap: 10px;
    }
  }

  .format-template-cards {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: stretch;
    gap: 12px;

    @media (max-width: 768px) {
      grid-template-columns: 1fr;
    }
  }

  .format-template-card {
    display: flex;
    min-width: 0;
    flex-direction: column;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    background: var(--gray-0);
    overflow: hidden;
    transition:
      border-color 0.2s ease,
      background 0.2s ease;

    &:hover {
      border-color: var(--main-color);
    }

    &.active {
      border-color: var(--main-color);
      background: var(--main-10);
    }

    .template-card-select {
      display: flex;
      flex: 1;
      flex-direction: column;
      align-items: stretch;
      gap: 8px;
      width: 100%;
      padding: 16px;
      border: 0;
      outline: 0;
      background: transparent;
      color: inherit;
      text-align: left;
      cursor: pointer;

      &:focus-visible {
        box-shadow: inset 0 0 0 2px var(--main-color);
      }
    }

    .card-header {
      display: flex;
      align-items: center;
      min-height: 24px;

      .type-title {
        color: var(--gray-800);
        font-size: 14px;
        font-weight: 600;
      }
    }

    .card-description {
      flex: 1;
      color: var(--gray-600);
      font-size: 12px;
      line-height: 1.55;
    }

    .template-select-control {
      display: inline-flex;
      align-items: center;
      align-self: flex-start;
      gap: 6px;
      min-height: 30px;
      padding: 4px 10px;
      border: 1px solid var(--gray-200);
      border-radius: 6px;
      color: var(--gray-700);
      font-size: 12px;
      font-weight: 500;
    }

    &.active .template-select-control {
      border-color: var(--main-color);
      background: var(--main-color);
      color: var(--gray-0);
    }

    .template-sub-option {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 12px 16px 16px;
      border-top: 1px solid var(--gray-150);
    }

    .sub-option-label {
      color: var(--gray-600);
      font-size: 12px;
      font-weight: 500;
    }
  }

  .wizard-steps {
    margin-bottom: 16px;
  }

  .advanced-kb-collapse {
    margin-top: 12px;

    :deep(.ant-collapse-header) {
      color: var(--gray-600);
      font-size: 13px;
    }

    .advanced-source-section + .advanced-source-section {
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid var(--gray-150);
    }

    .advanced-source-title {
      margin: 0 0 8px;
      color: var(--gray-700);
      font-size: 13px;
      font-weight: 600;
    }

    .generic-document-card {
      max-width: none;
    }
  }

  .policy-panel {
    margin-top: 12px;
    padding: 12px 16px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    background: var(--gray-0);

    .policy-row {
      display: flex;
      gap: 12px;
      padding: 6px 0;
      font-size: 13px;
      line-height: 1.6;

      & + .policy-row {
        border-top: 1px dashed var(--gray-150);
      }

      .policy-key {
        flex: 0 0 88px;
        color: var(--gray-500);
      }

      .policy-value {
        color: var(--gray-800);
      }
    }
  }

  .contract-preview {
    margin-bottom: 12px;

    .contract-digest {
      margin-left: 8px;
      color: var(--gray-400);
      font-family: monospace;
      font-size: 11px;
    }
  }

  .kb-type-card {
    position: relative;
    padding: 14px;
    border: 1px solid var(--gray-150);
    border-radius: 8px;
    overflow: hidden;
    background: var(--gray-0);
    cursor: pointer;
    transition: all 0.2s ease;

    &:hover {
      border-color: var(--main-color);
    }

    &.active {
      border-color: var(--main-color);
      background: var(--main-10);
      box-shadow: 0 0 0 1px var(--main-20);

      .type-icon {
        color: var(--main-color);
      }
    }

    .card-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;

      .type-icon {
        width: 20px;
        height: 20px;
        color: var(--main-color);
        flex-shrink: 0;
      }

      .type-title {
        color: var(--gray-800);
        font-size: 15px;
        font-weight: 600;
      }
    }

    .card-description {
      margin-bottom: 0;
      color: var(--gray-600);
      font-size: 13px;
      line-height: 1.5;
    }
  }

  .chunk-config {
    margin-top: 16px;
    padding: 12px 16px;
    background-color: var(--gray-25);
    border-radius: 6px;
    border: 1px solid var(--gray-150);

    h3 {
      margin-top: 0;
      margin-bottom: 12px;
      color: var(--gray-800);
    }

    .chunk-params {
      display: flex;
      flex-direction: column;
      gap: 12px;

      .param-row {
        display: flex;
        align-items: center;
        gap: 12px;

        label {
          min-width: 80px;
          font-weight: 500;
          color: var(--gray-700);
        }

        .param-hint {
          font-size: 12px;
          color: var(--gray-500);
          margin-left: 8px;
        }
      }
    }
  }
}

.database-container {
  padding: 0;
}

.loading-container {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  height: 300px;
  gap: 16px;
}

.new-database-modal {
  h3 {
    margin-top: 10px;
  }
}
</style>
