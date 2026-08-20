import http from './http'

export const login = (username, password) =>
  http.post('/api/auth/login', { username, password }).then(r => r.data)

export const getMe = () => http.get('/api/auth/me').then(r => r.data)

export const listUsers = () => http.get('/api/auth/users').then(r => r.data)

export const createUser = data => http.post('/api/auth/users', data).then(r => r.data)
