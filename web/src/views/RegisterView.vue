<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { authApi } from '@/apis/auth_api'
import { CheckCircle2, Copy, Eye, EyeOff, Hourglass, Leaf, LockKeyhole, ShieldCheck } from '@lucide/vue'

const router = useRouter()
const form = reactive({
  uid: '',
  username: '',
  password: '',
  confirmPassword: '',
  departmentId: null,
  inviteCode: ''
})
const loading = ref(false)
const error = ref('')
const submitted = ref(false)
const inviteRequired = ref(false)
const departments = ref([])
const result = ref(null)
const showKey = ref(false)
const copied = ref(false)

async function submit() {
  if (form.uid.trim().length < 3 || form.uid.trim().length > 20) {
    error.value = '登录 ID 需为 3-20 位字母、数字或下划线'
    return
  }
  if (form.password.length < 8) {
    error.value = '密码至少需要 8 位'
    return
  }
  if (form.password !== form.confirmPassword) {
    error.value = '两次输入的密码不一致'
    return
  }
  if (!form.departmentId) {
    error.value = '请选择申请加入的部门'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await authApi.register({
      uid: form.uid.trim(),
      username: form.username.trim(),
      password: form.password,
      department_id: form.departmentId,
      invite_code: form.inviteCode.trim() || null
    })
    result.value = res
    submitted.value = true
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function copyKey() {
  try {
    await navigator.clipboard.writeText(result.value.api_key)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch {
    /* 剪贴板不可用时忽略，用户可手动选择复制 */
  }
}

onMounted(async () => {
  try {
    const config = await authApi.getRegisterConfig()
    if (!config.enabled) {
      await router.replace('/login')
      return
    }
    inviteRequired.value = Boolean(config.invite_required)
    departments.value = config.departments || []
  } catch (e) {
    error.value = e.message
  }
})
</script>

<template>
  <div class="register-view">
    <div class="register-decoration decoration-one" />
    <div class="register-decoration decoration-two" />
    <main class="register-shell">
      <aside class="register-rail">
        <div class="register-brand">
          <img src="/brand/rice-endosperm/indexlogo.png" alt="稻芯智析徽标">
          <div><span>稻芯智析</span><small>水稻胚乳科研智能体</small></div>
        </div>
        <div class="register-rail-copy">
          <span class="register-icon"><Leaf size="22" /></span>
          <p class="eyebrow">ENTERPRISE RESEARCH AI</p>
          <h1>申请你的科研账号</h1>
          <p>注册提交后由企业管理员审核启用；账号、模型与知识范围均由服务端统一管理。</p>
        </div>
        <ul class="register-assurances">
          <li><ShieldCheck :size="17" /><span><strong>审批制开户</strong>注册后处于停用状态，管理员启用后生效</span></li>
          <li><LockKeyhole :size="17" /><span><strong>密钥一次下发</strong>API Key 明文仅注册时展示一次，服务端只存哈希</span></li>
          <li><Hourglass :size="17" /><span><strong>部门归属</strong>选择申请部门，管理员按部门治理权限</span></li>
        </ul>
      </aside>

      <section class="register-workspace">
        <header class="register-header">
          <div><span>新用户注册</span><h2>创建稻芯智析账号</h2></div>
          <span class="register-pill"><ShieldCheck :size="14" />企业审批制</span>
        </header>

        <template v-if="!submitted">
          <form class="register-form" @submit.prevent="submit">
            <div class="form-heading">
              <h3>账号信息</h3>
              <p>三项基础信息将进入管理员审核队列</p>
            </div>
            <label>
              <span>登录 ID</span>
              <input v-model="form.uid" placeholder="3-20 位字母/数字/下划线" autocomplete="username" required>
            </label>
            <label>
              <span>显示名称</span>
              <input v-model="form.username" placeholder="你的姓名或昵称" required>
            </label>
            <label>
              <span>申请部门</span>
              <select v-model="form.departmentId" required>
                <option :value="null" disabled>选择申请加入的部门</option>
                <option v-for="dept in departments" :key="dept.id" :value="dept.id">{{ dept.name }}</option>
              </select>
            </label>
            <div class="form-row">
              <label>
                <span>设置密码（至少 8 位）</span>
                <input v-model="form.password" type="password" autocomplete="new-password" required>
              </label>
              <label>
                <span>确认密码</span>
                <input v-model="form.confirmPassword" type="password" autocomplete="new-password" required>
              </label>
            </div>
            <label v-if="inviteRequired">
              <span>邀请码</span>
              <input v-model="form.inviteCode" placeholder="如系统要求请填写" autocomplete="off">
            </label>

            <p v-if="error" class="register-error" role="alert">{{ error }}</p>
            <button class="register-submit" :disabled="loading || !form.uid || !form.username || !form.password || !form.departmentId">
              {{ loading ? '正在提交注册…' : '提交注册申请' }}
            </button>
            <p class="register-footer">
              已有账号？
              <router-link to="/login">返回登录</router-link>
            </p>
          </form>
        </template>

        <template v-else>
          <div class="register-result">
            <CheckCircle2 class="result-icon" :size="44" />
            <h3>注册已提交，等待管理员审核</h3>
            <p class="result-lead">
              账号 <strong>{{ result.uid }}</strong>（{{ result.username }}）已进入
              <strong>{{ result.department }}</strong> 的审核队列。管理员在"用户管理"中启用后即可登录使用。
            </p>
            <div class="key-card">
              <div class="key-card-head">
                <span>你的专属 API Key（仅此一次展示，请立即保存）</span>
                <button type="button" @click="copyKey">
                  <Copy :size="14" />{{ copied ? '已复制' : '复制' }}
                </button>
              </div>
              <div class="key-card-body">
                <code>{{ showKey ? result.api_key : result.api_key.replace(/./g, '•').slice(0, 42) }}</code>
                <button type="button" :aria-label="showKey ? '隐藏密钥' : '显示密钥'" @click="showKey = !showKey">
                  <EyeOff v-if="showKey" :size="16" />
                  <Eye v-else :size="16" />
                </button>
              </div>
              <p class="key-note">启用前该密钥处于冻结状态；刷新或关闭页面后无法再次查看，可在用户详情中由管理员重置。</p>
            </div>
            <button class="register-submit" @click="router.push('/login')">前往登录页</button>
          </div>
        </template>
      </section>
    </main>
  </div>
</template>

<style scoped>
.register-view {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f4f7f4;
  overflow: hidden;
  padding: 24px;
}

.register-decoration {
  position: absolute;
  border-radius: 50%;
  filter: blur(70px);
  opacity: 0.5;
}

.decoration-one { width: 420px; height: 420px; background: #cfe3d2; top: -140px; right: -120px; }
.decoration-two { width: 320px; height: 320px; background: #dbe7d8; bottom: -120px; left: -80px; }

.register-shell {
  position: relative;
  display: flex;
  width: min(960px, 100%);
  border: 1px solid #dfe7df;
  border-radius: 18px;
  background: #ffffff;
  box-shadow: 0 18px 48px rgba(31, 58, 60, 0.08);
  overflow: hidden;
}

.register-rail {
  flex: 0 0 320px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 30px 26px;
  background: linear-gradient(160deg, #24503c 0%, #3c6b4f 100%);
  color: #eaf3ec;
}

.register-brand { display: flex; align-items: center; gap: 10px; }
.register-brand img { width: 38px; height: 38px; border-radius: 10px; background: #fff; padding: 3px; }
.register-brand span { font-weight: 700; font-size: 16px; display: block; }
.register-brand small { font-size: 11px; opacity: 0.75; }

.register-rail-copy h1 { font-size: 20px; margin: 6px 0; line-height: 1.35; }
.register-rail-copy p { font-size: 12px; line-height: 1.7; opacity: 0.85; margin: 0; }
.eyebrow { letter-spacing: 2px; font-size: 10px !important; opacity: 0.7; }
.register-icon { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border-radius: 12px; background: rgba(255, 255, 255, 0.14); }

.register-assurances { list-style: none; margin: auto 0 0; padding: 0; display: flex; flex-direction: column; gap: 12px; }
.register-assurances li { display: flex; gap: 9px; font-size: 12px; line-height: 1.6; opacity: 0.92; }
.register-assurances strong { display: block; font-size: 12px; }

.register-workspace { flex: 1; padding: 30px 34px; display: flex; flex-direction: column; }
.register-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.register-header span { font-size: 11px; color: #7d8a80; letter-spacing: 1px; }
.register-header h2 { margin: 2px 0 0; font-size: 20px; color: #1f3a30; }
.register-pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid #cfe0d3; color: #2f6b4f; border-radius: 999px; padding: 5px 11px; font-size: 12px; background: #f2f8f3; }

.register-form { display: flex; flex-direction: column; gap: 13px; }
.form-heading h3 { margin: 0 0 2px; font-size: 14px; color: #23423a; }
.form-heading p { margin: 0; font-size: 12px; color: #8a948d; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

.register-form label { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: #4a5a50; }
.register-form input,
.register-form select {
  border: 1px solid #d6dfd7; border-radius: 9px; padding: 9px 11px; font-size: 13px;
  background: #fbfdfb; color: #22352b; outline: none; transition: border-color 0.15s;
}
.register-form input:focus, .register-form select:focus { border-color: #4d8a66; }

.register-error { color: #c2554d; font-size: 12px; margin: 0; }
.register-submit {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  border: 0; border-radius: 10px; padding: 11px 16px; cursor: pointer;
  background: linear-gradient(135deg, #2f6b4f, #3c7d5c); color: #fff; font-size: 14px; font-weight: 600;
}
.register-submit:disabled { opacity: 0.55; cursor: not-allowed; }
.register-footer { font-size: 12px; color: #8a948d; text-align: center; margin: 2px 0 0; }
.register-footer a { color: #2f6b4f; font-weight: 600; }

.register-result { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 10px; padding: 10px 0; }
.result-icon { color: #2f6b4f; }
.register-result h3 { margin: 0; font-size: 17px; color: #1f3a30; }
.result-lead { margin: 0; font-size: 13px; color: #5a6a60; line-height: 1.7; max-width: 480px; }

.key-card { width: min(520px, 100%); margin-top: 6px; border: 1px solid #cfe0d3; border-radius: 12px; background: #f4faf5; text-align: left; }
.key-card-head { display: flex; justify-content: space-between; align-items: center; padding: 9px 13px; font-size: 12px; color: #2f6b4f; border-bottom: 1px solid #dcebdd; font-weight: 600; }
.key-card-head button { display: inline-flex; align-items: center; gap: 5px; border: 1px solid #bcd6c2; background: #fff; color: #2f6b4f; border-radius: 7px; padding: 4px 9px; cursor: pointer; font-size: 12px; }
.key-card-body { display: flex; align-items: center; gap: 8px; padding: 11px 13px; }
.key-card-body code { flex: 1; font-family: 'Cascadia Code', Consolas, monospace; font-size: 13px; color: #1f3a30; word-break: break-all; }
.key-card-body button { border: 0; background: transparent; color: #52614f; cursor: pointer; display: inline-flex; }
.key-note { margin: 0; padding: 0 13px 11px; font-size: 11px; color: #8a948d; line-height: 1.6; }

@media (max-width: 860px) {
  .register-shell { flex-direction: column; }
  .register-rail { flex: none; }
  .form-row { grid-template-columns: 1fr; }
}
</style>
