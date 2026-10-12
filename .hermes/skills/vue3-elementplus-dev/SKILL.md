---
name: vue3-elementplus-dev
description: Vue3 + ElementPlus 开发约束，适配龙芯智能编译平台项目
category: software-development
---

# Vue3 + ElementPlus 开发规范（龙芯编译平台专用）

## 强制约束

### 语法约束
- **必须使用 `<script setup>` 语法**，禁止 Vue2 Options API
- **禁止使用**：`this.xxx`、`data()`、`methods:`、`computed:` 等传统写法
- **禁止使用**：`v-model` 旧写法（`.sync` 修饰符），统一用 `v-model:xxx`

### WebSocket 资源释放（高频坑）
所有 WebSocket 连接必须在组件卸载时关闭：
```vue
<script setup>
import { onMounted, onUnmounted } from 'vue'

let ws: WebSocket | null = null

onMounted(() => {
  ws = new WebSocket('ws://localhost:8000/ws/compile/log')
  ws.onmessage = (e) => { /* ... */ }
})

onUnmounted(() => {
  if (ws) {
    ws.close()
    ws = null
  }
})
</script>
```

### 长日志虚拟滚动（核心组件 LogViewer.vue）
使用 `el-virtual-list` 或自研虚拟滚动，禁止直接渲染全部日志行：
```vue
<template>
  <el-virtual-list
    ref="virtualList"
    :data="logLines"
    :item-size="24"
    :estimated-item-size="24"
    style="height: 400px; overflow: auto;"
  >
    <template #default="{ item }">
      <div class="log-line" :class="item.type">{{ item.text }}</div>
    </template>
  </el-virtual-list>
</template>
```

### UI 对齐要求
每次开发页面组件时，**必须对照 `compile-platform-mockup.html`**：
- 布局结构：侧边栏 210px、顶部栏 56px、主内容区 margin-left: 210px
- 颜色变量：`--primary: #3370ff`、`--success: #18b26b`、`--warning: #f59e0b`、`--danger: #ef4444`
- 表格样式：`th` 背景 `#fafbfc`，`td` 底部边框 `#f0f25`
- 标签样式：`.tag` 圆角 20px，`.t-green` 成功，`.t-blue` 信息，`.t-orange` 警告，`.t-red` 错误，`.t-gray` 灰色
- 卡片样式：`border-radius: 10px`，`padding: 18px`

### 组件开发清单
| 组件名 | 职责 | 关键特性 |
|--------|------|----------|
| `LogViewer.vue` | 编译日志展示 | 虚拟滚动、WebSocket 实时追加、自动滚动到底部 |
| `SkillPreview.vue` | SKILL.md 预览 | Markdown 渲染、代码块高亮 |
| `ChatBox.vue` | 工单对话 | 气泡布局、用户/管理员区分、消息时间戳 |
| `StatusTag.vue` | 状态标签 | 统一颜色映射、支持自定义颜色 |

### 状态标签颜色映射（必须与 mockup 一致）
```typescript
const statusColors = {
  'success': '#18b26b',
  'completed': '#18b26b',
  'failed': '#ef4444',
  'compiling': '#3370ff',
  'pending': '#f59e0b',
  'processing': '#f59e0b',
  'approved': '#18b26b',
  'rejected': '#ef4444',
  'under_review': '#f59e0b',
  'synced': '#18b26b',
}
```

### 弹窗/模态框规范
- 使用 `el-dialog`，禁止 `alert()`
- 弹窗标题统一格式：`📟 编译日志 #${taskId}`
- 弹窗底部按钮顺序：次要操作（关闭/取消）← 主要操作（确认/提交）
- 确认操作前必须有二次确认（`confirm-dialog`）

### 自检清单（每次完成组件后必须检查）
- [ ] 使用 `<script setup>` 语法
- [ ] WebSocket/定时器在 `onUnmounted` 中销毁
- [ ] 长列表使用虚拟滚动
- [ ] UI 布局与 mockup.html 对齐
- [ ] 状态标签颜色正确
- [ ] 弹窗使用 el-dialog 而非 alert
