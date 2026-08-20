import http from './http'

export const listStudents = (params = {}) => http.get('/api/students', { params }).then(r => r.data)

export const getStudent = id => http.get(`/api/students/${id}`).then(r => r.data)

export const createStudent = data => http.post('/api/students', data).then(r => r.data)

export const updateStudent = (id, data) => http.put(`/api/students/${id}`, data).then(r => r.data)

export const deleteStudent = id => http.delete(`/api/students/${id}`).then(r => r.data)

export const batchDeleteStudents = ids => http.post('/api/students/batch-delete', ids).then(r => r.data)

export const exportExcel = (params = {}) =>
  http.get('/api/students/export/excel', { params, responseType: 'blob' })

export const importExcel = file => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post('/api/students/import/excel', fd, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }).then(r => r.data)
}
