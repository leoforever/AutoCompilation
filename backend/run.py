#!/usr/bin/env python3
"""
龙芯 LoongArch 智能编译平台 · 启动脚本
"""
import os
import sys
import uvicorn

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(__file__))

def main():
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 8000))
    reload = os.environ.get('RELOAD', 'true').lower() == 'true'
    
    print(f"Starting LoongArch Compile Platform on {host}:{port}")
    print(f"Reload mode: {reload}")
    
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )

if __name__ == "__main__":
    main()
