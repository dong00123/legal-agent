-- MySQL 初始化（只在数据卷为空时执行一次）
--
-- **这一句是为了兜底，不是必需的。** 应用侧的双重保险已经在了：
--   1. 每张表都声明了 ``mysql_charset=utf8mb4``（见 backend/app/db/models/user.py
--      的 MYSQL_ARGS，各模型都以 ``__table_args__`` 引用它）；
--   2. 库不存在时 ``backend/app/db/session.py`` 会自己建库，并带
--      ``DEFAULT CHARACTER SET utf8mb4``。
-- 那为什么还要写这一句：如果运维先用别的脚本建了库（或库是旧的），
-- 上面两条都管不到库级默认值 —— 那时表仍会对，但**连接字符集**可能不对，
-- 表现是入库成功、查询出来是问号。这里显式建一次，把这条路径也堵上。

CREATE DATABASE IF NOT EXISTS legal_agent
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- 应用账号只需对业务库有权限。给全局权限看起来省事，
-- 但一旦这个账号泄露，拖走的是整个实例上的所有库。
-- 口令是**占位符**，不得直接用于任何真实环境：正常部署时 compose 会按
-- deploy/.env 里的 MYSQL_PASSWORD 创建该账号，这句仅在镜像未建账号时兜底生效。
CREATE USER IF NOT EXISTS 'legal'@'%' IDENTIFIED BY 'changeme';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP
    ON legal_agent.* TO 'legal'@'%';
FLUSH PRIVILEGES;
