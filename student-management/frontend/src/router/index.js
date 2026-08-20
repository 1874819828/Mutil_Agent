import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/Login.vue'),
      meta: { title: '登录' }
    },
    {
      path: '/',
      component: () => import('../views/Layout.vue'),
      redirect: '/students',
      children: [
        {
          path: 'students',
          name: 'students',
          component: () => import('../views/students/List.vue'),
          meta: { title: '学生管理' }
        },
        {
          path: 'students/create',
          name: 'student-create',
          component: () => import('../views/students/Form.vue'),
          meta: { title: '新增学生' }
        },
        {
          path: 'students/:id/edit',
          name: 'student-edit',
          component: () => import('../views/students/Form.vue'),
          meta: { title: '编辑学生' },
          props: true
        },
        {
          path: 'classes',
          name: 'classes',
          component: () => import('../views/classes/List.vue'),
          meta: { title: '班级管理' }
        }
      ]
    }
  ]
})

router.beforeEach((to, from, next) => {
  if (to.path === '/login') return next()
  const token = localStorage.getItem('token')
  if (!token) return next('/login')
  next()
})

export default router
