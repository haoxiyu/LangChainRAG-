import client from './client'

// ---------- 认证 ----------
export const authApi = {
  register: (data) => client.post('/auth/register', data),
  login: (data) => client.post('/auth/login', data),
  me: () => client.get('/auth/me'),
  changePassword: (data) => client.post('/auth/change-password', data),
}

// ---------- 知识库 ----------
export const kbApi = {
  list: () => client.get('/knowledge-bases'),
  get: (id) => client.get(`/knowledge-bases/${id}`),
  create: (data) => client.post('/knowledge-bases', data),
  update: (id, data) => client.patch(`/knowledge-bases/${id}`, data),
  remove: (id) => client.delete(`/knowledge-bases/${id}`),
}

// ---------- 文档 ----------
export const docApi = {
  list: (kbId) => client.get(`/knowledge-bases/${kbId}/documents`),
  upload: (kbId, file, onProgress) => {
    const form = new FormData()
    form.append('file', file)
    return client.post(`/knowledge-bases/${kbId}/documents`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (e) => {
        if (onProgress && e.total) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      },
    })
  },
  remove: (kbId, docId) => client.delete(`/knowledge-bases/${kbId}/documents/${docId}`),
  chunks: (kbId, docId, params) =>
    client.get(`/knowledge-bases/${kbId}/documents/${docId}/chunks`, { params }),
  reprocess: (kbId, docId) =>
    client.post(`/knowledge-bases/${kbId}/documents/${docId}/reprocess`),
}

// ---------- 会话 ----------
export const convApi = {
  list: (params) => client.get('/conversations', { params }),
  create: (data) => client.post('/conversations', data),
  get: (id, params) => client.get(`/conversations/${id}`, { params }),
  messages: (id, params) => client.get(`/conversations/${id}/messages`, { params }),
  // 局部更新:目前只支持重命名
  update: (id, data) => client.patch(`/conversations/${id}`, data),
  rename: (id, title) => client.patch(`/conversations/${id}`, { title }),
  remove: (id) => client.delete(`/conversations/${id}`),
  clearAll: () => client.delete('/conversations'),
}

// ---------- 管理 ----------
export const adminApi = {
  stats: () => client.get('/admin/stats'),
  users: (params) => client.get('/admin/users', { params }),
}

// ---------- 上传限制 ----------
// 扩展名与大小上限都由服务端下发,前端不再各写一份常量:
// 只有一份配置,才不会出现前端拦下后端本可接受的文件这种偏差。
export const uploadApi = {
  limits: () => client.get('/admin/upload-limits'),
}

/**
 * 流式问答。
 *
 * 浏览器端用 fetch + ReadableStream 读 SSE,而不是 EventSource ——
 * EventSource 只支持 GET 且无法带自定义请求头,拿不到 Authorization。
 *
 * @param {object} payload  { conversation_id?, question }
 * @param {(event: object) => void} onEvent  每收到一个事件回调一次
 * @param {AbortSignal} signal  用于「停止生成」
 */
export async function streamChat(payload, onEvent, signal) {
  const token = localStorage.getItem('rag_token')
  const resp = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
    signal,
  })

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const data = await resp.json()
      if (data?.detail) detail = data.detail
    } catch {
      /* 响应体不是 JSON,沿用状态码 */
    }
    throw new Error(detail)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 以空行分隔事件;最后一段可能不完整,留到下一轮
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() ?? ''
    for (const block of blocks) {
      const line = block.trim()
      if (!line.startsWith('data:')) continue
      try {
        onEvent(JSON.parse(line.slice(5).trim()))
      } catch {
        // 忽略无法解析的心跳/注释行
      }
    }
  }
}
