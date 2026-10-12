# Day 1 认证系统测试指南

## 一、自动化测试（推荐）

```bash
cd /root/.openclaw/workspace/compile-platform/backend

# 运行所有认证测试
python3 -m pytest tests/test_auth.py -v

# 预期结果：12 passed
```

## 二、手动 API 测试

### 1. 启动服务
```bash
cd backend
rm -f cache/compile_platform.db  # 清理旧数据
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 2. 测试注册
```bash
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"test123456","role":"user"}'
```
**预期**: 200 OK，返回用户信息

### 3. 测试登录
```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=testuser&password=test123456"
```
**预期**: 200 OK，返回 `{code, data: {access_token, token_type, expires_in}, user}`

### 4. 测试获取当前用户
```bash
TOKEN="your_token_here"
curl http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer $TOKEN"
```
**预期**: 200 OK，返回用户信息

### 5. 测试修改密码
```bash
curl -X POST "http://localhost:8000/api/auth/change-password?old_password=test123456&new_password=newpass123" \
  -H "Authorization: Bearer $TOKEN"
```
**预期**: 200 OK，返回 "密码修改成功"

### 6. 错误场景测试
```bash
# 错误密码
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=testuser&password=wrong"

# 无Token访问
curl http://localhost:8000/api/auth/me

# 重复注册
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"test123456","role":"user"}'
```
**预期**: 401 或 400，返回错误信息

## 三、前端测试

### 1. 启动前端
```bash
cd frontend
npm install
npm run dev
```

### 2. 测试流程
1. 访问 `http://localhost:3000` → 自动跳转到 `/login`
2. 输入用户名 `testuser`，密码 `test123456`
3. 登录成功后跳转到 `/workbench`
4. 侧边栏显示用户名
5. 点击右上角头像 → 退出登录
6. 再次访问 `/workbench` → 自动跳转登录页

## 四、Swagger UI 测试

访问 `http://localhost:8000/docs`

1. 点击 `/api/auth/register` → Try it out → Execute
2. 点击 `/api/auth/login` → 复制返回的 token
3. 点击 `/api/auth/me` → Authorize → 输入 `Bearer {token}` → Execute

## 五、测试检查清单

- [ ] 注册成功返回用户信息
- [ ] 重复用户名注册返回400
- [ ] 密码少于6位返回422
- [ ] 登录成功返回JWT token
- [ ] 错误密码返回401
- [ ] 不存在用户返回401
- [ ] 带Token访问/me返回用户信息
- [ ] 无Token访问/me返回401
- [ ] 无效Token返回401
- [ ] 修改密码成功
- [ ] 原密码错误返回400
- [ ] 禁用用户登录返回403
- [ ] 前端登录页正常显示
- [ ] 前端登录成功后跳转
- [ ] 前端Token过期自动跳转登录
