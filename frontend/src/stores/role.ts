import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useRoleStore = defineStore('role', () => {
  const role = ref<'user' | 'admin'>('user')
  
  function setRole(r: 'user' | 'admin') {
    role.value = r
  }
  
  return { role, setRole }
})
