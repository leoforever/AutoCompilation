-- 龙芯 LoongArch 智能编译平台 · 数据库 DDL
-- 请人工审核后再执行

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'user',  -- user | admin
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);

-- 目标系统表
CREATE TABLE IF NOT EXISTS target_systems (
    system_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    arch VARCHAR(20) NOT NULL,
    base_image VARCHAR(255) NOT NULL,
    tag VARCHAR(50),
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 编译任务表
CREATE TABLE IF NOT EXISTS compile_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id VARCHAR(50) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,
    system_id INTEGER NOT NULL,
    software_name VARCHAR(100) NOT NULL,
    source_type VARCHAR(10) NOT NULL,  -- upload | git
    source_path VARCHAR(255),
    git_url VARCHAR(500),
    git_branch VARCHAR(100),
    build_args TEXT,
    compile_mode VARCHAR(20) NOT NULL DEFAULT 'container',  -- container | local
    status VARCHAR(20) NOT NULL DEFAULT 'submitted',
    matched_skills TEXT,
    depend_tree TEXT,
    log_output TEXT,
    artifact_path VARCHAR(255),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    duration_seconds INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (system_id) REFERENCES target_systems(system_id)
);

-- 工单表
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    source VARCHAR(10) NOT NULL,  -- cloud | client
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    title VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES compile_tasks(id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 工单消息表
CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role VARCHAR(10) NOT NULL,  -- user | admin
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 经验表
CREATE TABLE IF NOT EXISTS experiences (
    experience_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    skill_name VARCHAR(100) NOT NULL,
    skill_type VARCHAR(10) NOT NULL,  -- generic | specific
    system_id INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'submitted',
    skill_content TEXT,
    rejection_reason TEXT,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP,
    reviewer_id INTEGER,
    FOREIGN KEY (task_id) REFERENCES compile_tasks(id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (system_id) REFERENCES target_systems(system_id),
    FOREIGN KEY (reviewer_id) REFERENCES users(user_id)
);

-- Skill 经验库表
CREATE TABLE IF NOT EXISTS skills (
    skill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) UNIQUE NOT NULL,
    type VARCHAR(10) NOT NULL,
    description TEXT,
    system_ids TEXT,
    version VARCHAR(20),
    skill_content TEXT,
    script_content TEXT,
    is_active BOOLEAN DEFAULT 1,
    sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER,
    FOREIGN KEY (created_by) REFERENCES users(user_id)
);

-- 操作审计日志表
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id INTEGER,
    details TEXT,
    ip_address VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_tasks_task_id ON compile_tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON compile_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON compile_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_ticket_id ON tickets(ticket_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_experiences_user_id ON experiences(user_id);
CREATE INDEX IF NOT EXISTS idx_experiences_status ON experiences(status);

-- 初始化数据
INSERT OR IGNORE INTO users (username, password_hash, role) VALUES 
    ('admin', '$2b$12$Lq3.uG0Vv/EzLQ7Nh6dA3eM0K6F6tZ9XJr1Z5xY8wV9K2L3M4N5O6', 'admin');

INSERT OR IGNORE INTO target_systems (name, arch, base_image, tag, is_active) VALUES
    ('openEuler 24.03 LTS', 'loongarch64', 'loongarch64/oe2403:latest', 'latest', 1),
    ('统信 UOS V20 专业版', 'loongarch64', 'uos/v20:latest', 'latest', 1),
    ('龙蜥 Anolis OS 8', 'loongarch64', 'anolis/anolis8:latest', 'latest', 1);
