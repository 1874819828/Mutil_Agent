<template>
  <div>
    <el-card shadow="never" style="margin-bottom:16px">
      <el-space>
        <el-button type="primary" :icon="Plus" @click="showDialog()">新增班级</el-button>
      </el-space>
    </el-card>

    <el-card shadow="never">
      <el-table :data="tableData" v-loading="loading" stripe border>
        <el-table-column prop="name" label="班级名称" />
        <el-table-column prop="grade" label="年级/入学年份" width="120" />
        <el-table-column prop="head_teacher" label="班主任" width="120" />
        <el-table-column prop="student_count" label="学生人数" width="100" />
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="showDialog(row)">编辑</el-button>
            <el-button type="danger" link size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑班级' : '新增班级'" width="500px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="班级名称" prop="name">
          <el-input v-model="form.name" placeholder="如: 软件技术2024-1班" />
        </el-form-item>
        <el-form-item label="入学年份" prop="grade">
          <el-input-number v-model="form.grade" :min="2000" :max="2100" />
        </el-form-item>
        <el-form-item label="班主任">
          <el-input v-model="form.head_teacher" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { listClasses, createClass, updateClass, deleteClass } from '../../api/classes'

const loading = ref(false)
const tableData = ref([])
const dialogVisible = ref(false)
const saving = ref(false)
const editing = ref(null)
const formRef = ref(null)
const form = reactive({ name: '', grade: 2024, head_teacher: '' })
const rules = {
  name: [{ required: true, message: '请输入班级名称', trigger: 'blur' }]
}

async function fetchData() {
  loading.value = true
  try { tableData.value = await listClasses() } finally { loading.value = false }
}

function showDialog(row = null) {
  editing.value = row
  if (row) {
    form.name = row.name; form.grade = row.grade; form.head_teacher = row.head_teacher || ''
  } else {
    form.name = ''; form.grade = 2024; form.head_teacher = ''
  }
  dialogVisible.value = true
}

async function handleSave() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    if (editing.value) {
      await updateClass(editing.value.id, { ...form })
      ElMessage.success('更新成功')
    } else {
      await createClass({ ...form })
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally { saving.value = false }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除班级 "${row.name}" 吗？`, '确认删除', { type: 'warning' })
    await deleteClass(row.id)
    ElMessage.success('删除成功')
    fetchData()
  } catch { /* 取消 */ }
}

onMounted(fetchData)
</script>
