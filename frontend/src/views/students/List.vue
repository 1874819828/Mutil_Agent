<template>
  <div>
    <!-- 搜索筛选栏 -->
    <el-card shadow="never" style="margin-bottom:16px">
      <el-form :inline="true" :model="filters">
        <el-form-item label="关键词">
          <el-input v-model="filters.keyword" placeholder="学号 / 姓名 / 手机" clearable style="width:220px" @keyup.enter="handleSearch" />
        </el-form-item>
        <el-form-item label="班级">
          <el-select v-model="filters.class_id" clearable placeholder="全部" style="width:160px">
            <el-option v-for="c in classList" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="性别">
          <el-select v-model="filters.gender" clearable placeholder="全部" style="width:100px">
            <el-option label="男" value="男" />
            <el-option label="女" value="女" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="filters.status" clearable placeholder="全部" style="width:120px">
            <el-option v-for="s in ['在读','休学','退学','毕业']" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">搜索</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 操作栏 -->
    <el-card shadow="never" style="margin-bottom:16px">
      <el-space>
        <el-button type="primary" :icon="Plus" @click="$router.push('/students/create')">新增学生</el-button>
        <el-upload :show-file-list="false" :before-upload="handleImport" accept=".xlsx,.xls">
          <el-button :icon="Upload">导入Excel</el-button>
        </el-upload>
        <el-button :icon="Download" @click="handleExport">导出Excel</el-button>
        <el-button type="danger" :icon="Delete" :disabled="selectedIds.length===0" @click="handleBatchDelete">批量删除</el-button>
      </el-space>
    </el-card>

    <!-- 数据表格 -->
    <el-card shadow="never">
      <el-table :data="tableData" v-loading="loading" @selection-change="handleSelectionChange" stripe border>
        <el-table-column type="selection" width="50" />
        <el-table-column prop="student_no" label="学号" width="120" />
        <el-table-column prop="name" label="姓名" width="100" />
        <el-table-column prop="gender" label="性别" width="60" />
        <el-table-column prop="class_name" label="班级" width="120" />
        <el-table-column prop="phone" label="联系电话" width="130" />
        <el-table-column prop="email" label="邮箱" min-width="180" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status==='在读'?'success':row.status==='休学'?'warning':'info'" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="$router.push(`/students/${row.id}/edit`)">编辑</el-button>
            <el-button type="danger" link size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div style="margin-top:16px;display:flex;justify-content:flex-end">
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.page_size"
          :total="pagination.total"
          :page-sizes="[10,20,50,100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="fetchData"
          @current-change="fetchData"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Upload, Download, Delete } from '@element-plus/icons-vue'
import { listStudents, deleteStudent, batchDeleteStudents, exportExcel, importExcel } from '../../api/students'
import { listClasses } from '../../api/classes'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()
const loading = ref(false)
const tableData = ref([])
const classList = ref([])
const selectedIds = ref([])

const filters = reactive({ keyword: '', class_id: '', gender: '', status: '' })
const pagination = reactive({ page: 1, page_size: 20, total: 0 })

async function fetchData() {
  loading.value = true
  try {
    const res = await listStudents({
      page: pagination.page,
      page_size: pagination.page_size,
      keyword: filters.keyword || undefined,
      class_id: filters.class_id || undefined,
      gender: filters.gender || undefined,
      status: filters.status || undefined,
    })
    tableData.value = res.items
    pagination.total = res.total
  } finally {
    loading.value = false
  }
}

function handleSearch() { pagination.page = 1; fetchData() }
function handleReset() {
  filters.keyword = ''; filters.class_id = ''; filters.gender = ''; filters.status = ''
  pagination.page = 1; fetchData()
}
function handleSelectionChange(rows) { selectedIds.value = rows.map(r => r.id) }

async function handleDelete(row) {
  if (!authStore.hasRole('admin')) { ElMessage.warning('需要管理员权限'); return }
  try {
    await ElMessageBox.confirm(`确定删除学生 "${row.name}" 吗？`, '确认删除', { type: 'warning' })
    await deleteStudent(row.id)
    ElMessage.success('删除成功')
    fetchData()
  } catch { /* 取消 */ }
}

async function handleBatchDelete() {
  if (!authStore.hasRole('admin')) { ElMessage.warning('需要管理员权限'); return }
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${selectedIds.value.length} 名学生吗？`, '批量删除', { type: 'warning' })
    await batchDeleteStudents(selectedIds.value)
    ElMessage.success('批量删除成功')
    selectedIds.value = []
    fetchData()
  } catch { /* 取消 */ }
}

async function handleImport(file) {
  try {
    const res = await importExcel(file)
    ElMessage.success(res.message)
    fetchData()
  } catch { /* handled by interceptor */ }
  return false // 阻止默认上传
}

async function handleExport() {
  try {
    const res = await exportExcel({
      keyword: filters.keyword || undefined,
      class_id: filters.class_id || undefined,
    })
    const url = URL.createObjectURL(res.data)
    const a = document.createElement('a')
    a.href = url; a.download = 'students.xlsx'; a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('导出成功')
  } catch { /* handled */ }
}

onMounted(async () => {
  classList.value = await listClasses()
  fetchData()
})
</script>
