// 404 与权限拒绝共用响应，避免向跨租户调用者暴露资源是否存在。
export const isGraphAccessDenied = (error) => [401, 403, 404].includes(error?.response?.status)
