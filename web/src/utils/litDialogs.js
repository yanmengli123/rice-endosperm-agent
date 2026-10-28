// 文献域的应用内确认 / 输入弹窗：替换原生 alert / confirm / prompt（企业级交互统一）。
// 复用 main.js 全局注册的 ant-design-vue：确认走 Modal.confirm（Promise 化），
// 提示类直接由调用方使用 antd 的 message（与 base.js、部门管理页同质感）。
// beforeunload 保留浏览器原生实现——那是平台级标准，不属于应用内弹窗。
import { createVNode, ref } from 'vue'
import { Input, Modal, message } from 'ant-design-vue'

/**
 * 应用内确认框。resolve(true)=确认，resolve(false)=取消。
 * content 支持字符串或 VNode（多行文本用 white-space: pre-line 的 VNode）。
 */
export function litConfirm({
  title,
  content = '',
  okText = '确定',
  cancelText = '取消',
  danger = false
}) {
  return new Promise((resolve) => {
    Modal.confirm({
      title,
      content,
      okText,
      cancelText,
      okButtonProps: danger ? { danger: true } : undefined,
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
      afterClose: () => resolve(false)
    })
  })
}

/**
 * 应用内单行输入框（替代 window.prompt）。resolve(trimmed)=确认，resolve(null)=取消；
 * required 且输入为空时阻止关闭并提示，不产生空提交。
 */
export function litPrompt({
  title,
  placeholder = '',
  okText = '确定',
  cancelText = '取消',
  required = false,
  requiredHint = '此项为必填'
}) {
  return new Promise((resolve) => {
    const value = ref('')
    Modal.confirm({
      title,
      width: 460,
      content: () =>
        createVNode(Input, {
          value: value.value,
          placeholder,
          maxlength: 500,
          'aria-label': title,
          'onUpdate:value': (next) => {
            value.value = next
          }
        }),
      okText,
      cancelText,
      onOk: (close) => {
        const trimmed = value.value.trim()
        if (required && !trimmed) {
          message.warning(requiredHint)
          // 带 close 参数的回调由调用方控制关闭；校验失败既不关闭，也不拒绝 Promise。
          return
        }
        resolve(trimmed)
        close()
      },
      onCancel: () => resolve(null),
      afterClose: () => resolve(null)
    })
  })
}
