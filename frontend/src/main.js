import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import 'element-plus/dist/index.css'
import '@/styles/main.css'

import App from '@/App.vue'
import router from '@/router'

const app = createApp(App)

// 图标不做全量全局注册:各组件按需 import,避免整套图标被打进主包
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
