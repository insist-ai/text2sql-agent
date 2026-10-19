import request from './request'

/**
 * 鉴权接口（后端 Step 3，统一响应 { code, message, data }）。
 * request 拦截器已解出 response.data，这里再取一层 .data 即业务数据。
 */

/** 注册 */
export function registerUser(data) {
  return request.post('/auth/register', data).then((r) => r.data)
}

/** 登录，返回 { access_token, refresh_token, token_type } */
export function loginUser(data) {
  return request.post('/auth/login', data).then((r) => r.data)
}
