import http from './http'

export const listClasses = () => http.get('/api/classes').then(r => r.data)

export const createClass = data => http.post('/api/classes', data).then(r => r.data)

export const updateClass = (id, data) => http.put(`/api/classes/${id}`, data).then(r => r.data)

export const deleteClass = id => http.delete(`/api/classes/${id}`).then(r => r.data)
