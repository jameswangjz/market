<script setup>
import { reactive, ref } from "vue";
import axios from "axios";
import { KeyRound, X } from "lucide-vue-next";
const props = defineProps({ authenticated: Boolean, collapsed: Boolean });
const emit = defineEmits(["changed"]);
const open = ref(false), busy = ref(false), error = ref(""), notice = ref("");
const form = reactive({ identifier: "", code: "", current_password: "", new_password: "", confirmation: "" });
function show() {
  Object.keys(form).forEach(key => form[key] = "");
  error.value = notice.value = "";
  open.value = true;
}
function failure(e) {
  const detail = e.response?.data?.detail;
  error.value = typeof detail === "string" ? detail : "操作失败，请检查输入或稍后重试";
}
async function sendCode() {
  if (busy.value) return;
  if (!form.identifier.trim()) { error.value = "请输入用户名、邮箱或手机号码"; return; }
  busy.value = true; error.value = "";
  try {
    const { data } = await axios.post("/api/auth/password-reset/request", { identifier: form.identifier });
    notice.value = data.message;
  } catch (e) { failure(e); }
  finally { busy.value = false; }
}
async function submit() {
  if (busy.value) return;
  error.value = "";
  if (form.new_password !== form.confirmation) { error.value = "两次输入的新密码不一致"; return; }
  if (!/[a-zA-Z]/.test(form.new_password) || !/[0-9]/.test(form.new_password)) {
    error.value = "新密码至少包含字母和数字"; return;
  }
  busy.value = true;
  try {
    const endpoint = props.authenticated ? "change-password" : "password-reset/confirm";
    const body = props.authenticated
      ? { current_password: form.current_password, new_password: form.new_password }
      : { identifier: form.identifier, code: form.code, new_password: form.new_password };
    const headers = props.authenticated ? { Authorization: `Bearer ${localStorage.getItem("market_token") || ""}` } : {};
    const { data } = await axios.post(`/api/auth/${endpoint}`, body, { headers });
    notice.value = data.message;
    Object.keys(form).forEach(key => form[key] = "");
    if (props.authenticated) { open.value = false; emit("changed"); }
  } catch (e) { failure(e); }
  finally { busy.value = false; }
}
</script>

<template>
  <button type="button" :class="authenticated ? 'nav-item' : 'text-btn full-btn'" :title="authenticated ? '修改密码' : '忘记密码'" @click="show">
    <KeyRound v-if="authenticated" :size="18" /><span v-if="!authenticated || !collapsed">{{ authenticated ? '修改密码' : '忘记密码' }}</span>
  </button>
  <Teleport to="body">
    <div v-if="open" class="modal-scrim password-security-scrim">
      <form class="modal-card password-security-dialog" role="dialog" aria-modal="true" aria-labelledby="password-security-title" @submit.prevent="submit">
        <div class="drawer-head">
          <h2 id="password-security-title">{{ authenticated ? '修改密码' : '重置密码' }}</h2>
          <button type="button" class="icon-btn" aria-label="关闭" :disabled="busy" @click="open = false"><X :size="18" /></button>
        </div>
        <label v-if="authenticated">原密码<input v-model="form.current_password" type="password" autocomplete="current-password" maxlength="128" required /></label>
        <template v-else>
          <label>用户名、邮箱或手机号码<input v-model="form.identifier" autocomplete="username" maxlength="255" required /></label>
          <button type="button" class="secondary-btn" :disabled="busy" @click="sendCode">发送邮箱验证码</button>
          <label>邮箱验证码<input v-model="form.code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>
        </template>
        <label>新密码<input v-model="form.new_password" type="password" autocomplete="new-password" minlength="8" maxlength="128" placeholder="8至128位，包含字母和数字" required /></label>
        <label>确认新密码<input v-model="form.confirmation" type="password" autocomplete="new-password" minlength="8" maxlength="128" required /></label>
        <p v-if="notice" role="status" class="muted">{{ notice }}</p>
        <p v-if="error" role="alert" class="error-text">{{ error }}</p>
        <div class="modal-actions">
          <button type="button" class="secondary-btn" :disabled="busy" @click="open = false">取消</button>
          <button type="submit" class="primary-btn" :disabled="busy">{{ busy ? '处理中…' : '确认修改' }}</button>
        </div>
      </form>
    </div>
  </Teleport>
</template>

<style scoped>
.password-security-scrim { z-index: 110; }
.password-security-dialog { width: min(460px, calc(100vw - 32px)); max-height: 90dvh; overflow-y: auto; }
.password-security-dialog label { display: grid; gap: 6px; margin: 12px 0; font-size: 13px; }
.password-security-dialog input { width: 100%; box-sizing: border-box; }
.password-security-dialog h2 { font-size: 18px; }
</style>
