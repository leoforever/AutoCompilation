# 龙芯智能编译平台 - 部署完成

## 服务状态
- 前端: http://10.40.65.201:3000/
- API: http://10.40.65.201:8000/docs
- PostgreSQL: localhost:5432

## 默认账号
- 用户名: admin
- 密码: admin123
- 角色: admin

## 启动方式
```bash
cd /root/.openclaw/workspace/compile-platform
bash setup.sh install   # 首次安装
bash setup.sh start     # 启动服务
bash setup.sh restart   # 重启服务
bash setup.sh status    # 查看状态
```

## 测试截图
- `/tmp/setup-verify.png` - 浏览器登录成功截图
