/**
 * 通用危险操作确认（替代原生 window.confirm：不阻塞主线程、样式与主题一致）。
 * 返回 Promise<boolean>，仅在用户确认时 resolve true。
 *
 * 历史实现位于 useLiteratureSessions.js（文献域），因其本质是通用 UI 原语，
 * 迁移到独立模块供全站视图复用；useLiteratureSessions 对外保持再导出兼容。
 */
import { Modal } from 'ant-design-vue'

export function confirmAction(content) {
  return new Promise((resolve) => {
    Modal.confirm({
      title: '请确认',
      content,
      okText: '确认',
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
      // Modal.destroyAll()/programmatic close need not invoke onCancel.
      afterClose: () => resolve(false)
    })
  })
}
