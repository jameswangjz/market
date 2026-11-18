# Redis重启与计费采集恢复

## 推荐方案

Redis持久化继续采用已有PVC、AOF everysec、noeviction与备份。持久化健康不等于计费连续性已经证明，不能直接覆盖来源标识或把未知用量按零计算。

| 情况 | 恢复方式 | 业务语义 |
| --- | --- | --- |
| 计划重启 | 管理员执行seal，暂停扣额并排空计费事件、生成封存摘要；重启后执行resume校验并重新绑定进程ID | 证据一致才延续原分代和原游标 |
| 非计划重启或封存校验失败 | 管理员核对后执行open，保留旧流、游标和事实，登记未知数量的未决缺口，建立新分代 | 新调用恢复采集；历史缺口不被掩盖 |
| Redis暂时不可达、数据库异常、事件不合法 | 采集暂停、就绪失败并记录安全错误码；进程继续定期重试 | 不创建虚假事实，不跳过坏事件或重置计数 |

当前选择显式管理员审查，不在每次重启时自动open，不因AOF状态正常自动resume。计划维护应将seal/校验/resume纳入运维编排；直接滚动重启Redis或整机断电不应被视为已封存。preStop不能覆盖掉电、强制终止等情况，不能作为唯一保证。

单虚拟机开发环境先使用上述方案；生产多节点可另行规划Redis高可用和持久化副本，但主从切换也必须遵守计费证据规则。若要求故障期间也自动恢复且保证计费不丢，应另行设计持久事件日志或数据库outbox及端到端幂等，不能仅通过探针或修改run_id获得该保证。

## 本次程序调整

仅修改api-charge-ledger-worker，不修改公共online_delivery_worker循环，以避免影响交付与退款Worker。

- 启动/存活探针读取`/tmp/api-charge-ledger-alive`，判断进程循环是否存活。
- 就绪探针读取`/tmp/api-charge-ledger-heartbeat`，仅在采集事务成功后更新。发生采集错误立即删除该成功心跳，保持未就绪而不触发无意义重启。
- 状态文件`/tmp/api-charge-ledger-status.json`区分ready与blocked，输出经过归类的错误码与检查时间，不输出连接串、凭据或事件原文。
- 连续同类错误日志最多每分钟一次，恢复时记录恢复日志；网络、数据库、坏事件及来源连续性异常不被当成成功。
- 若循环真正卡住，存活心跳也会超时，仍由K8S重启。没有通过取消探针来掩盖故障。

## 运维命令

使用已审核版本的`scripts/redis_source_admin.py`，在API容器内运行；每次output必须使用新的私有路径，并复制到主机备份目录，避免Pod删除后丢失。

```bash
PYTHONPATH=/app python -B redis_source_admin.py status --output /tmp/source-before-唯一时间.json
PYTHONPATH=/app python -B redis_source_admin.py seal --actor admin@market.local --reason '计划维护，暂停并封存计费源' --output /tmp/source-sealed-唯一时间.json
# 确认seal成功，再通过受控维护重启Redis，等待恢复。
PYTHONPATH=/app python -B redis_source_admin.py resume --actor admin@market.local --reason '维护完成，复核封存证据后恢复' --output /tmp/source-resumed-唯一时间.json
```

非计划故障需要管理员确认缺口后，使用open而不是伪造resume：

```bash
PYTHONPATH=/app python -B redis_source_admin.py open --actor admin@market.local --reason '非计划重启，保留旧源和游标，登记未决缺口并恢复新分代' --output /tmp/source-opened-唯一时间.json
```

封存期间跨自然日导致部分有到期时间的日计数证据消失，也可能无法证明连续性；应按校验结果转缺口流程，不强行修改摘要。任何自动化都必须使用相同校验和审计，不允许FLUSH、归零历史游标、补造零用量、将缺口自动标为已解决。

## 验证和范围

本次定向测试覆盖：事务成功提交、失败不确认事件、不删除源事件、来源暂停时存活与就绪分离、恢复心跳、日志脱敏、探针表达式，以及计划重启封存校验、非计划重启拒绝直接恢复、CAS失败重试、新代次保留旧游标和未知缺口。

当前环境恢复前备份数据库和来源诊断，执行一次带理由的open；恢复后检查新来源绑定、旧流/旧游标保留、缺口增加与Worker就绪。不会为了验证再重启业务Redis，不生成业务调用或修改订单，受保护订单及历史用量不动。计划/非计划重启规则的自动化测试使用隔离依赖，不将其称为本轮真实Redis重启演练。
