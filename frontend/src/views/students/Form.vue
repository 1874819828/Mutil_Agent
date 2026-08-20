<template>
  <el-card shadow="never">
    <template #header>
      <span>{{ isEdit ? '编辑学生' : '新增学生' }}</span>
    </template>

    <el-form ref="formRef" :model="form" :rules="rules" label-width="100px" style="max-width:600px">
      <el-form-item label="学号" prop="student_no">
        <el-input v-model="form.student_no" :disabled="isEdit" />
      </el-form-item>
      <el-form-item label="姓名" prop="name">
        <el-input v-model="form.name" />
      </el-form-item>
      <el-form-item label="性别" prop="gender">
        <el-radio-group v-model="form.gender">
          <el-radio value="男">男</el-radio>
          <el-radio value="女">女</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="出生日期">
        <el-date-picker v-model="form.birth_date" type="date" placeholder="选择日期" value-format="YYYY-MM-DD" />
      </el-form-item>
      <el-form-item label="班级">
        <el-select v-model="form.class_id" clearable placeholder="请选择班级" style="width:100%">
          <el-option v-for="c in classList" :key="c.id" :label="c.name" :value="c.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="联系电话">
        <el-input v-model="form.phone" />
      </el-form-item>
      <el-form-item label="邮箱">
        <el-input v-model="form.email" />
      </el-form-item>
      <el-form-item label="家庭地址">
        <el-input v-model="form.address" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="form.status" style="width:120px">
          <el-option v-for="s in ['在读','休学','退学','毕业']" :key="s" :label="s" :value="s" />
        </el-select>
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
        <el-button @click="$router.back()">返回</el-button>
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getStudent, createStudent, updateStudent } from '../../api/students'
import { listClasses } from '../../api/classes'

const route = useRoute()
const router = useRouter()
const isEdit = computed(() => !!route.params.id)
const formRef = ref(null)
const saving = ref(false)
const classList = ref([])

const form = reactive({
  student_no: '', name: '', gender: '男', birth_date: null,
  class_id: null, phone: '', email: '', address: '', status: '在读', remark: ''
})

const rules = {
  student_no: [{ required: true, message: '请输入学号', trigger: 'blur' }],
  name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  gender: [{ required: true, message: '请选择性别', trigger: 'change' }]
}

async function handleSave() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    const data = { ...form }
    if (!data.class_id) data.class_id = null
    if (isEdit.value) {
      await updateStudent(route.params.id, data)
      ElMessage.success('更新成功')
    } else {
      await createStudent(data)
      ElMessage.success('新增成功')
    }
    router.push('/students')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  classList.value = await listClasses()
  if (isEdit.value) {
    const student = await getStudent(route.params.id)
    Object.assign(form, student)
  }
})
</script>
