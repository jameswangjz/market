# Redis维护脚本使用说明

## 固定安装位置

在192.168.10.10上使用root执行，入口为：

```bash
/root/market/ops/redis/market_redis_maintenance.py --help
```

同目录的redis_source_admin.py与redis_source_continuity.py为配套文件，不能单独删除。脚本只操作market命名空间，不创建临时Pod，不更改探针、镜像、PVC或其他命名空间。

## 只读检查

```bash
/root/market/ops/redis/market_redis_maintenance.py status
```

active_source_healthy表示当前来源、进程及持久化检查正常，不证明历史缺口已解决。safe_sealed_resume_candidate表示已封存且具备恢复候选条件；explicit_gap_required表示需要人工核查并明确接受未知缺口。status只检查并保存报告，不执行备份、来源修改或重启。

## 计划内重启

```bash
/root/market/ops/redis/market_redis_maintenance.py planned-restart \
  --confirm-restart --reason '计划维护Redis'
```

自动顺序：检查当前来源与持久化 → 备份数据库和部署配置 → seal暂停并排空封存 → 重启market-redis并等待就绪 → 校验封存与持久化 → resume恢复原分代 → 检查来源与Worker就绪。

任何步骤失败立即停止，不自动open新缺口。维护期间计费接入暂停，Redis连接短暂中断也可能影响其他依赖，应安排维护窗口；仅在预检和备份成功、seal明确成功后才会重启。不要用本命令处理已异常的来源。

## 非计划重启后的恢复

Redis已经异常重启时，不再次重启Redis。管理员接受未知缺口后执行：

```bash
/root/market/ops/redis/market_redis_maintenance.py recover \
  --accept-gap --reason 'Redis非计划重启，确认保留未知计费缺口后恢复新分代'
```

来源健康时返回already_healthy_no_change，不新增分代、缺口或修改来源。已封存且具备恢复条件时拒绝open，提示使用resume。确需缺口时先检查持久化和备份，再open新分代、保留旧流旧游标与未知数量缺口，最后检查Worker就绪。

## 计划维护中断后继续恢复

修复故障后，若已存在有效封存证据，可执行：

```bash
/root/market/ops/redis/market_redis_maintenance.py resume \
  --reason '维护中断已处理，校验封存证据后继续恢复'
```

本命令只恢复，不重启Redis，不创建新分代。校验失败不要覆盖run_id、删除旧流、归零计数或修改封存摘要；核查后再决定是否明确接受缺口并执行recover。

## 报告与安全限制

- 默认以admin@market.local作为审计身份，API侧检查其是否为活动的平台系统管理员；需要其他管理员时使用--actor。
- 修改类命令必须填写--reason；计划重启另需--confirm-restart，异常恢复另需--accept-gap。
- 使用主机排他锁避免本脚本并发执行；此锁不替代数据库事务锁，其他绕过本脚本的直接操作也应避免并行。
- 报告、修改前数据库备份及部署快照保存在/root/market/backups/redis-maintenance-时间-随机后缀，输出的evidence字段给出本次目录；权限限制为root。
- 容器报告使用唯一文件名，复制到主机后清理对应临时报告，不覆盖旧报告。
- 返回码0表示命令完成、只读检查完成或健康无需变更；检查模式还须查看source_status判断是否健康。非0表示停止，先查报告，不自动重复执行修改命令。
- 不包含密码或Token参数，不自动认可数据连续性，不修复历史缺口、不补造用量、不重置额度。

## 验证范围

14项离线测试覆盖执行顺序、显式授权、备份/封存/重启失败停止、封存校验或持久化不健康拒绝恢复、健康时重复recover不修改来源，以及只读检查不进行业务修改。部署时只验证帮助和真实只读status，不再次重启业务Redis，也不为测试新增实际缺口。
