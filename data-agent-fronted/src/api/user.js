import request from './request'

/** 获取当前登录用户信息（后端 Step 4，统一响应 { code, message, data }） */
export function getCurrentUser() {
  return request.get('/users/me').then((r) => r.data)
}
