import axios from 'axios'

/** @type {string} localStorage保存自定义阅读http服务接口的键值 */
export const baseURL_localStorage_key = 'remoteUrl'
/** localStorage保存阅读Web服务访问密码的键值 */
export const webServicePassword_localStorage_key = 'webServicePassword'
const SECOND = 1000

const ajax = axios.create({
  baseURL:
    import.meta.env.VITE_API ||
    localStorage.getItem(baseURL_localStorage_key) ||
    location.origin,
  timeout: 120 * SECOND,
})

// 所有请求携带访问密码（服务端开启认证时必需）
ajax.interceptors.request.use((config) => {
  const token = localStorage.getItem(webServicePassword_localStorage_key)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 密码缺失或错误时补一次输入并重试，避免整页功能直接不可用
ajax.interceptors.response.use(undefined, async (error) => {
  const config = error.config
  if (error.response?.status === 401 && config && !config.__authRetried) {
    const token = window.prompt('此阅读 Web 服务已开启访问密码，请输入密码')
    if (token) {
      localStorage.setItem(webServicePassword_localStorage_key, token)
      config.__authRetried = true
      config.headers.Authorization = `Bearer ${token}`
      return ajax.request(config)
    }
  }
  throw error
})

export default ajax
